"""Compare uncached anonymization with a bounded cache through the real CLI.

The cache implementation here is experimental and monkeypatched only for each
benchmark run. It is not enabled by AirLock DB.

Example:
    python benchmarks/benchmark_anonymizer_digest_cache.py --rows 100000
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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airlock_db import cli  # noqa: E402
from airlock_db.anonymize import RunAnonymizer  # noqa: E402


_CACHE_FIELDS = frozenset({"account_id", "counterparty"})
_BENCHMARK_KEY = bytes(range(32))
_ROW_COLUMNS = (
    "transaction_id, account_id, posted_at, amount, currency, "
    "transaction_type, status, counterparty, description"
)


def _write_input(
    path: Path,
    rows: int,
    account_cardinality: int,
    counterparty_cardinality: int,
    invalid_every: int,
) -> int:
    valid_rows = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(rows):
            record = {
                "transaction_id": f"00000000-0000-0000-0000-{index + 1:012d}",
                "account_id": f"ACC-{index % account_cardinality:07d}",
                "posted_at": "2026-01-15T12:30:00Z",
                "amount": "12.34",
                "currency": "USD",
                "transaction_type": "DEBIT",
                "status": "POSTED",
                "counterparty": (
                    f"Counterparty {index % counterparty_cardinality:08d}"
                ),
                "description": f"Unique description {index}",
            }
            if invalid_every and (index + 1) % invalid_every == 0:
                record.pop("counterparty")
            else:
                valid_rows += 1
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    return valid_rows


def _run_cli(
    input_path: Path,
    output_path: Path,
    *,
    cache_enabled: bool,
    capacity_per_field: int,
) -> tuple[int, float]:
    """Run real migration code with a temporary fixed key and cache patch."""

    original_init = RunAnonymizer.__init__
    original_digest = RunAnonymizer._digest
    states: dict[int, tuple[dict[str, dict[str, str]], set[str]]] = {}

    def init(instance: RunAnonymizer) -> None:
        original_init(instance)
        instance._RunAnonymizer__key = _BENCHMARK_KEY
        if cache_enabled:
            states[id(instance)] = ({}, set())

    def digest(instance: RunAnonymizer, field: str, value: str) -> str:
        if not cache_enabled or field not in _CACHE_FIELDS or capacity_per_field == 0:
            return original_digest(instance, field, value)
        caches, disabled = states[id(instance)]
        if field in disabled:
            return original_digest(instance, field, value)
        values = caches.setdefault(field, {})
        cached = values.get(value)
        if cached is not None:
            return cached
        result = original_digest(instance, field, value)
        if len(values) >= capacity_per_field:
            # Drop this field's raw keys and bypass it after high cardinality.
            values.clear()
            disabled.add(field)
        else:
            values[value] = result
        return result

    RunAnonymizer.__init__ = init
    RunAnonymizer._digest = digest
    try:
        args = cli._build_parser().parse_args(
            [
                "--input",
                str(input_path),
                "--output-dir",
                str(output_path),
                "--anonymize",
                "--batch-size",
                "1000",
                "--max-report-issues",
                "0",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            started = time.perf_counter()
            code = cli.run_migration(args)
            elapsed = time.perf_counter() - started
        return code, elapsed
    finally:
        RunAnonymizer.__init__ = original_init
        RunAnonymizer._digest = original_digest
        states.clear()


def _assert_same_database(left_path: Path, right_path: Path) -> int:
    query = f"SELECT {_ROW_COLUMNS} FROM transactions ORDER BY transaction_id"
    left = sqlite3.connect(left_path)
    right = sqlite3.connect(right_path)
    try:
        count = 0
        for left_row, right_row in zip(
            left.execute(query), right.execute(query), strict=True
        ):
            if left_row != right_row:
                raise RuntimeError(f"SQLite output differs at row {count + 1}")
            count += 1
        left_count = left.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        right_count = right.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        if count != left_count or count != right_count:
            raise RuntimeError("SQLite output row counts differ")
        return count
    finally:
        left.close()
        right.close()


def _benchmark_scenario(
    root: Path,
    *,
    name: str,
    rows: int,
    account_cardinality: int,
    counterparty_cardinality: int,
    invalid_every: int,
    repeats: int,
    capacity_per_field: int,
) -> dict[str, float | int]:
    input_path = root / f"{name}.jsonl"
    valid_rows = _write_input(
        input_path,
        rows,
        account_cardinality,
        counterparty_cardinality,
        invalid_every,
    )
    expected_code = 1 if valid_rows != rows else 0
    samples: dict[str, list[float]] = {"uncached": [], "cached": []}

    for trial in range(repeats):
        order = ("uncached", "cached") if trial % 2 == 0 else ("cached", "uncached")
        outputs: dict[str, Path] = {}
        with tempfile.TemporaryDirectory(prefix="airlock-cache-pair-", dir=root) as pair_dir:
            pair_root = Path(pair_dir)
            for mode in order:
                output_path = pair_root / mode
                code, elapsed = _run_cli(
                    input_path,
                    output_path,
                    cache_enabled=(mode == "cached"),
                    capacity_per_field=capacity_per_field,
                )
                if code != expected_code:
                    raise RuntimeError(
                        f"{name}/{mode}: CLI returned {code}, expected {expected_code}"
                    )
                samples[mode].append(elapsed)
                outputs[mode] = output_path / "target_migration.db"
            migrated = _assert_same_database(outputs["uncached"], outputs["cached"])
            if migrated != valid_rows:
                raise RuntimeError(
                    f"{name}: expected {valid_rows} SQLite rows, got {migrated}"
                )

    uncached = statistics.median(samples["uncached"])
    cached = statistics.median(samples["cached"])
    return {
        "median_uncached_seconds": uncached,
        "median_cached_seconds": cached,
        "change_vs_uncached_percent": (cached / uncached - 1) * 100,
        "rows_checked_per_trial": rows,
        "valid_rows_per_trial": valid_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--capacity-per-field", type=int, default=1024)
    args = parser.parse_args()
    if args.rows < 1 or args.repeats < 1 or args.capacity_per_field < 0:
        parser.error("rows and repeats must be positive; cache capacity must be non-negative")

    scenarios = (
        ("repeated_ids", 10, 25, 0),
        ("mixed_account_reuse", 10, args.rows, 0),
        ("all_unique", args.rows, args.rows, 0),
        ("invalid_rows", 10, 25, 1000),
    )
    with tempfile.TemporaryDirectory(prefix="airlock-cache-benchmark-") as temp_dir:
        root = Path(temp_dir)
        results = {
            name: _benchmark_scenario(
                root,
                name=name,
                rows=args.rows,
                account_cardinality=accounts,
                counterparty_cardinality=counterparties,
                invalid_every=invalid_every,
                repeats=args.repeats,
                capacity_per_field=args.capacity_per_field,
            )
            for name, accounts, counterparties, invalid_every in scenarios
        }

    print(
        json.dumps(
            {
                "rows": args.rows,
                "repeats": args.repeats,
                "cache_capacity_per_field": args.capacity_per_field,
                "cached_fields": sorted(_CACHE_FIELDS),
                "description_and_source_cached": False,
                "output_parity": "exact SQLite row parity verified for each trial",
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
