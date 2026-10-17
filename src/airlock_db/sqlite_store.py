"""Transactional SQLite storage for validated financial transactions."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .schemas import FinancialTransaction
from .repair_progress import record_repair_progress


_SCHEMA_VERSION = 2
_PREVIOUS_SCHEMA_VERSION = 1
_DEFAULT_BATCH_SIZE = 500
_MAX_BATCH_SIZE = 10_000
_LOOKUP_CHUNK_SIZE = 500
_MAX_BATCH_ESTIMATED_BYTES = 4 * 1024 * 1024
_AMOUNT_QUANTUM = Decimal("0.01")
_CREATE_TRANSACTIONS_TABLE = """
CREATE TABLE transactions (
    transaction_id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    transaction_type TEXT NOT NULL
        CHECK (transaction_type IN ('DEBIT', 'CREDIT', 'REFUND')),
    status TEXT NOT NULL
        CHECK (status IN ('PENDING', 'POSTED', 'REVERSED')),
    counterparty TEXT NOT NULL,
    description TEXT,
    migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_CREATE_POSTED_AT_INDEX = "CREATE INDEX idx_transactions_posted_at ON transactions(posted_at)"
_INSERT_TRANSACTION = """
INSERT INTO transactions (
    transaction_id,
    account_id,
    posted_at,
    amount,
    currency,
    transaction_type,
    status,
    counterparty,
    description
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(transaction_id) DO NOTHING
"""
_REPLACE_TRANSACTION = """
INSERT INTO transactions (
    transaction_id,
    account_id,
    posted_at,
    amount,
    currency,
    transaction_type,
    status,
    counterparty,
    description
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(transaction_id) DO UPDATE SET
    account_id = excluded.account_id,
    posted_at = excluded.posted_at,
    amount = excluded.amount,
    currency = excluded.currency,
    transaction_type = excluded.transaction_type,
    status = excluded.status,
    counterparty = excluded.counterparty,
    description = excluded.description,
    migrated_at = CURRENT_TIMESTAMP
WHERE transactions.account_id IS NOT excluded.account_id
   OR transactions.posted_at IS NOT excluded.posted_at
   OR transactions.amount IS NOT excluded.amount
   OR transactions.currency IS NOT excluded.currency
   OR transactions.transaction_type IS NOT excluded.transaction_type
   OR transactions.status IS NOT excluded.status
   OR transactions.counterparty IS NOT excluded.counterparty
   OR transactions.description IS NOT excluded.description
"""
_SELECT_EXISTING = """
SELECT transaction_id, account_id, posted_at, amount, currency,
       transaction_type, status, counterparty, description
FROM transactions
WHERE transaction_id IN ({placeholders})
"""
_MIGRATION_PROGRESS_TABLE = "migration_progress"
_CREATE_MIGRATION_PROGRESS = """
CREATE TABLE IF NOT EXISTS migration_progress (
    scope TEXT NOT NULL,
    source TEXT NOT NULL,
    last_record INTEGER NOT NULL,
    source_size INTEGER NOT NULL,
    source_mtime REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, source)
)
"""
_INSERT_MIGRATION_PROGRESS = """
INSERT OR REPLACE INTO migration_progress (
    scope, source, last_record, source_size, source_mtime, updated_at
) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
"""
_SELECT_MIGRATION_PROGRESS = """
SELECT source, last_record, source_size, source_mtime
FROM migration_progress
WHERE scope = ?
ORDER BY source
"""


def _normalized_sql(statement: str) -> str:
    normalized = " ".join(statement.strip().rstrip(";").split()).casefold()
    return normalized.replace(" if not exists ", " ")


def _canonical_amount(amount: Decimal) -> str:
    """Render validated currency amounts as fixed-point strings with two places."""

    return format(amount.quantize(_AMOUNT_QUANTUM), "f")


def _estimate_row_size(row: tuple[Any, ...]) -> int:
    """Approximate batch payload bytes to decide when to flush early.

    Strings dominate real rows. Exact object sizes are unnecessary because
    this value only drives the pending-batch memory cap.
    """

    size = 64
    for value in row:
        if type(value) is str:
            size += len(value) + 49
        elif value is None:
            size += 16
        else:
            size += 64
    return size


def _rows_to_insert_skip_policy(
    batch: list[tuple[Any, ...]],
    first_rows: dict[str, tuple[Any, ...]],
    inserted_ids: set[str],
) -> list[tuple[Any, ...]] | tuple[tuple[Any, ...], ...]:
    """Keep skip-policy SQLite writes limited to IDs that can be inserted."""

    return tuple(
        row for transaction_id, row in first_rows.items()
        if transaction_id in inserted_ids
    )


def _progress_label(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"migration progress {name} must be a non-empty string")
    return value


def _progress_count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"migration progress {name} must be a non-negative integer")
    return value


