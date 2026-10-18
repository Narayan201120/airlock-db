"""End-to-end check for `repair --decisions FILE --dry-run`.

Builds a migration with four rejected rows, requires a dry-run to leave the
database, the checkpoints, and the repair log byte-identical while printing
one planned outcome per entry, then applies the same file for real and
requires the predicted counts to match. A decision that would fail
validation exits 1 without writing, parse errors and a missing --decisions
exit 2, an unused entry is ignored, and a second dry-run reports the
entries the live run already saved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _valid_row() -> dict:
    return {
        "transaction_id": "00000001-1111-4111-8111-111111111111",
        "account_id": "ACC-1001",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": "12.50",
        "currency": "EUR",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": "Alpha One",
    }


def _row(index: int, **overrides: str) -> dict:
    row = {
        "transaction_id": f"{index:08d}-1111-4111-8111-111111111111",
        "account_id": f"ACC-10{index:02d}",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": "125.50",
        "currency": "USD",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": f"Party {index}",
    }
    row.update(overrides)
    return row


# One valid row and four rejected rows, in source order. The rejected rows
# become log entries 1..4: amount, currency, posted_at, account_id.
ROWS = [
    _valid_row(),
    _row(2, amount="-5.00"),
    _row(3, currency="EUROS"),
    _row(4, posted_at="not-a-date"),
    _row(5, account_id="x"),
]


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row_count(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        connection.close()


def _run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that repair --dry-run predicts and never writes."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = (
        args.work_dir.resolve()
        if args.work_dir
        else Path(tempfile.mkdtemp(prefix="airlock-decisions-dry-run-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    messy = work / "messy.jsonl"
    _write_jsonl(messy, ROWS)
    out = work / "out"
    log = out / "airlock_errors.jsonl"
    code, text = _run(
        ["--input", str(messy), "--output-dir", str(out), "--error-log", str(log)]
    )
    check("migration exit", code == 1, f"exit={code}\n{text[-400:]}")
    check("repair log written", log.is_file(), f"missing {log}")
    database = out / "target_migration.db"

    decisions = work / "decisions.jsonl"
    _write_jsonl(
        decisions,
        [
            {"entry": 1, "action": "set", "field": "amount", "value": "5.00"},
            {"entry": 2, "action": "set", "field": "currency", "value": "USD"},
            {"entry": 3, "action": "drop"},
            {"entry": 4, "action": "skip"},
            {"entry": 99, "action": "skip"},
        ],
    )

    # 1. The dry-run predicts every entry and writes nothing.
    before_database = _sha256(database)
    before_log = _sha256(log)
    rows_before = _row_count(database)
    code, text = _run(
        [
            "repair",
            "--error-log",
            str(log),
            "--decisions",
            str(decisions),
            "--dry-run",
        ]
    )
    check("dry-run exit", code == 0, f"exit={code}\n{text[-600:]}")
    check(
        "dry-run line 1",
        "entry 1: would correct (set amount)" in text,
        text[-600:],
    )
    check(
        "dry-run line 2",
        "entry 2: would correct (set currency)" in text,
        text[-600:],
    )
    check("dry-run line 3", "entry 3: would drop" in text, text[-600:])
    check(
        "dry-run line 4",
        "entry 4: would skip (left unchanged)" in text,
        text[-600:],
    )
    check(
        "dry-run unused line",
        "entry 99: ignored (no matching log entry)" in text,
        text[-600:],
    )
    check(
        "dry-run summary",
        "Preview run: 4 of 5 decisions would apply (2 correct, 1 drop, 1 skip); "
        "the database, the checkpoints, and the repair log were not touched." in text,
        text[-600:],
    )
    check(
        "dry-run database untouched",
        _sha256(database) == before_database,
        "the database changed",
    )
    check("dry-run log untouched", _sha256(log) == before_log, "the log changed")
    check(
        "dry-run rows untouched",
        _row_count(database) == rows_before,
        f"{_row_count(database)} != {rows_before}",
    )
    scenarios["dry_run"] = {"exit": code, "rows": rows_before}

    # 2. The live run applies the same file with the predicted counts.
    code, text = _run(
        ["repair", "--error-log", str(log), "--decisions", str(decisions)]
    )
    check("live repair exit", code == 0, f"exit={code}\n{text[-600:]}")
    check(
        "live repair counts",
        "Repair complete: 2 corrected, 1 dropped, 0 non-repairable entries." in text,
        text[-600:],
    )
    check(
        "live repair skip line",
        "Skipped: 1 entries were marked skip and left unchanged." in text,
        text[-600:],
    )
    check(
        "live repair decisions saved",
        "3 repair decisions saved" in text,
        text[-600:],
    )
    rows_after = _row_count(database)
    check(
        "live repair rows match the prediction",
        rows_after == rows_before + 2,
        f"{rows_after} != {rows_before} + 2",
    )
    scenarios["live_apply"] = {
        "corrected": 2,
        "dropped": 1,
        "skipped": 1,
        "rows_added": rows_after - rows_before,
    }

    # 3. A second dry-run reports the entries the live run saved.
    code, text = _run(
        [
            "repair",
            "--error-log",
            str(log),
            "--decisions",
            str(decisions),
            "--dry-run",
        ]
    )
    check("resumed dry-run exit", code == 0, f"exit={code}\n{text[-600:]}")
    check(
        "resumed dry-run lines",
        text.count("ignored (already saved in SQLite)") == 3,
        text[-600:],
    )
    check(
        "resumed dry-run summary",
        "Preview run: 1 of 5 decisions would apply (0 correct, 0 drop, 1 skip)" in text,
        text[-600:],
    )
    scenarios["resumed"] = {"ignored": 3, "applied": 1}

    # 4. A decision that would fail validation exits 1 and writes nothing.
    fail_out = work / "out-fail"
    fail_log = fail_out / "airlock_errors.jsonl"
    code, text = _run(
        [
            "--input",
            str(messy),
            "--output-dir",
            str(fail_out),
            "--error-log",
            str(fail_log),
        ]
    )
    check("fail migration exit", code == 1, f"exit={code}\n{text[-400:]}")
    fail_database = fail_out / "target_migration.db"
    failing = work / "failing.jsonl"
    _write_jsonl(
        failing,
        [{"entry": 1, "action": "set", "field": "amount", "value": "-5.00"}],
    )
    before_database = _sha256(fail_database)
    before_log = _sha256(fail_log)
    code, text = _run(
        [
            "repair",
            "--error-log",
            str(fail_log),
            "--decisions",
            str(failing),
            "--dry-run",
        ]
    )
    check("failing dry-run exit", code == 1, f"exit={code}\n{text[-600:]}")
    check(
        "failing dry-run message",
        "entry 1: correction still fails validation" in text,
        text[-600:],
    )
    check(
        "failing dry-run summary",
        "1 would fail validation" in text,
        text[-600:],
    )
    check(
        "failing dry-run database untouched",
        _sha256(fail_database) == before_database,
        "the database changed",
    )
    check(
        "failing dry-run log untouched",
        _sha256(fail_log) == before_log,
        "the log changed",
    )
    scenarios["would_fail"] = {"exit": code}

    # 5. A parse error exits 2, and --dry-run requires --decisions.
    broken = work / "broken.jsonl"
    _write_jsonl(broken, [{"entry": 1, "action": "explode"}])
    code, text = _run(
        [
            "repair",
            "--error-log",
            str(fail_log),
            "--decisions",
            str(broken),
            "--dry-run",
        ]
    )
    check("parse error exit", code == 2, f"exit={code}\n{text[-400:]}")
    check("parse error message", "unknown action" in text, text[-400:])
    code, text = _run(["repair", "--error-log", str(fail_log), "--dry-run"])
    check("no decisions exit", code == 2, f"exit={code}\n{text[-400:]}")
    check(
        "no decisions message",
        "--dry-run requires --decisions" in text,
        text[-400:],
    )
    scenarios["parse_errors"] = {"exit": 2}

    evidence = work / "decisions_dry_run_verification.json"
    evidence.write_text(
        json.dumps(
            {
                "status": "passed" if not failures else "failed",
                "scenarios": scenarios,
                "failures": failures,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if failures:
        for message in failures:
            print(f"FAIL {message}", file=sys.stderr)
        print(
            f"decisions_dry_run_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"decisions_dry_run_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
