"""End-to-end check for `airlock-db verify`.

Runs real migrations, then reconciles each committed target against its
sources: an untouched handoff must be consistent, and every tampered variant
(a changed row, a deleted row, an inserted row, a changed source file, or the
wrong duplicate policy) must fail with the matching counts.
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
TICKET_SCHEMA = REPO_ROOT / "examples" / "schema_config.json"
REPORT_NAME = "airlock_verify.json"


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, result.stdout + result.stderr


def _report(output_dir: Path) -> dict:
    return json.loads((output_dir / REPORT_NAME).read_text(encoding="utf-8"))


def _dynamic_table(output_dir: Path) -> str:
    summary = json.loads(
        (output_dir / "airlock_summary.json").read_text(encoding="utf-8")
    )
    return f"dynamic_records_{summary['schema']['fingerprint']}"


def _mutate(database: Path, statement: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that `airlock-db verify` detects every handoff problem."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="airlock-handoff-"))
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    # Prepare one anonymized dynamic-schema run and the built-in fixture run.
    clean = work / "clean"
    code, text = _run(
        [
            "--input",
            str(SAMPLE),
            "--config",
            str(SAMPLE_SCHEMA),
            "--anonymize",
            "--output-dir",
            str(clean),
        ]
    )
    check("clean migration exit", code == 0, f"exit={code}\n{text[-400:]}")
    table = _dynamic_table(clean)

    # 1. An untouched handoff verifies, and anonymized fields stay out of the digests.
    code, text = _run(
        [
            "verify",
            "--input",
            str(SAMPLE),
            "--config",
            str(SAMPLE_SCHEMA),
            "--output-dir",
            str(clean),
        ]
    )
    check("clean verify exit", code == 0, f"exit={code}\n{text[-600:]}")
    check("clean verify message", "Verification: consistent" in text, text[-600:])
    report = _report(clean)
    check("clean report status", report["status"] == "consistent", str(report["status"]))
    counts = report["records"]
    check(
        "clean report counts",
        counts == {
            "accepted": 10,
            "distinct_ids": 10,
            "collapsed": 0,
            "stored": 10,
            "matched": 10,
            "changed": 0,
            "missing": 0,
            "unexpected": 0,
        },
        str(counts),
    )
    check(
        "clean excluded fields",
        report["excluded_fields"] == ["customer_id"],
        str(report["excluded_fields"]),
    )
    scenarios["clean"] = {"matched": counts["matched"]}

    # 2. A changed stored value fails with one changed row.
    changed = work / "changed"
    shutil.copytree(clean, changed)
    _mutate(
        changed / "target_migration.db",
        f"UPDATE {table} SET country = 'Elsewhere' "
        f"WHERE source_row = (SELECT MIN(source_row) FROM {table})",
    )
    code, text = _run(
        ["verify", "--input", str(SAMPLE), "--config", str(SAMPLE_SCHEMA), "--output-dir", str(changed)]
    )
    check("changed verify exit", code == 1, f"exit={code}\n{text[-400:]}")
    report = _report(changed)
    check(
        "changed report counts",
        report["records"]["changed"] == 1 and report["records"]["missing"] == 0,
        str(report["records"]),
    )
    check(
        "changed sample id",
        any(sample["id"] == "2" for sample in report["samples"]["changed"]),
        str(report["samples"]["changed"]),
    )
    scenarios["changed"] = {"changed": 1}

    # 3. A deleted row fails as missing.
    missing = work / "missing"
    shutil.copytree(clean, missing)
    _mutate(
        missing / "target_migration.db",
        f"DELETE FROM {table} WHERE source_row = (SELECT MIN(source_row) FROM {table})",
    )
    code, text = _run(
        ["verify", "--input", str(SAMPLE), "--config", str(SAMPLE_SCHEMA), "--output-dir", str(missing)]
    )
    check("missing verify exit", code == 1, f"exit={code}\n{text[-400:]}")
    report = _report(missing)
    check(
        "missing report counts",
        report["records"] == {
            "accepted": 10,
            "distinct_ids": 10,
            "collapsed": 0,
            "stored": 9,
            "matched": 9,
            "changed": 0,
            "missing": 1,
            "unexpected": 0,
        },
        str(report["records"]),
    )
    scenarios["missing"] = {"missing": 1}

    # 4. An inserted row fails as unexpected.
    unexpected = work / "unexpected"
    shutil.copytree(clean, unexpected)
    _mutate(
        unexpected / "target_migration.db",
        f"INSERT INTO {table} (source_row, invoice_no, stock_code, description, "
        "quantity, invoice_date, unit_price, customer_id, country) VALUES "
        "(99999, 'X', 'X', NULL, 1, '2026-01-01T00:00:00', '1.00', NULL, 'X')",
    )
    code, text = _run(
        ["verify", "--input", str(SAMPLE), "--config", str(SAMPLE_SCHEMA), "--output-dir", str(unexpected)]
    )
    check("unexpected verify exit", code == 1, f"exit={code}\n{text[-400:]}")
    report = _report(unexpected)
    check(
        "unexpected report counts",
        report["records"]["unexpected"] == 1 and report["records"]["matched"] == 10,
        str(report["records"]),
    )
    scenarios["unexpected"] = {"unexpected": 1}

    # 5. A source file that changed after the run fails as changed.
    tampered_input = work / "tampered.jsonl"
    original_text = SAMPLE.read_text(encoding="utf-8")
    tampered_text = original_text.replace(
        '"country": "United Kingdom"}', '"country": "Nowhere"}', 1
    )
    check("tamper applied", tampered_text != original_text, "sample text did not change")
    tampered_input.write_text(tampered_text, encoding="utf-8")
    unchanged_output = work / "clean-copy"
    shutil.copytree(clean, unchanged_output)
    code, text = _run(
        [
            "verify",
            "--input",
            str(tampered_input),
            "--config",
            str(SAMPLE_SCHEMA),
            "--output-dir",
            str(unchanged_output),
        ]
    )
    check("tampered input exit", code == 1, f"exit={code}\n{text[-400:]}")
    report = _report(unchanged_output)
    check(
        "tampered input counts",
        report["records"]["changed"] == 1 and report["records"]["matched"] == 9,
        str(report["records"]),
    )
    scenarios["tampered_input"] = {"changed": 1}

    # 6. Duplicate ids: skip and replace runs each verify with their own policy.
    duplicates = work / "duplicates.jsonl"
    duplicates.write_text(
        "\n".join(
            [
                '{"ticket_id": "TKT-000001", "customer_name": "Alice", "priority": "HIGH", "opened_at": "2026-01-05T08:00:00Z"}',
                '{"ticket_id": "TKT-000001", "customer_name": "Alice", "priority": "LOW", "opened_at": "2026-01-05T08:00:00Z"}',
                '{"ticket_id": "TKT-000002", "customer_name": "Bob", "priority": "NORMAL", "opened_at": "2026-01-06T09:00:00Z"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    skip_run = work / "duplicates-skip"
    code, text = _run(
        ["--input", str(duplicates), "--config", str(TICKET_SCHEMA), "--output-dir", str(skip_run)]
    )
    check("duplicates skip migration", code == 1, f"exit={code}\n{text[-400:]}")
    code, text = _run(
        [
            "verify",
            "--input",
            str(duplicates),
            "--config",
            str(TICKET_SCHEMA),
            "--output-dir",
            str(skip_run),
        ]
    )
    check("duplicates skip verify", code == 0, f"exit={code}\n{text[-400:]}")
    report = _report(skip_run)
    check(
        "duplicates skip counts",
        report["records"]["accepted"] == 3
        and report["records"]["distinct_ids"] == 2
        and report["records"]["collapsed"] == 1
        and report["records"]["matched"] == 2,
        str(report["records"]),
    )
    replace_run = work / "duplicates-replace"
    code, text = _run(
        [
            "--input",
            str(duplicates),
            "--config",
            str(TICKET_SCHEMA),
            "--conflict-policy",
            "replace",
            "--output-dir",
            str(replace_run),
        ]
    )
    check("duplicates replace migration", code == 0, f"exit={code}\n{text[-400:]}")
    code, text = _run(
        [
            "verify",
            "--input",
            str(duplicates),
            "--config",
            str(TICKET_SCHEMA),
            "--output-dir",
            str(replace_run),
        ]
    )
    check("duplicates replace verify", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "duplicates replace policy from summary",
        _report(replace_run)["conflict_policy"] == "replace",
        str(_report(replace_run)["conflict_policy"]),
    )
    # The wrong policy must fail: with skip the winner is the HIGH row, not LOW.
    code, text = _run(
        [
            "verify",
            "--input",
            str(duplicates),
            "--config",
            str(TICKET_SCHEMA),
            "--conflict-policy",
            "skip",
            "--output-dir",
            str(replace_run),
        ]
    )
    check("policy mismatch exit", code == 1, f"exit={code}\n{text[-400:]}")
    check(
        "policy mismatch warning",
        "warning" in text and "recorded conflict policy" in text,
        text[-500:],
    )
    scenarios["duplicates"] = {"skip": "consistent", "replace": "consistent"}

    # 7. The built-in schema path verifies too.
    builtin = work / "builtin"
    code, text = _run(
        [
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--output-dir",
            str(builtin),
        ]
    )
    check("builtin migration exit", code == 1, f"exit={code}\n{text[-400:]}")
    code, text = _run(
        [
            "verify",
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--output-dir",
            str(builtin),
        ]
    )
    check("builtin verify exit", code == 0, f"exit={code}\n{text[-800:]}")
    check(
        "builtin verify counts",
        _report(builtin)["records"]["matched"] == 8,
        str(_report(builtin)["records"]),
    )
    scenarios["builtin"] = {"matched": 8}

    # 8. A directory without a target database is refused.
    empty = work / "empty-output"
    empty.mkdir()
    code, text = _run(
        ["verify", "--input", str(SAMPLE), "--config", str(SAMPLE_SCHEMA), "--output-dir", str(empty)]
    )
    check("no database exit", code == 1, f"exit={code}")
    check("no database message", "no target database" in text, text[-300:])
    scenarios["no_database"] = {"exit": code}

    # 9. The wrong schema config is refused with a clear table message.
    code, text = _run(
        [
            "verify",
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--config",
            str(TICKET_SCHEMA),
            "--output-dir",
            str(builtin),
        ]
    )
    check("flag conflict exit", code == 2, f"exit={code}")
    check(
        "flag conflict message",
        "cannot be combined" in text,
        text[-300:],
    )
    code, text = _run(
        ["verify", "--input", str(SAMPLE), "--config", str(TICKET_SCHEMA), "--output-dir", str(builtin)]
    )
    check("wrong config exit", code == 1, f"exit={code}")
    check("wrong config message", "was not found" in text, text[-300:])
    scenarios["wrong_config"] = {"exit": code}

    # 10. Missing sources are refused by the argument parser.
    code, text = _run(["verify", "--output-dir", str(clean)])
    check("no sources exit", code == 2, f"exit={code}")
    check("no sources message", "at least one --input" in text, text[-300:])
    scenarios["no_sources"] = {"exit": code}

    evidence = work / "verify_handoff_verification.json"
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
        print(f"verify_handoff_failed failures={len(failures)} work_dir={work}", file=sys.stderr)
        return 1
    print(f"verify_handoff_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
