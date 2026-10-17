"""A/B benchmark for schema text sanitization through the full CLI.

Compares the current ASCII fast path in schemas.py with its prior Unicode
category-by-category check. The generated JSONL is streamed by AirLock DB.

Example:
    python benchmarks/benchmark_schema_text.py --rows 50000 --repeats 3
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unicodedata import category


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airlock_db import cli, schemas  # noqa: E402


_ROW_COLUMNS = (
    "transaction_id, account_id, posted_at, amount, currency, "
    "transaction_type, status, counterparty, description"
)


def _legacy_clean_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError("must not contain control, format, or surrogate characters")
    return value.strip()


def _write_input(path: Path, rows: int) -> int:
    valid_rows = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(rows):
            description = f"Unique description {index}"
            residue = index % 10_000
            if residue == 0:
                description += "\u0007"
            elif residue == 1:
                description += "\u200d"
            elif residue == 2:
                description += "\u0085"
            else:
                valid_rows += 1
            counterparty = (
                f"Counterparty Caf\u00e9 {index % 500:03d}"
                if index % 20 == 0
                else f"Counterparty {index % 500:03d}"
            )
            row = {
                "transaction_id": f"00000000-0000-0000-0000-{index + 1:012d}",
                "account_id": f"ACC-{index % 100:07d}",
                "posted_at": "2026-01-15T12:30:00Z",
                "amount": "12.34",
                "currency": "USD",
                "transaction_type": "DEBIT",
                "status": "POSTED",
                "counterparty": counterparty,
                "description": description,
            }
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    return valid_rows


def _run_cli(input_path: Path, output_path: Path, cleaner) -> tuple[int, float]:
    original = schemas._clean_text
    schemas._clean_text = cleaner
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
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            started = time.perf_counter()
            result = cli.run_migration(args)
            elapsed = time.perf_counter() - started
        return result, elapsed
    finally:
        schemas._clean_text = original


def _assert_same_database(left_path: Path, right_path: Path, expected_rows: int) -> None:
    query = f"SELECT {_ROW_COLUMNS} FROM transactions ORDER BY transaction_id"
    left = sqlite3.connect(left_path)
    right = sqlite3.connect(right_path)
    try:
        rows = 0
        for left_row, right_row in zip(
            left.execute(query), right.execute(query), strict=True
        ):
            if left_row != right_row:
                raise RuntimeError(f"database output differs at row {rows + 1}")
            rows += 1
        if rows != expected_rows:
            raise RuntimeError(f"expected {expected_rows} migrated rows, found {rows}")
    finally:
        left.close()
        right.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.rows < 1 or args.repeats < 1:
        parser.error("rows and repeats must be positive")

    optimized_cleaner = schemas._clean_text
    results = {"legacy": [], "ascii_fast_path": []}
    with tempfile.TemporaryDirectory(prefix="airlock-text-benchmark-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "source.jsonl"
        expected_rows = _write_input(input_path, args.rows)
        for trial in range(args.repeats):
            order = (
                ("legacy", _legacy_clean_text),
                ("ascii_fast_path", optimized_cleaner),
            )
            if trial % 2:
                order = tuple(reversed(order))
            outputs: dict[str, Path] = {}
            with tempfile.TemporaryDirectory(
                prefix="airlock-text-pair-", dir=root
            ) as pair_dir:
                pair_root = Path(pair_dir)
                for name, cleaner in order:
                    output_path = pair_root / name
                    code, elapsed = _run_cli(input_path, output_path, cleaner)
                    if code != 1:
                        raise RuntimeError(f"{name}: expected invalid-row exit code 1, got {code}")
                    results[name].append(elapsed)
                    outputs[name] = output_path / "target_migration.db"
                _assert_same_database(
                    outputs["legacy"], outputs["ascii_fast_path"], expected_rows
                )

    legacy = statistics.median(results["legacy"])
    fast = statistics.median(results["ascii_fast_path"])
    print(
        json.dumps(
            {
                "rows_checked": args.rows,
                "valid_rows": expected_rows,
                "repeats": args.repeats,
                "database_parity": "exact across every trial",
                "legacy_median_seconds": legacy,
                "ascii_fast_path_median_seconds": fast,
                "elapsed_change_percent": (fast / legacy - 1) * 100,
                "elapsed_saved_seconds": legacy - fast,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
