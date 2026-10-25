"""End-to-end check for `repair --error-log PATH --summary`.

Builds three real logs (mixed rejected rows, a malformed line, and an
anonymized migration), requires the exact entry, repairability, raw-row, and
issue-category counts, and requires the command to leave its directory
byte-identical. Rewritten completion markers cover the truncated and
incomplete states; a missing log, a cut-off log, and every repair-only flag
exit 2 without writing a database, decisions file, or template.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
MESSY = REPO_ROOT / "examples" / "messy_transactions.json"
CONSTRAINTS = REPO_ROOT / "examples" / "constraints.json"
SCHEMA = REPO_ROOT / "examples" / "schema_config.json"
LOG_NAME = "airlock_errors.jsonl"


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


def _token(text: str) -> dict[str, str]:
    for line in text.splitlines():
        if line.startswith("repair_summary "):
            return dict(part.split("=", 1) for part in line.split()[1:])
    return {}


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _valid_record() -> dict:
    return {
        "transaction_id": "00000001-1111-4111-8111-111111111111",
        "account_id": "ACC-1001",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": "12.50",
        "currency": "EUR",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": "Northwind",
        "description": "ok",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that repair --summary reports log contents without writing."
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
        else Path(tempfile.mkdtemp(prefix="airlock-repair-summary-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    # Build a mixed log from the messy fixture.
    mixed_dir = work / "mixed"
    code, text = _run(
        [
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--error-log",
            "--output-dir",
            str(mixed_dir),
        ]
    )
    check("mixed migration exit", code == 1, f"exit={code}\n{text[-400:]}")
    mixed_log = mixed_dir / LOG_NAME
    check("mixed log exists", mixed_log.is_file(), f"missing {mixed_log}")

    # 1. A mixed log reports exact entry, repairability, and category counts.
    code, text = _run(["repair", "--error-log", str(mixed_log), "--summary"])
    check("mixed exit", code == 0, f"exit={code}\n{text[-600:]}")
    token = _token(text)
    expected = {
        "entries": "24",
        "repairable": "24",
        "non_repairable": "0",
        "raw_rows_omitted": "0",
        "issue_occurrences": "26",
        "issues_truncated": "0",
        "omitted_records": "0",
        "complete": "1",
    }
    for key, value in expected.items():
        check(f"mixed token {key}", token.get(key) == value, text[-600:])
    check("mixed entries line", "Entries: 24" in text, text[-600:])
    check(
        "mixed repairable line",
        "Repairable: 24; non-repairable: 0" in text,
        text[-600:],
    )
    check("mixed raw rows", "Raw rows omitted: 0" in text, text[-600:])
    check(
        "mixed categories",
        "Issues: value_constraint=17, extra_field=3, missing_field=3, "
        "new_enum_value=2, type_mismatch=1" in text,
        text[-600:],
    )
    check("mixed schema", "Schema: FinancialTransaction" in text, text[-600:])
    check(
        "mixed fingerprint",
        "fingerprint financial-transaction-v1" in text,
        text[-600:],
    )
    check("mixed anonymized", "Anonymized: no" in text, text[-600:])
    check("mixed complete", "Log: complete" in text, text[-600:])
    scenarios["mixed"] = token

    # 2. The summary reads only the log: no database, checkpoint, or staging file.
    solo = work / "solo"
    solo.mkdir(parents=True)
    shutil.copy2(mixed_log, solo / LOG_NAME)
    before = _tree_hashes(solo)
    code, text = _run(["repair", "--error-log", str(solo / LOG_NAME), "--summary"])
    check("solo exit", code == 0, f"exit={code}\n{text[-400:]}")
    check("solo untouched", _tree_hashes(solo) == before, "a file changed")
    check(
        "solo only log",
        sorted(path.name for path in solo.iterdir()) == [LOG_NAME],
        f"files: {sorted(path.name for path in solo.iterdir())}",
    )
    scenarios["read_only"] = {"files": sorted(before)}

    # 3. A source-level error is non-repairable and notices its missing raw row.
    broken_source = work / "broken.jsonl"
    invalid = _valid_record()
    invalid["amount"] = "-5.00"
    broken_source.write_text(
        "\n".join(
            [
                json.dumps(_valid_record()),
                json.dumps(invalid),
                "{not json",
                json.dumps(_valid_record()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    broken_dir = work / "broken"
    code, text = _run(
        [
            "--input",
            str(broken_source),
            "--error-log",
            "--output-dir",
            str(broken_dir),
        ]
    )
    check("broken migration exit", code == 1, f"exit={code}\n{text[-400:]}")
    code, text = _run(
        ["repair", "--error-log", str(broken_dir / LOG_NAME), "--summary"]
    )
    check("broken exit", code == 0, f"exit={code}\n{text[-600:]}")
    token = _token(text)
    for key, value in (
        ("entries", "2"),
        ("repairable", "1"),
        ("non_repairable", "1"),
        ("raw_rows_omitted", "1"),
        ("issue_occurrences", "2"),
    ):
        check(f"broken token {key}", token.get(key) == value, text[-600:])
    check(
        "broken categories",
        "Issues: source_error=1, value_constraint=1" in text,
        text[-600:],
    )
    check(
        "broken repairable line",
        "Repairable: 1; non-repairable: 1" in text,
        text[-600:],
    )
    scenarios["source_error"] = token

    # 4. An anonymized log summarizes even though a normal repair refuses it.
    anon_dir = work / "anonymized"
    code, text = _run(
        [
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--anonymize",
            "--error-log",
            "--output-dir",
            str(anon_dir),
        ]
    )
    check("anonymized migration exit", code == 1, f"exit={code}\n{text[-400:]}")
    anon_log = anon_dir / LOG_NAME
    code, text = _run(["repair", "--error-log", str(anon_log), "--summary"])
    check("anonymized exit", code == 0, f"exit={code}\n{text[-600:]}")
    token = _token(text)
    for key, value in (
        ("entries", "24"),
        ("repairable", "0"),
        ("non_repairable", "24"),
        ("raw_rows_omitted", "24"),
        ("issue_occurrences", "26"),
    ):
        check(f"anonymized token {key}", token.get(key) == value, text[-600:])
    check("anonymized line", "Anonymized: yes" in text, text[-600:])
    anon_before = _tree_hashes(anon_dir)
    code, text = _run(["repair", "--error-log", str(anon_log)])
    check("anonymized repair refused", code == 2, f"exit={code}\n{text[-400:]}")
    check(
        "anonymized repair wrote nothing",
        _tree_hashes(anon_dir) == anon_before,
        "a file changed",
    )
    scenarios["anonymized"] = token

    # 5. Rewritten completion markers cover the truncated and incomplete states.
    lines = mixed_log.read_text(encoding="utf-8").splitlines()
    truncated_log = work / "truncated" / LOG_NAME
    _write_lines(
        truncated_log,
        lines[:-1]
        + ['{"kind":"end","records":24,"complete":true,"truncated":true,"omitted_records":7}'],
    )
    code, text = _run(["repair", "--error-log", str(truncated_log), "--summary"])
    check("truncated exit", code == 0, f"exit={code}\n{text[-600:]}")
    check(
        "truncated line",
        "Log: truncated; omitted records: 7" in text,
        text[-600:],
    )
    check(
        "truncated token",
        _token(text).get("omitted_records") == "7"
        and _token(text).get("complete") == "1",
        text[-600:],
    )
    scenarios["truncated"] = _token(text)

    incomplete_log = work / "incomplete" / LOG_NAME
    _write_lines(
        incomplete_log,
        lines[:-1]
        + ['{"kind":"end","records":24,"complete":false,"truncated":false,"omitted_records":0}'],
    )
    code, text = _run(["repair", "--error-log", str(incomplete_log), "--summary"])
    check("incomplete exit", code == 0, f"exit={code}\n{text[-600:]}")
    check(
        "incomplete line",
        "Log: incomplete (the migration stopped before the end)" in text,
        text[-600:],
    )
    check("incomplete token", _token(text).get("complete") == "0", text[-600:])
    scenarios["incomplete"] = _token(text)

    # 6. A missing log and a cut-off log exit 2 and create nothing.
    missing = work / "absent.jsonl"
    code, text = _run(["repair", "--error-log", str(missing), "--summary"])
    check("missing exit", code == 2, f"exit={code}\n{text[-400:]}")
    check("missing message", "cannot read repair log" in text, text[-400:])
    check("missing no file", not missing.exists(), f"{missing} appeared")

    cut_dir = work / "cut"
    cut_dir.mkdir(parents=True)
    cut_log = cut_dir / LOG_NAME
    cut_log.write_bytes(mixed_log.read_bytes()[:-30])
    code, text = _run(["repair", "--error-log", str(cut_log), "--summary"])
    check("cut exit", code == 2, f"exit={code}\n{text[-400:]}")
    check("cut message", "cannot read repair log" in text, text[-400:])
    check(
        "cut only log",
        sorted(path.name for path in cut_dir.iterdir()) == [LOG_NAME],
        f"files: {sorted(path.name for path in cut_dir.iterdir())}",
    )
    scenarios["unreadable"] = {"exit": 2}

    # 7. Repair-only flags are refused before anything is opened.
    refused = [
        ("--output-db", [str(work / "refused.db")]),
        ("--config", [str(SCHEMA)]),
        ("--constraints", [str(CONSTRAINTS)]),
        ("--decisions", [str(work / "decisions.jsonl")]),
        ("--dry-run", []),
        ("--emit-decisions", [str(work / "template.jsonl")]),
        ("--anonymize", []),
        ("--overwrite", []),
    ]
    for flag, extra in refused:
        code, text = _run(
            ["repair", "--error-log", str(mixed_log), "--summary", flag, *extra]
        )
        check(f"{flag} exit", code == 2, f"exit={code}\n{text[-300:]}")
        check(f"{flag} message", flag in text, text[-300:])
    check("refused database", not (work / "refused.db").exists(), "a database appeared")
    check(
        "refused decisions",
        not (work / "decisions.jsonl").exists(),
        "a decisions file appeared",
    )
    check(
        "refused template",
        not (work / "template.jsonl").exists(),
        "a template appeared",
    )
    scenarios["refusals"] = [flag for flag, _ in refused]

    evidence = work / "repair_summary_verification.json"
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
            f"repair_summary_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"repair_summary_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