def _progress_mtime(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"migration progress {name} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"migration progress {name} must be finite")
    return parsed


def _ensure_migration_progress_table(
    connection: sqlite3.Connection,
    *,
    verify: bool = True,
) -> None:
    """Create the auxiliary progress table in the caller's transaction.

    The table is not a managed record table, so schema version checks never
    look at it. Its shape is still verified once per store to keep an
    unrelated object from silently absorbing checkpoints.
    """

    if not verify:
        return
    existing = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name = ?",
        (_MIGRATION_PROGRESS_TABLE,),
    ).fetchone()
    if existing is None:
        connection.execute(_CREATE_MIGRATION_PROGRESS)
        return
    object_type, table_sql = existing
    if (
        object_type != "table"
        or not table_sql
        or _normalized_sql(table_sql) != _normalized_sql(_CREATE_MIGRATION_PROGRESS)
    ):
        raise sqlite3.DatabaseError(
            "migration progress table does not match the managed format"
        )


def record_migration_progress(
    connection: sqlite3.Connection,
    scope: str,
    source: str,
    last_record: int,
    source_size: int,
    source_mtime: float,
) -> None:
    """Store one resumable checkpoint row in the caller's open transaction."""

    connection.execute(
        _INSERT_MIGRATION_PROGRESS,
        (
            _progress_label(scope, "scope"),
            _progress_label(source, "source"),
            _progress_count(last_record, "last record"),
            _progress_count(source_size, "source size"),
            _progress_mtime(source_mtime, "source mtime"),
        ),
    )


def read_migration_progress(
    database_path: str | Path,
    scope: str,
) -> dict[str, tuple[int, int, float]]:
    """Read stored checkpoints for one scope from an existing target database.

    Returns a mapping of source to (last_record, source_size, source_mtime).
    A missing database or a database without checkpoint rows yields an empty
    mapping.
    """

    scope_value = _progress_label(scope, "scope")
    path = Path(database_path)
    if not path.is_file():
        return {}
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        existing = connection.execute(
            "SELECT type FROM sqlite_master WHERE name = ?",
            (_MIGRATION_PROGRESS_TABLE,),
        ).fetchone()
        if existing is None:
            return {}
        if existing[0] != "table":
            raise sqlite3.DatabaseError(
                "migration progress object is not a table"
            )
        progress: dict[str, tuple[int, int, float]] = {}
        rows = connection.execute(_SELECT_MIGRATION_PROGRESS, (scope_value,))
        for source, last_record, source_size, source_mtime in rows:
            key = str(source)
            if (
                isinstance(last_record, bool)
                or not isinstance(last_record, int)
                or last_record < 0
                or isinstance(source_size, bool)
                or not isinstance(source_size, int)
                or source_size < 0
                or isinstance(source_mtime, bool)
                or not isinstance(source_mtime, float)
                or not math.isfinite(source_mtime)
            ):
                raise sqlite3.DatabaseError(
                    "migration progress contains an invalid checkpoint"
                )
            progress[key] = (last_record, source_size, source_mtime)
        return progress
    finally:
        connection.close()


class _MigrationCheckpointMixin:
    """Opt-in resumable checkpoints shared by the SQLite target stores.

    A checkpoint writes the pending batch and the caller's progress row in one
    transaction, then commits it. Row counts keep accumulating, so a later
    commit() still summarizes the whole run.
    """

    def _flush(self) -> None:
        """Write the pending batch; the concrete store provides this."""

        raise NotImplementedError

    def checkpoint(
        self,
        scope: str,
        source: str,
        last_record: int,
        source_size: int,
        source_mtime: float,
    ) -> None:
        """Commit accepted rows and migration progress as one durable step."""

        connection = self._connection
        if connection is None or self._committed:
            raise RuntimeError("SQLite target store is not open for writes")
        _ensure_migration_progress_table(
            connection, verify=not self._migration_progress_ready
        )
        self._migration_progress_ready = True
        self._flush()
        record_migration_progress(
            connection, scope, source, last_record, source_size, source_mtime
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")


@dataclass(frozen=True, slots=True)
class SQLiteWriteSummary:
    """Committed row counts for one migration run."""

    inserted: int
    duplicate_transaction_ids: int
    updated: int
    conflicting_transaction_ids: int

    def to_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "duplicate_transaction_ids": self.duplicate_transaction_ids,
            "updated": self.updated,
            "conflicting_transaction_ids": self.conflicting_transaction_ids,
        }


