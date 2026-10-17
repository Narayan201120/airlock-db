"""SQLite-backed checkpoints for interactive repair sessions."""

from __future__ import annotations

import sqlite3
from pathlib import Path


_TABLE_NAME = "airlock_repair_progress"
_CREATE_TABLE = """CREATE TABLE airlock_repair_progress (
    log_fingerprint TEXT NOT NULL,
    entry_number INTEGER NOT NULL CHECK (entry_number > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('corrected', 'dropped')),
    decision_fingerprint TEXT NOT NULL,
    PRIMARY KEY (log_fingerprint, entry_number)
) WITHOUT ROWID"""


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.strip().rstrip(";").split()).casefold()


def _check_table(connection: sqlite3.Connection) -> bool:
    existing = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name = ?", (_TABLE_NAME,)
    ).fetchone()
    if existing is None:
        return False
    object_type, sql = existing
    if object_type != "table" or not sql or _normalize_sql(sql) != _normalize_sql(_CREATE_TABLE):
        raise sqlite3.DatabaseError("repair progress table does not match the managed format")
    return True


def record_repair_progress(
    connection: sqlite3.Connection,
    log_fingerprint: str,
    entry_number: int,
    outcome: str,
    decision_fingerprint: str,
    *,
    verify_table: bool = True,
) -> None:
    """Record one accepted correction or drop in the caller's open transaction."""

    if len(log_fingerprint) != 64 or any(char not in "0123456789abcdef" for char in log_fingerprint):
        raise ValueError("log fingerprint must be a lowercase SHA-256 digest")
    if isinstance(entry_number, bool) or not isinstance(entry_number, int) or entry_number < 1:
        raise ValueError("repair entry number must be positive")
    if outcome not in {"corrected", "dropped"}:
        raise ValueError("repair outcome must be 'corrected' or 'dropped'")
    if len(decision_fingerprint) != 64 or any(
        char not in "0123456789abcdef" for char in decision_fingerprint
    ):
        raise ValueError("decision fingerprint must be a lowercase SHA-256 digest")
    if verify_table and not _check_table(connection):
        connection.execute(_CREATE_TABLE)
    connection.execute(
        "INSERT OR IGNORE INTO airlock_repair_progress "
        "(log_fingerprint, entry_number, outcome, decision_fingerprint) "
        "VALUES (?, ?, ?, ?)",
        (log_fingerprint, entry_number, outcome, decision_fingerprint),
    )
    saved = connection.execute(
        "SELECT outcome, decision_fingerprint FROM airlock_repair_progress "
        "WHERE log_fingerprint = ? AND entry_number = ?",
        (log_fingerprint, entry_number),
    ).fetchone()
    if saved is None or saved[0] != outcome or saved[1] != decision_fingerprint:
        raise sqlite3.IntegrityError(
            "this repair entry was handled differently by another session"
        )


class RepairProgressReader:
    """Stream prior decisions from an existing target database in entry order."""

    def __init__(
        self,
        database_path: str | Path,
        log_fingerprint: str,
        *,
        entry_count: int,
    ) -> None:
        self.database_path = Path(database_path)
        self.log_fingerprint = log_fingerprint
        self.entry_count = entry_count
        self._connection: sqlite3.Connection | None = None
        self._cursor: sqlite3.Cursor | None = None

    def __enter__(self) -> RepairProgressReader:
        if not self.database_path.exists():
            return self
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        if _check_table(self._connection):
            maximum = self._connection.execute(
                "SELECT MAX(entry_number) FROM airlock_repair_progress "
                "WHERE log_fingerprint = ?",
                (self.log_fingerprint,),
            ).fetchone()[0]
            if maximum is not None and maximum > self.entry_count:
                raise sqlite3.DatabaseError("repair progress refers to an entry outside this log")
            self._cursor = self._connection.execute(
                "SELECT entry_number, outcome FROM airlock_repair_progress "
                "WHERE log_fingerprint = ? ORDER BY entry_number",
                (self.log_fingerprint,),
            )
        return self

    def next(self) -> tuple[int, str] | None:
        if self._cursor is None:
            return None
        row = self._cursor.fetchone()
        if row is None:
            return None
        entry_number, outcome = row
        if (
            isinstance(entry_number, bool)
            or not isinstance(entry_number, int)
            or entry_number < 1
            or outcome not in {"corrected", "dropped"}
        ):
            raise sqlite3.DatabaseError("repair progress contains an invalid checkpoint")
        return entry_number, outcome

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._cursor = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
