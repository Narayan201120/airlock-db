"""End-to-end check for the small CLI wins: --sample, --source-query, and
`repair --emit-decisions`.

The script runs the real CLI against the example fixtures and a generated
SQLite source database, then pins what each preview, query, and decisions
template must produce, including the failures:
a sampled run that must not claim completeness, a query that must be refused
or reported, and a template that must be inert until an operator edits it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SAMPLE = REPO_ROOT / "examples" / "real_data" / "online_retail_sample.jsonl"
SAMPLE_SCHEMA = REPO_ROOT / "examples" / "real_data" / "online_retail_schema.json"
MESSY = REPO_ROOT / "examples" / "messy_transactions.json"
CONSTRAINTS = REPO_ROOT / "examples" / "constraints.json"


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _run(args: list[str], input_text: str | None = None) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=600,
    )
    return result.returncode, result.stdout + result.stderr


def _summary(output_dir: Path) -> dict:
    return json.loads((output_dir / "airlock_summary.json").read_text(encoding="utf-8"))


def _row_count(output_dir: Path) -> int:
    summary = _summary(output_dir)
    table = (
        "transactions"
        if summary["schema"]["name"] == "FinancialTransaction"
        else f"dynamic_records_{summary['schema']['fingerprint']}"
    )
    connection = sqlite3.connect(output_dir / "target_migration.db")
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


def _build_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE tx (transaction_id TEXT, account_id TEXT, counterparty TEXT, "
            "amount REAL, currency TEXT, transaction_type TEXT, status TEXT, "
            "posted_at TEXT, description TEXT)"
        )
        connection.executemany(
            "INSERT INTO tx VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("4d5e6f70-1122-4334-8556-778899aabb01", "ACC-1001", "Acme", 10.5, "USD",
                 "DEBIT", "POSTED", "2026-01-01T10:00:00Z", "settled"),
                ("4d5e6f70-1122-4334-8556-778899aabb02", "ACC-1002", "Beta", 20.25, "USD",
                 "CREDIT", "POSTED", "2026-01-02T11:00:00Z", "settled"),
                ("4d5e6f70-1122-4334-8556-778899aabb03", "ACC-1003", "Gamma", 30.0, "USD",
                 "DEBIT", "PENDING", "2026-01-03T12:00:00Z", "waiting"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify --sample, --source-query, and repair --emit-decisions end to end."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="airlock-cli-wins-"))
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    # 1. --sample stops a clean run at the requested count and says so.
    sampled = work / "sample-clean"
    code, text = _run(
        ["--input", str(SAMPLE), "--config", str(SAMPLE_SCHEMA),
         "--sample", "4", "--output-dir", str(sampled)]
    )
    check("sample clean exit", code == 0, f"exit={code}\n{text[-400:]}")
    records = _summary(sampled)["records"]
    check(
        "sample clean counts",
        records["checked"] == 4 and records["valid"] == 4,
        f"records={records}",
    )
    check(
        "sample clean markers",
        records["sample_limit"] == 4 and records["sample_reached"] is True,
        f"records={records}",
    )
    check("sample clean rows", _row_count(sampled) == 4, f"rows={_row_count(sampled)}")
    check("sample clean line", "Sample: stopped after 4 accepted records" in text, text[-300:])
    check(
        "sample clean notice",
        "Preview run: stopped after 4 accepted records" in (sampled / "airlock_report.html").read_text(encoding="utf-8"),
        "notice missing from report",
    )
    scenarios["sample"] = {"checked": records["checked"], "rows": 4}

    # 2. A limit larger than the input does not mark the run as sampled.
    full = work / "sample-full"
    code, text = _run(
        ["--input", str(SAMPLE), "--config", str(SAMPLE_SCHEMA),
         "--sample", "100", "--output-dir", str(full)]
    )
    check("sample full exit", code == 0, f"exit={code}\n{text[-400:]}")
    records = _summary(full)["records"]
    check(
        "sample full counts",
        records["checked"] == 10 and records["valid"] == 10 and records["sample_reached"] is False,
        f"records={records}",
    )
    check("sample full line absent", "Sample: stopped" not in text, text[-300:])
    check(
        "sample full notice absent",
        "Preview run:" not in (full / "airlock_report.html").read_text(encoding="utf-8"),
        "notice present in a complete run",
    )
    scenarios["sample_full"] = {"checked": records["checked"]}

    # 3. A sampled run tells repair that the log is partial.
    partial = work / "sample-errors"
    code, text = _run(
        ["--input", str(MESSY), "--constraints", str(CONSTRAINTS), "--error-log",
         "--sample", "4", "--output-dir", str(partial)]
    )
    check("sample errors exit", code == 0, f"exit={code}\n{text[-400:]}")
    records = _summary(partial)["records"]
    check(
        "sample errors counts",
        records["valid"] == 4 and records["checked"] < 32 and records["sample_reached"] is True,
        f"records={records}",
    )
    code, text = _run(
        ["repair", "--error-log", str(partial / "airlock_errors.jsonl"),
         "--constraints", str(CONSTRAINTS)],
        input_text="q\n",
    )
    check("sample errors repair exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "sample errors partial warning",
        "the source migration did not finish" in text,
        text[-500:],
    )
    scenarios["sample_errors"] = {"checked": records["checked"], "valid": records["valid"]}

    # 4. --emit-decisions writes one inert line per repairable entry.
    messy = work / "messy"
    code, text = _run(
        ["--input", str(MESSY), "--constraints", str(CONSTRAINTS), "--error-log",
         "--output-dir", str(messy)]
    )
    check("messy exit", code == 1, f"exit={code}\n{text[-300:]}")
    template = work / "template.jsonl"
    code, text = _run(
        ["repair", "--error-log", str(messy / "airlock_errors.jsonl"),
         "--constraints", str(CONSTRAINTS), "--emit-decisions", str(template)]
    )
    check("emit exit", code == 0, f"exit={code}\n{text[-400:]}")
    check("emit wrote template", template.is_file(), "template file missing")
    emitted = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines()]
    check("emit line count", len(emitted) == 24, f"lines={len(emitted)}")
    check(
        "emit lines are inert",
        all(entry.get("action") == "skip" and set(entry) == {"entry", "action"} for entry in emitted),
        f"first={emitted[0] if emitted else None}",
    )
    check(
        "emit entry order",
        [entry["entry"] for entry in emitted] == sorted(entry["entry"] for entry in emitted),
        "entries are not ascending",
    )
    check("emit summary line", "24 repairable entries" in text, text[-300:])
    scenarios["emit"] = {"lines": len(emitted)}

    # 5. Emit refuses to clobber, needs --overwrite, and cannot mix with --decisions.
    code, text = _run(
        ["repair", "--error-log", str(messy / "airlock_errors.jsonl"),
         "--constraints", str(CONSTRAINTS), "--emit-decisions", str(template)]
    )
    check("emit refuse exit", code == 2 and "already exists" in text, f"exit={code}\n{text[-200:]}")
    code, text = _run(
        ["repair", "--error-log", str(messy / "airlock_errors.jsonl"),
         "--constraints", str(CONSTRAINTS), "--emit-decisions", str(template), "--overwrite"]
    )
    check("emit overwrite exit", code == 0, f"exit={code}\n{text[-200:]}")
    code, text = _run(
        ["repair", "--error-log", str(messy / "airlock_errors.jsonl"),
         "--constraints", str(CONSTRAINTS), "--emit-decisions", str(template),
         "--decisions", str(template)]
    )
    check(
        "emit mixed exit",
        code == 2 and "cannot be combined" in text,
        f"exit={code}\n{text[-200:]}",
    )
    scenarios["emit_refusals"] = {"checked": 3}

    # 6. The template run is inert: nothing corrected, nothing dropped.
    before = _row_count(messy)
    code, text = _run(
        ["repair", "--error-log", str(messy / "airlock_errors.jsonl"),
         "--constraints", str(CONSTRAINTS), "--decisions", str(template)]
    )
    check("skip run exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "skip run outcome",
        "0 corrected, 0 dropped" in text and "Skipped: 24 entries" in text,
        text[-400:],
    )
    check("skip run untouched db", _row_count(messy) == before, "target rows changed")
    scenarios["skip_run"] = {"skipped": 24}

    # 7. An edited template applies exactly the edited line.
    edited = template.read_text(encoding="utf-8").splitlines()
    edited[0] = '{"entry": 1, "action": "drop"}'
    (work / "edited.jsonl").write_text("\n".join(edited) + "\n", encoding="utf-8")
    code, text = _run(
        ["repair", "--error-log", str(messy / "airlock_errors.jsonl"),
         "--constraints", str(CONSTRAINTS), "--decisions", str(work / "edited.jsonl")]
    )
    check("edited run exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "edited run outcome",
        "0 corrected, 1 dropped" in text and "1 repair decisions saved" in text,
        text[-400:],
    )
    code, text = _run(["report", "--output-dir", str(messy)])
    check(
        "edited run report",
        code == 0 and "Repair outcomes: 0 corrected, 1 dropped, 23 pending" in text,
        f"exit={code}\n{text[-300:]}",
    )
    scenarios["edited_run"] = {"dropped": 1}

    # 8. --source-query reads a filtered SELECT and stays verifiable.
    legacy = work / "legacy.db"
    _build_legacy_database(legacy)
    query = "SELECT * FROM tx WHERE status = 'POSTED'"
    query_run = work / "query"
    code, text = _run(
        ["--source-db", str(legacy), "--source-query", query, "--output-dir", str(query_run)]
    )
    check("query exit", code == 0, f"exit={code}\n{text[-400:]}")
    records = _summary(query_run)["records"]
    check(
        "query counts",
        records["checked"] == 2 and records["valid"] == 2,
        f"records={records}",
    )
    check("query rows", _row_count(query_run) == 2, f"rows={_row_count(query_run)}")
    check(
        "query label",
        _summary(query_run)["sources"] == [f"{legacy} (query)"],
        f"sources={_summary(query_run)['sources']}",
    )
    code, text = _run(
        ["verify", "--source-db", str(legacy), "--source-query", query,
         "--output-dir", str(query_run)]
    )
    check(
        "query verify",
        code == 0 and "Verification: consistent" in text,
        f"exit={code}\n{text[-400:]}",
    )
    scenarios["source_query"] = {"checked": records["checked"]}

    # 9. Bad queries and bad flags are refused before any run starts.
    refusals = {
        "not select": (["--source-db", str(legacy), "--source-query", "DELETE FROM tx"], "only SELECT statements"),
        "two statements": (["--source-db", str(legacy), "--source-query", "SELECT 1; SELECT 2"], "single SQL statement"),
        "with table": (
            ["--source-db", str(legacy), "--source-query", "SELECT * FROM tx", "--source-table", "tx"],
            "cannot be combined",
        ),
        "two queries": (
            ["--source-db", str(legacy), "--source-query", "SELECT * FROM tx", "--source-query", "SELECT * FROM tx"],
            "exactly one --source-query",
        ),
        "no database": (["--input", str(SAMPLE), "--source-query", "SELECT 1"], "exactly one --source-query"),
    }
    for label, (extra, expected) in refusals.items():
        code, text = _run([*extra, "--output-dir", str(work / "refused")])
        check(
            f"refuse {label}",
            code == 2 and expected in text,
            f"exit={code}\n{text[-200:]}",
        )
    code, text = _run(["verify", "--source-db", str(legacy), "--source-query", "DELETE FROM tx"])
    check(
        "refuse verify query",
        code == 2 and "only SELECT statements" in text,
        f"exit={code}\n{text[-200:]}",
    )
    scenarios["query_refusals"] = {"checked": len(refusals) + 1}

    # 10. A query that fails at runtime is a source error, not a crash.
    broken = work / "query-broken"
    code, text = _run(
        ["--source-db", str(legacy), "--source-query", "SELECT * FROM missing_table",
         "--output-dir", str(broken)]
    )
    check("query broken exit", code == 1, f"exit={code}\n{text[-300:]}")
    check(
        "query broken issue",
        _summary(broken)["issues"]["by_code"] == {"source_db_error": 1},
        f"issues={_summary(broken)['issues']['by_code']}",
    )
    scenarios["query_broken"] = {"issues": 1}

    # 11. Duplicate column names cannot be represented, so they are reported.
    duplicate = work / "query-duplicate"
    code, text = _run(
        ["--source-db", str(legacy),
         "--source-query", "SELECT amount AS value, amount AS value FROM tx",
         "--output-dir", str(duplicate)]
    )
    check("query duplicate exit", code == 1, f"exit={code}\n{text[-300:]}")
    check(
        "query duplicate issue",
        _summary(duplicate)["issues"]["by_code"] == {"source_db_query_columns": 1},
        f"issues={_summary(duplicate)['issues']['by_code']}",
    )
    scenarios["query_columns"] = {"issues": 1}

    evidence = work / "cli_wins_verification.json"
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
        print(f"cli_wins_failed failures={len(failures)} work_dir={work}", file=sys.stderr)
        return 1
    print(f"cli_wins_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
