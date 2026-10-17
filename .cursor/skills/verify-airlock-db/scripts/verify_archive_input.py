"""End-to-end check that .gz and .zip inputs migrate like plain files.

Builds a small JSONL fixture, its gzip and zip twins, and a directory that
holds only a compressed file, then migrates each with the real CLI and
requires identical stored rows and counts. Broken archives must fail as
source issues, not crashes.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
COLUMNS = (
    "transaction_id",
    "account_id",
    "posted_at",
    "amount",
    "currency",
    "transaction_type",
    "status",
    "counterparty",
    "description",
)
RECORDS = (
    {
        "transaction_id": "3f8a1c2e-4b6d-4e9a-8f1c-2d5e7a9b0c11",
        "account_id": "ACC-1001",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": "125.50",
        "currency": "USD",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": "Northwind, Ltd",
        "description": "Monthly supplies",
    },
    {
        "transaction_id": "8b2d4f6a-1c3e-4a5b-9d7e-0f1a2b3c4d5e",
        "account_id": "ACC_2200",
        "posted_at": "2026-02-03T16:45:00+02:00",
        "amount": "89.99",
        "currency": "EUR",
        "transaction_type": "CREDIT",
        "status": "POSTED",
        "counterparty": "Blue Yonder Airlines",
        "description": "Retail banking fee",
    },
    {
        "transaction_id": "not-a-uuid",
        "account_id": "ACC-1001",
        "posted_at": "2026-03-01T10:00:00Z",
        "amount": "10.00",
        "currency": "USD",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": "Example Co",
        "description": "Rejected row",
    },
)


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _jsonl_lines() -> str:
    return "".join(json.dumps(record) + "\n" for record in RECORDS)


def _write_fixtures(work: Path) -> dict[str, Path]:
    payload = _jsonl_lines().encode("utf-8")
    plain = work / "plain.jsonl"
    plain.write_bytes(payload)
    with gzip.open(work / "plain.jsonl.gz", "wb") as stream:
        stream.write(payload)
    with zipfile.ZipFile(work / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plain.jsonl", payload)
        archive.writestr("notes.txt", "unsupported member; must be ignored")
    scan_dir = work / "scan"
    scan_dir.mkdir()
    with gzip.open(scan_dir / "copy.jsonl.gz", "wb") as stream:
        stream.write(payload)
    (work / "broken.zip").write_bytes(b"this is not a zip file")
    (work / "broken.jsonl.gz").write_bytes(b"this is not gzip data")
    return {
        "plain": plain,
        "gz": work / "plain.jsonl.gz",
        "zip": work / "bundle.zip",
        "scan": scan_dir,
        "broken_zip": work / "broken.zip",
        "broken_gz": work / "broken.jsonl.gz",
    }


def _run_cli(input_path: Path, output_dir: Path) -> tuple[int, str]:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "main.py"),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, (result.stdout + result.stderr)[-800:]


def _stored_rows(output_dir: Path) -> list[tuple[object, ...]]:
    database = output_dir / "target_migration.db"
    with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as db:
        return db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM transactions ORDER BY transaction_id"
        ).fetchall()


def _summary_counts(output_dir: Path) -> tuple[int, int, int]:
    summary = json.loads((output_dir / "airlock_summary.json").read_text(encoding="utf-8"))
    records = summary["records"]
    return records["checked"], records["valid"], records["invalid"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that .gz and .zip inputs migrate identically to plain files."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory for fixtures and outputs; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="airlock-archive-"))
    _reset_dir(work)
    paths = _write_fixtures(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    code, tail = _run_cli(paths["plain"], work / "out-plain")
    check("plain exit", code == 1, f"exit={code}\n{tail}")
    expected_rows = _stored_rows(work / "out-plain")
    expected_counts = _summary_counts(work / "out-plain")
    check("plain counts", expected_counts == (3, 2, 1), f"counts={expected_counts}")
    scenarios["plain"] = {"exit": code, "counts": list(expected_counts), "rows": len(expected_rows)}

    for name, key in (("gz", "gz"), ("zip", "zip"), ("scan", "scan")):
        output_dir = work / f"out-{name}"
        code, tail = _run_cli(paths[key], output_dir)
        rows = _stored_rows(output_dir)
        counts = _summary_counts(output_dir)
        check(f"{name} exit", code == 1, f"exit={code}\n{tail}")
        check(f"{name} counts", counts == expected_counts, f"counts={counts}")
        check(f"{name} rows", rows == expected_rows, "stored rows differ from the plain run")
        scenarios[name] = {"exit": code, "counts": list(counts), "rows": len(rows)}

    code, tail = _run_cli(paths["broken_zip"], work / "out-broken-zip")
    report = (work / "out-broken-zip" / "airlock_report.html").read_text(encoding="utf-8")
    check("broken zip exit", code == 1, f"exit={code}\n{tail}")
    check("broken zip issue", "archive_error" in report, "report is missing archive_error")
    check("broken zip rows", _stored_rows(work / "out-broken-zip") == [], "rows were stored")
    scenarios["broken_zip"] = {"exit": code, "issue": "archive_error"}

    code, tail = _run_cli(paths["broken_gz"], work / "out-broken-gz")
    report = (work / "out-broken-gz" / "airlock_report.html").read_text(encoding="utf-8")
    check("broken gz exit", code == 1, f"exit={code}\n{tail}")
    check(
        "broken gz issue",
        "not a gzipped file" in report.casefold(),
        "report is missing the gzip read error",
    )
    check("broken gz rows", _stored_rows(work / "out-broken-gz") == [], "rows were stored")
    scenarios["broken_gz"] = {"exit": code, "issue": "io_error"}

    evidence = work / "archive_verification.json"
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
        print(f"archive_input_failed failures={len(failures)} work_dir={work}", file=sys.stderr)
        return 1
    print(f"archive_input_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
