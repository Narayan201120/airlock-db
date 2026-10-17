"""SQLite storage for records validated by a configured dynamic schema."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from .schema_config import DynamicSchema
from .sqlite_store import (
    SQLiteWriteSummary,
    _MAX_BATCH_ESTIMATED_BYTES,
    _MAX_BATCH_SIZE,
    _MigrationCheckpointMixin,
    _estimate_row_size,
)
from .repair_progress import record_repair_progress


_LOOKUP_CHUNK_SIZE = 500
_INTERNAL_COLUMN = "migrated_at"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.strip().rstrip(";").split()).casefold()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class DynamicSQLiteTargetStore(_MigrationCheckpointMixin):
    """Store all fields of a configured model in a schema-specific SQLite table.

    The table name includes the validated schema fingerprint, so different
    client layouts cannot be mixed accidentally. Configured field names are
    validated before reaching this module and quoted in every SQL statement.
    checkpoint() is optional: without it the run stays one transaction.
    """

    def __init__(
        self,
        database_path: str | Path,
        schema: DynamicSchema,
        *,
        batch_size: int = 500,
        conflict_policy: str = "skip",
    ) -> None:
        if not 1 <= batch_size <= _MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {_MAX_BATCH_SIZE}")
        if conflict_policy not in {"skip", "replace"}:
            raise ValueError("conflict_policy must be 'skip' or 'replace'")
        self.database_path = Path(database_path)
        self.schema = schema
        self.batch_size = batch_size
        self.conflict_policy = conflict_policy
        self.table_name = f"dynamic_records_{schema.fingerprint}"
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
        self._field_names = tuple(field.name for field in schema.fields)
        self._id_index = self._field_names.index(schema.id_field)
        self._quoted_fields = tuple(_quote_identifier(name) for name in self._field_names)
        self._create_sql = self._build_create_sql()

    def _build_create_sql(self) -> str:
        columns: list[str] = []
        for field in self.schema.fields:
            definition = f"{_quote_identifier(field.name)} {field.sql_type}"
            if field.required:
                definition += " NOT NULL"
            if field.type_name == "enum":
                values = ", ".join(_sql_literal(value) for value in field.enum_values)
                definition += f" CHECK ({_quote_identifier(field.name)} IN ({values}))"
            elif field.type_name == "boolean":
                definition += f" CHECK ({_quote_identifier(field.name)} IN (0, 1))"
            columns.append(definition)
        columns.append(f'{_quote_identifier(_INTERNAL_COLUMN)} TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP')
        columns.append(f"PRIMARY KEY ({_quote_identifier(self.schema.id_field)})")
        return f"CREATE TABLE {_quote_identifier(self.table_name)} ({', '.join(columns)})"

    def __enter__(self) -> DynamicSQLiteTargetStore:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        self._connection = connection
        try:
            existing = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?",
                (self.table_name,),
            ).fetchone()
            if existing is None:
                connection.execute(self._create_sql)
            else:
                object_type, table_sql = existing
                if object_type != "table" or not table_sql:
                    raise RuntimeError("The configured dynamic target is not a table.")
                if _normalize_sql(table_sql) != _normalize_sql(self._create_sql):
                    raise RuntimeError(
                        "The existing dynamic target does not match the configured schema."
                    )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            connection.close()
            self._connection = None
            raise
        return self

    def _database_value(self, field_index: int, value: Any) -> Any:
        if value is None:
            return None
        # Pydantic has already normalized configured primitive fields. Return
        # them directly and consult field metadata only for boolean coercion.
        if type(value) is str:
            return value
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, bool):
            field = self.schema.fields[field_index]
            return int(value) if field.type_name == "boolean" else value
        if type(value) is int:
            return value
        if type(value) is float:
            return value
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return format(value, "f")
        if field.type_name == "boolean":
            return int(value)
        return value

    def add(self, record: BaseModel) -> None:
        """Queue one validated record for a bounded batch write."""

        if self._connection is None or self._committed:
            raise RuntimeError("SQLite target store is not open for writes")
        # Configured models contain only declared fields; reuse the validated
        # values instead of allocating a second mapping with model_dump().
        values = record.__dict__
        row = tuple(
            self._database_value(index, values.get(name))
            for index, name in enumerate(self._field_names)
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

    def _existing_rows(self, keys: tuple[Any, ...]) -> dict[Any, tuple[Any, ...]]:
        if self._connection is None:
            raise RuntimeError("SQLite target store is not open")
        existing: dict[Any, tuple[Any, ...]] = {}
        select_fields = ", ".join(self._quoted_fields)
        quoted_id = self._quoted_fields[self._id_index]
        for offset in range(0, len(keys), _LOOKUP_CHUNK_SIZE):
            chunk = keys[offset : offset + _LOOKUP_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT {select_fields} FROM {_quote_identifier(self.table_name)} "
                f"WHERE {quoted_id} IN ({placeholders})",
                chunk,
            )
            for row in rows:
                values = tuple(row)
                existing[values[self._id_index]] = values
        return existing

    def _insert_sql(self, *, replace: bool) -> str:
        columns = ", ".join(self._quoted_fields)
        placeholders = ", ".join("?" for _ in self._field_names)
        quoted_table = _quote_identifier(self.table_name)
        quoted_id = self._quoted_fields[self._id_index]
        statement = (
            f"INSERT INTO {quoted_table} ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT ({quoted_id}) "
        )
        updates = [
            f"{column} = excluded.{column}"
            for index, column in enumerate(self._quoted_fields)
            if index != self._id_index
        ]
        if not replace or not updates:
            return statement + "DO NOTHING"
        comparisons = " OR ".join(
            f"{quoted_table}.{column} IS NOT excluded.{column}"
            for index, column in enumerate(self._quoted_fields)
            if index != self._id_index
        )
        return (
            statement
            + "DO UPDATE SET "
            + ", ".join(updates)
            + f" WHERE {comparisons}"
        )

    def _flush(self) -> None:
        if not self._batch:
            return
        if self._connection is None:
            raise RuntimeError("SQLite target store is not open")

        submitted = len(self._batch)
        first_rows: dict[Any, tuple[Any, ...]] = {}
        last_rows: dict[Any, tuple[Any, ...]] = {}
        batch_conflicts: set[Any] = set()
        for row in self._batch:
            key = row[self._id_index]
            first = first_rows.setdefault(key, row)
            if first != row:
                batch_conflicts.add(key)
            if self.conflict_policy == "replace":
                last_rows[key] = row

        keys = tuple(first_rows)
        existing = self._existing_rows(keys)
        candidates = last_rows if self.conflict_policy == "replace" else first_rows
        persisted_conflicts = {
            key for key, row in candidates.items()
            if key in existing and row != existing[key]
        }
        self._conflicts += len(batch_conflicts | persisted_conflicts)
        inserted_keys = set(first_rows) - set(existing)
        self._duplicates += submitted - len(inserted_keys)
        if self.conflict_policy == "replace":
            self._updated += sum(
                1
                for key, row in last_rows.items()
                if key in existing and row != existing[key]
            )
            rows_to_write = tuple(last_rows.values())
        elif len(inserted_keys) == submitted:
            # Avoid an allocation when every submitted row is a new unique key.
            rows_to_write = self._batch
        else:
            # first_rows preserves first-row-wins for duplicate keys in a batch.
            rows_to_write = tuple(
                row for key, row in first_rows.items() if key in inserted_keys
            )

        self._connection.executemany(
            self._insert_sql(replace=self.conflict_policy == "replace"),
            rows_to_write,
        )
        self._inserted += len(inserted_keys)
        self._batch.clear()
        self._batch_estimated_bytes = 0

    def commit(self) -> SQLiteWriteSummary:
        """Flush pending rows and commit the complete migration transaction."""

        if self._connection is None or self._committed:
            raise RuntimeError("SQLite target store cannot be committed")
        self._flush()
        self._connection.commit()
        self._committed = True
        return SQLiteWriteSummary(
            self._inserted,
            self._duplicates,
            self._updated,
            self._conflicts,
        )

    def rollback(self) -> None:
        """Discard all pending writes from this run."""

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