class SQLiteTargetStore(_MigrationCheckpointMixin):
    """Initialize the target schema and insert rows in one transaction.

    Duplicate transaction IDs can be skipped or replaced according to the
    caller's explicit conflict policy. Amounts are stored as decimal strings
    so SQLite's floating-point numeric affinity cannot change their value.
    checkpoint() is optional: without it the run stays one transaction.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        conflict_policy: str = "skip",
    ) -> None:
        if not 1 <= batch_size <= _MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {_MAX_BATCH_SIZE}")
        if conflict_policy not in {"skip", "replace"}:
            raise ValueError("conflict_policy must be 'skip' or 'replace'")
        self.database_path = Path(database_path)
        self.batch_size = batch_size
        self.conflict_policy = conflict_policy
        self._connection: sqlite3.Connection | None = None
        self._batch: list[tuple[Any, ...]] = []
        self._batch_estimated_bytes = 0
        self._inserted = 0
        self._duplicates = 0
        self._updated = 0
        self._conflicts = 0
        self._committed = False
        self._repair_progress_ready = False
        self._migration_progress_ready = False
        self._index_pending = False

    def __enter__(self) -> SQLiteTargetStore:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        self._connection = connection
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"Target database schema version {current_version} is newer "
                    f"than this AirLock DB version supports ({_SCHEMA_VERSION})."
                )
            if current_version not in {0, _PREVIOUS_SCHEMA_VERSION, _SCHEMA_VERSION}:
                raise RuntimeError(f"Unsupported target database schema version {current_version}.")

            table = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = 'transactions'"
            ).fetchone()
            if table is None:
                if current_version != 0:
                    raise RuntimeError(
                        "Target database schema version is set, but the transactions table is missing."
                    )
                connection.execute(_CREATE_TRANSACTIONS_TABLE)
            else:
                object_type, table_sql = table
                if current_version == 0:
                    raise RuntimeError(
                        "An unmanaged transactions object already exists in the target database. "
                        "AirLock DB will not adopt it."
                    )
                if object_type != "table" or not table_sql:
                    raise RuntimeError("The target transactions object is not a managed table.")
                if _normalized_sql(table_sql) != _normalized_sql(_CREATE_TRANSACTIONS_TABLE):
                    raise RuntimeError(
                        "The target transactions table does not match the schema managed by AirLock DB."
                    )

            index = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = 'idx_transactions_posted_at'"
            ).fetchone()
            if index is None:
                # Build the posted-at index after the load so bulk inserts do
                # not pay per-row index maintenance.
                self._index_pending = True
            elif index[0] != "index" or not index[1] or _normalized_sql(index[1]) != _normalized_sql(_CREATE_POSTED_AT_INDEX):
                raise RuntimeError("The target posted-at index does not match the managed schema.")

            if current_version == _PREVIOUS_SCHEMA_VERSION:
                self._normalize_legacy_amounts(connection)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            connection.close()
            self._connection = None
            raise
        return self

    @staticmethod
    def _normalize_legacy_amounts(connection: sqlite3.Connection) -> None:
        last_id: str | None = None
        while True:
            if last_id is None:
                rows = connection.execute(
                    "SELECT transaction_id, amount FROM transactions "
                    "ORDER BY transaction_id LIMIT 1000"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT transaction_id, amount FROM transactions "
                    "WHERE transaction_id > ? ORDER BY transaction_id LIMIT 1000",
                    (last_id,),
                ).fetchall()
            if not rows:
                break
            last_id = str(rows[-1][0])
            updates: list[tuple[str, str]] = []
            for transaction_id, stored_amount in rows:
                try:
                    amount = Decimal(str(stored_amount))
                    if not amount.is_finite() or amount <= 0:
                        raise InvalidOperation
                    canonical = _canonical_amount(amount)
                except (InvalidOperation, ValueError) as exc:
                    raise RuntimeError(
                        f"Cannot migrate invalid amount for transaction {transaction_id}."
                    ) from exc
                if Decimal(canonical) != amount:
                    raise RuntimeError(
                        f"Cannot migrate an amount with more than two decimal places "
                        f"for transaction {transaction_id}."
                    )
                if str(stored_amount) != canonical:
                    updates.append((canonical, str(transaction_id)))
            if updates:
                connection.executemany(
                    "UPDATE transactions SET amount = ? WHERE transaction_id = ?",
                    updates,
                )

    def add(self, transaction: FinancialTransaction) -> None:
        """Queue one validated row and flush when the batch is full."""

        if self._connection is None or self._committed:
            raise RuntimeError("SQLite target store is not open for writes")
        row = (
            str(transaction.transaction_id),
            transaction.account_id,
            transaction.posted_at.isoformat(),
            _canonical_amount(transaction.amount),
            transaction.currency,
            transaction.transaction_type.value,
            transaction.status.value,
            transaction.counterparty,
            transaction.description,
        )
        self._batch.append(row)
        self._batch_estimated_bytes += _estimate_row_size(row)
        if (
            len(self._batch) >= self.batch_size
            or self._batch_estimated_bytes >= _MAX_BATCH_ESTIMATED_BYTES
        ):
            self._flush()

    def record_repair_progress(
        self,
        log_fingerprint: str,
        entry_number: int,
        outcome: str,
        decision_fingerprint: str,
    ) -> None:
        if self._connection is None or self._committed:
            raise RuntimeError("SQLite target store is not open for writes")
        record_repair_progress(
            self._connection,
            log_fingerprint,
            entry_number,
            outcome,
            decision_fingerprint,
            verify_table=not self._repair_progress_ready,
        )
        self._repair_progress_ready = True

    def _flush(self) -> None:
        if not self._batch:
            return
        if self._connection is None:
            raise RuntimeError("SQLite target store is not open")
        submitted = len(self._batch)
        unique_rows = {row[0]: row for row in self._batch}
        existing = self._existing_rows(tuple(unique_rows))
        inserted_ids = set(unique_rows) - existing.keys()
        self._duplicates += submitted - len(inserted_ids)
        if len(unique_rows) == submitted:
            # Every ID is unique inside this batch, so intra-batch value
            # conflicts are impossible. Alias the map instead of rebuilding it.
            first_rows: dict[str, tuple[Any, ...]] = unique_rows
            batch_conflicts: set[str] = set()
        else:
            first_rows = {}
            batch_conflicts = set()
            for row in self._batch:
                transaction_id = str(row[0])
                first_row = first_rows.setdefault(transaction_id, row)
                if tuple(first_row[1:]) != tuple(row[1:]):
                    batch_conflicts.add(transaction_id)
        persisted_conflicts = {
            transaction_id
            for transaction_id, row in unique_rows.items()
            if transaction_id in existing and tuple(row[1:]) != existing[transaction_id]
        }
        self._conflicts += len(batch_conflicts | persisted_conflicts)
        if self.conflict_policy == "replace":
            self._updated += sum(
                1
                for transaction_id, row in unique_rows.items()
                if transaction_id in existing and tuple(row[1:]) != existing[transaction_id]
            )
            statement = _REPLACE_TRANSACTION
        else:
            statement = _INSERT_TRANSACTION
        before = self._connection.total_changes
        if self.conflict_policy == "replace":
            rows_to_write = unique_rows.values()
        elif len(inserted_ids) == submitted:
            # Keep the no-duplicate path allocation-free for the common case.
            rows_to_write = self._batch
        else:
            # This path filters repeated IDs in a batch and IDs already stored.
            rows_to_write = _rows_to_insert_skip_policy(
                self._batch, first_rows, inserted_ids
            )
        self._connection.executemany(statement, rows_to_write)
        inserted = self._connection.total_changes - before
        self._inserted += len(inserted_ids) if self.conflict_policy == "replace" else inserted
        self._batch.clear()
        self._batch_estimated_bytes = 0

    def _existing_rows(self, transaction_ids: tuple[str, ...]) -> dict[str, tuple[Any, ...]]:
        if self._connection is None:
            raise RuntimeError("SQLite target store is not open")
        existing: dict[str, tuple[Any, ...]] = {}
        for offset in range(0, len(transaction_ids), _LOOKUP_CHUNK_SIZE):
            chunk = transaction_ids[offset : offset + _LOOKUP_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                _SELECT_EXISTING.format(placeholders=placeholders), chunk
            )
            for row in rows:
                existing[str(row[0])] = tuple(row[1:])
        return existing

    def commit(self) -> SQLiteWriteSummary:
        """Write any pending rows and commit the complete migration batch."""

        if self._connection is None or self._committed:
            raise RuntimeError("SQLite target store cannot be committed")
        self._flush()
        self._connection.commit()
        if self._index_pending:
            self._connection.execute(_CREATE_POSTED_AT_INDEX)
            self._connection.commit()
            self._index_pending = False
        self._committed = True
        return SQLiteWriteSummary(
            self._inserted, self._duplicates, self._updated, self._conflicts
        )

    def rollback(self) -> None:
        """Discard all writes from this run after a storage failure."""

        if self._connection is not None and not self._committed:
            self._connection.rollback()
        self._batch.clear()
        self._batch_estimated_bytes = 0
        self._inserted = 0
        self._duplicates = 0
        self._updated = 0
        self._conflicts = 0

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self._committed:
            self.rollback()
        self.close()
