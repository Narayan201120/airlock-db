"""Benchmark deferred duplicate-skip accounting through the migration CLI.

Compares the previous select-first flush (stored rows were looked up for every
batch before writing) with the deferred flush (the batch is written first and
stored rows are only read back when a batch skipped something). Both variants
must produce identical databases and identical storage summaries.

Example:
    python benchmarks/benchmark_sqlite_duplicate_skip.py --rows 50000 --repeats 3
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airlock_db import cli, sqlite_store  # noqa: E402


_ROW_COLUMNS = (
    "transaction_id, account_id, posted_at, amount, currency, "
    "transaction_type, status, counterparty, description"
)
_SUMMARY_PATTERN = re.compile(r"Migration finished:.*")


def _write_input(path: Path, rows: int, unique_count: int) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(rows):
            identity = index % unique_count
            row = {
                "transaction_id": (
                    f"00000000-0000-0000-0000-{identity + 1:012d}"
                ),
                "account_id": f"ACC-{identity:07d}",
                "posted_at": "2026-01-15T12:30:00Z",
                "amount": "12.34",
                "currency": "USD",
                "transaction_type": "DEBIT",
                "status": "POSTED",
                "counterparty": f"Counterparty {identity:07d}",
                "description": f"Description {identity}",
            }
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


def _select_first_flush(self: sqlite_store.SQLiteTargetStore) -> None:
    """The previous skip-policy flush: look up stored rows, then write."""

    if not self._batch:
        return
    if self._connection is None:
        raise RuntimeError("SQLite target store is not open")
    if self.conflict_policy != "skip":
        raise RuntimeError("the select-first variant supports the skip policy only")
    submitted = len(self._batch)
    unique_rows = {row[0]: row for row in self._batch}
    existing = self._existing_rows(tuple(unique_rows))
    inserted_ids = set(unique_rows) - existing.keys()
    self._duplicates += submitted - len(inserted_ids)
    if len(unique_rows) == submitted:
        first_rows = unique_rows
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
    before = self._connection.total_changes
    if len(inserted_ids) == submitted:
        rows_to_write = self._batch
    else:
        rows_to_write = tuple(
            row
            for transaction_id, row in first_rows.items()
            if transaction_id in inserted_ids
        )
    self._connection.executemany(sqlite_store._INSERT_TRANSACTION, rows_to_write)
    inserted = self._connection.total_changes - before
    self._inserted += inserted
    self._batch.clear()
    self._batch_estimated_bytes = 0


def _run_cli(
    input_path: Path,
    output_path: Path,
    *,
    deferred: bool,
) -> tuple[int, float, str]:
    original_flush = sqlite_store.SQLiteTargetStore._flush
    if not deferred:
        sqlite_store.SQLiteTargetStore._flush = _select_first_flush
    try:
        args = cli._build_parser().parse_args(
            [
                "--input",
                str(input_path),
                "--output-dir",
                str(output_path),
                "--batch-size",
                "1000",
                "--max-report-issues",
                "0",
                "--verbose",
            ]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        logger = logging.getLogger("airlock_db")
        previous_level = logger.level
        handler = logging.StreamHandler(stderr)
        handler.setLevel(logging.INFO)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                started = time.perf_counter()
                result = cli.run_migration(args)
                elapsed = time.perf_counter() - started
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        summary_match = _SUMMARY_PATTERN.search(stderr.getvalue())
        if summary_match is None:
            raise RuntimeError("migration summary was not emitted")
        return result, elapsed, summary_match.group(0)
    finally:
        sqlite_store.SQLiteTargetStore._flush = original_flush


def _assert_same_database(left_path: Path, right_path: Path, expected_rows: int) -> None:
    query = f"SELECT {_ROW_COLUMNS} FROM transactions ORDER BY transaction_id"
    left = sqlite3.connect(left_path)
    right = sqlite3.connect(right_path)
    try:
        count = 0
        for left_row, right_row in zip(
            left.execute(query), right.execute(query), strict=True
        ):
            if left_row != right_row:
                raise RuntimeError(f"database output differs at row {count + 1}")
            count += 1
        if count != expected_rows:
            raise RuntimeError(f"expected {expected_rows} migrated rows, found {count}")
    finally:
        left.close()
        right.close()


def _benchmark_case(
    root: Path,
    *,
    rows: int,
    unique_count: int,
    repeats: int,
) -> dict[str, object]:
    input_path = root / f"source-{unique_count}.jsonl"
    _write_input(input_path, rows, unique_count)
    elapsed: dict[str, list[float]] = {"select_first": [], "deferred": []}
    summaries: list[str] = []
    for trial in range(repeats):
        order = (False, True) if trial % 2 == 0 else (True, False)
        output_paths: dict[bool, Path] = {}
        pair_root = Path(tempfile.mkdtemp(prefix="sqlite-skip-pair-", dir=root))
        try:
            for deferred in order:
                output_path = pair_root / ("deferred" if deferred else "select-first")
                result, duration, summary = _run_cli(
                    input_path, output_path, deferred=deferred
                )
                if result != 0:
                    raise RuntimeError(f"migration exited with status {result}")
                elapsed["deferred" if deferred else "select_first"].append(duration)
                output_paths[deferred] = output_path / "target_migration.db"
                summaries.append(summary)
            _assert_same_database(
                output_paths[False], output_paths[True], unique_count
            )
        finally:
            import shutil

            shutil.rmtree(pair_root)

    # Each A/B pair must report identical insert, update, and duplicate counts.
    if any(summaries[index] != summaries[index + 1] for index in range(0, len(summaries), 2)):
        raise RuntimeError("migration storage summaries differ between variants")
    select_first_median = statistics.median(elapsed["select_first"])
    deferred_median = statistics.median(elapsed["deferred"])
    return {
        "rows_checked_per_run": rows,
        "unique_ids": unique_count,
        "repeats_per_run": repeats,
        "database_parity": "exact",
        "storage_summary_parity": "exact",
        "select_first_median_seconds": select_first_median,
        "deferred_median_seconds": deferred_median,
        "elapsed_change_percent": (deferred_median / select_first_median - 1) * 100,
        "elapsed_saved_seconds": select_first_median - deferred_median,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.rows < 2 or args.repeats < 1:
        parser.error("rows must be at least 2 and repeats must be positive")

    with tempfile.TemporaryDirectory(prefix="airlock-sqlite-skip-benchmark-") as temp:
        root = Path(temp)
        results = [
            _benchmark_case(
                root,
                rows=args.rows,
                unique_count=args.rows,
                repeats=args.repeats,
            ),
            _benchmark_case(
                root,
                rows=args.rows,
                unique_count=max(1, args.rows // 10),
                repeats=args.repeats,
            ),
        ]
    print(json.dumps({"cases": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
