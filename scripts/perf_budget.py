"""Performance budget gate for AirLock DB.

Runs two small deterministic workloads through the real CLI and fails when
wall time exceeds a generous ceiling or stored row counts drift. Ceilings are
set far above observed times (10x or more) so shared CI runners do not flake;
this gate catches order-of-magnitude regressions, not noise.

Workloads:
  1. 2,000 valid dynamic-schema rows (throughput path).
  2. 2,000 dynamic-schema rows with 10 percent invalid plus a repair log,
     followed by verify (invalid path plus verify path).

Usage:
    python scripts/perf_budget.py [--work-dir PATH]

Exit 0 when every budget holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Generous ceilings in seconds for one CLI run on a shared runner.
BUDGET_MIGRATION_VALID = 30.0
BUDGET_MIGRATION_INVALID = 60.0
BUDGET_VERIFY = 60.0
# Minimum end-to-end throughput on the 2k valid workload.
BUDGET_MIN_RECORDS_PER_SECOND = 500.0

SCHEMA = {
    "version": 1,
    "model_name": "BudgetRow",
    "id_field": "order_id",
    "strict": False,
    "strip_whitespace": True,
    "fields": {
        "order_id": {"type": "string", "required": True, "min_length": 1, "max_length": 40},
        "status": {"type": "string", "required": False, "max_length": 40},
        "quantity": {"type": "integer", "required": False},
        "sku": {"type": "string", "required": False, "max_length": 40},
        "email": {"type": "string", "required": False, "max_length": 120},
        "phone": {"type": "string", "required": False, "max_length": 40},
    },
}

RULES = {
    "field_rules": [
        {"field": "status", "enum": ["open", "closed"]},
        {"field": "quantity", "min": 1, "max": 500},
        {"field": "sku", "pattern": "^SKU-[0-9]{4}$"},
        {"required_any_of": ["email", "phone"]},
    ]
}


def _write_jsonl(path: Path, rows: int, invalid_every: int) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(rows):
            if invalid_every and index % invalid_every == 0:
                row = {
                    "order_id": f"ORD-{index}",
                    "status": "bogus",
                    "quantity": 99999,
                    "sku": "BAD",
                    "email": "a@example.com",
                }
            else:
                row = {
                    "order_id": f"ORD-{index}",
                    "status": "open",
                    "quantity": 10,
                    "sku": "SKU-0001",
                    "email": "a@example.com",
                }
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


def _run_cli(args: list[str]) -> tuple[int, float, str]:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    elapsed = time.perf_counter() - started
    return result.returncode, elapsed, result.stdout + result.stderr


def _stored_rows(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'dynamic_records_%'"
        ).fetchone()
        if table is None:
            raise RuntimeError("no managed dynamic table found")
        count = connection.execute(
            f'SELECT COUNT(*) FROM "{table[0].replace(chr(34), chr(34) * 2)}"'
        ).fetchone()
        return int(count[0])
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AirLock DB performance budgets.")
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args()

    work = args.work_dir.resolve() if args.work_dir else Path(
        tempfile.mkdtemp(prefix="airlock-perf-budget-")
    )
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    (work / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    (work / "rules.json").write_text(json.dumps(RULES), encoding="utf-8")

    # Workload 1: valid rows, throughput path.
    source = work / "valid.jsonl"
    _write_jsonl(source, 2000, 0)
    out = work / "out-valid"
    code, elapsed, _ = _run_cli(
        [
            "--input", str(source),
            "--config", str(work / "schema.json"),
            "--constraints", str(work / "rules.json"),
            "--output-dir", str(out),
            "--batch-size", "1000",
            "--max-report-issues", "0",
        ]
    )
    rate = 2000 / elapsed if elapsed > 0 else 0.0
    print(f"valid-migration: exit={code} {elapsed:.2f}s {rate:,.0f} records/s")
    if code != 0:
        failures.append(f"valid migration exited {code}, expected 0")
    if elapsed > BUDGET_MIGRATION_VALID:
        failures.append(
            f"valid migration took {elapsed:.2f}s, budget {BUDGET_MIGRATION_VALID:.0f}s"
        )
    if rate < BUDGET_MIN_RECORDS_PER_SECOND:
        failures.append(
            f"valid migration rate {rate:,.0f} records/s below "
            f"{BUDGET_MIN_RECORDS_PER_SECOND:,.0f} records/s"
        )
    try:
        stored = _stored_rows(out / "target_migration.db")
        if stored != 2000:
            failures.append(f"valid migration stored {stored} rows, expected 2000")
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        failures.append(f"could not count valid rows: {exc}")

    # Workload 2: invalid rows with a repair log, then verify.
    source = work / "messy.jsonl"
    _write_jsonl(source, 2000, 10)
    out = work / "out-messy"
    code, elapsed, _ = _run_cli(
        [
            "--input", str(source),
            "--config", str(work / "schema.json"),
            "--constraints", str(work / "rules.json"),
            "--error-log",
            "--output-dir", str(out),
            "--batch-size", "1000",
            "--max-report-issues", "1000",
        ]
    )
    print(f"invalid-migration: exit={code} {elapsed:.2f}s")
    if code != 1:
        failures.append(f"invalid migration exited {code}, expected 1")
    if elapsed > BUDGET_MIGRATION_INVALID:
        failures.append(
            f"invalid migration took {elapsed:.2f}s, budget {BUDGET_MIGRATION_INVALID:.0f}s"
        )
    try:
        stored = _stored_rows(out / "target_migration.db")
        if stored != 1800:
            failures.append(f"invalid migration stored {stored} rows, expected 1800")
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        failures.append(f"could not count invalid-path rows: {exc}")

    code, elapsed, text = _run_cli(
        [
            "verify",
            "--input", str(source),
            "--config", str(work / "schema.json"),
            "--constraints", str(work / "rules.json"),
            "--output-dir", str(out),
        ]
    )
    print(f"verify: exit={code} {elapsed:.2f}s")
    if code != 0:
        failures.append(f"verify exited {code}, expected 0")
    if elapsed > BUDGET_VERIFY:
        failures.append(f"verify took {elapsed:.2f}s, budget {BUDGET_VERIFY:.0f}s")
    if "consistent" not in text:
        failures.append("verify output missing the 'consistent' verdict")

    if args.work_dir is None:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        for failure in failures:
            print(f"BUDGET FAIL {failure}", file=sys.stderr)
        return 1
    print("perf_budget_ok workloads=3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
