"""Benchmark JSONL validation and SQLite migration with synthetic support tickets."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence


def _bounded_integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}"
            )
        return number

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a local JSONL-to-SQLite migration with synthetic data."
    )
    parser.add_argument(
        "--rows",
        type=_bounded_integer(1, 999_999),
        default=100_000,
        help="number of generated records (default: 100000)",
    )
    parser.add_argument(
        "--runs",
        type=_bounded_integer(1, 20),
        default=3,
        help="number of independent CLI runs (default: 3)",
    )
    parser.add_argument(
        "--batch-size",
        type=_bounded_integer(1, 10_000),
        default=500,
        help="SQLite write batch size (default: 500)",
    )
    parser.add_argument(
        "--invalid-every",
        type=_bounded_integer(0, 999_999),
        default=0,
        help="mark every Nth row invalid; 0 keeps all rows valid (default: 0)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="dynamic schema config (default: examples/schema_config.json)",
    )
    return parser


def _write_jsonl(path: Path, row_count: int, invalid_every: int) -> None:
    priorities = ("LOW", "NORMAL", "HIGH")
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(1, row_count + 1):
            row = {
                "ticket_id": f"TKT-{index:06d}",
                "customer_name": f"Customer {index}",
                "priority": (
                    "INVALID"
                    if invalid_every and index % invalid_every == 0
                    else priorities[index % len(priorities)]
                ),
                "estimated_cost": "42.50",
                "opened_at": "2026-01-18T08:00:00Z",
                "notes": f"Imported ticket {index}",
            }
            stream.write(json.dumps(row, separators=(",", ":")))
            stream.write("\n")


def _stored_row_count(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE 'dynamic_records_%'"
        ).fetchall()
        if len(tables) != 1:
            raise RuntimeError(
                f"expected one managed dynamic table, found {len(tables)}"
            )
        table_name = str(tables[0][0]).replace('"', '""')
        result = connection.execute(
            f'SELECT COUNT(*) FROM "{table_name}"'
        ).fetchone()
        if result is None:
            raise RuntimeError("could not count migrated rows")
        return int(result[0])
    finally:
        connection.close()


def _run_benchmark(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    entry_point = project_root / "main.py"
    config = args.config or project_root / "examples" / "schema_config.json"
    if not entry_point.is_file():
        print(f"benchmark: CLI entry point not found: {entry_point}", file=sys.stderr)
        return 2
    if not config.is_file():
        print(f"benchmark: schema config not found: {config}", file=sys.stderr)
        return 2

    durations: list[float] = []
    with tempfile.TemporaryDirectory(prefix="airlock-benchmark-") as temp_dir:
        work = Path(temp_dir)
        source = work / "tickets.jsonl"
        _write_jsonl(source, args.rows, args.invalid_every)
        expected_invalid = args.rows // args.invalid_every if args.invalid_every else 0
        expected_migrated = args.rows - expected_invalid
        print(
            f"Generated {args.rows:,} JSONL records with {expected_invalid:,} invalid "
            f"({source.stat().st_size:,} bytes)."
        )

        for run_number in range(1, args.runs + 1):
            output = work / f"output-{run_number}"
            command = [
                sys.executable,
                str(entry_point),
                "--input",
                str(source),
                "--config",
                str(config.resolve()),
                "--output-dir",
                str(output),
                "--batch-size",
                str(args.batch_size),
            ]
            started = time.perf_counter()
            try:
                result = subprocess.run(
                    command,
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except OSError as exc:
                print(f"benchmark: could not start CLI: {exc}", file=sys.stderr)
                return 2
            elapsed = time.perf_counter() - started
            expected_exit_code = 1 if expected_invalid else 0
            if result.returncode != expected_exit_code:
                details = result.stderr.strip() or result.stdout.strip()
                print(
                    f"benchmark: CLI run {run_number} returned {result.returncode}; "
                    f"expected {expected_exit_code}: {details}",
                    file=sys.stderr,
                )
                return result.returncode or 1

            try:
                migrated = _stored_row_count(output / "target_migration.db")
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                print(f"benchmark: could not verify run {run_number}: {exc}", file=sys.stderr)
                return 2
            if migrated != expected_migrated:
                print(
                    f"benchmark: run {run_number} stored {migrated:,} rows; "
                    f"expected {expected_migrated:,}",
                    file=sys.stderr,
                )
                return 1

            durations.append(elapsed)
            rate = args.rows / elapsed if elapsed else 0.0
            print(
                f"run {run_number}/{args.runs}: {elapsed:.3f}s, "
                f"{rate:,.0f} records/s, {migrated:,}/{args.rows:,} rows migrated"
            )

    print(
        f"median: {statistics.median(durations):.3f}s across {args.runs} runs "
        f"(batch size {args.batch_size}, invalid every {args.invalid_every})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
