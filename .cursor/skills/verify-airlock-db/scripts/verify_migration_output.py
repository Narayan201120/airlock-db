from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _token_shape(table: str, field: str) -> tuple[str, int]:
    if table.startswith("dynamic_records_"):
        return "", 32
    built_in_shapes = {
        "account_id": ("A", 31),
        "counterparty": ("CP-", 32),
        "description": ("DESC-", 48),
    }
    try:
        return built_in_shapes[field]
    except KeyError as exc:
        raise ValueError(
            f"no built-in anonymization token format is defined for {field!r}"
        ) from exc


def verify(args: argparse.Namespace) -> dict[str, object]:
    report_path = args.output_dir / "airlock_report.html"
    if not report_path.is_file():
        raise ValueError("output directory is missing the HTML report")

    row_count: int | None = None
    token_violations: dict[str, int] | None = None
    integrity: str | None = None
    posted_at_index: bool | None = None
    if not args.skip_sqlite:
        database = args.output_dir / "target_migration.db"
        if not database.is_file():
            raise ValueError("output directory is missing the SQLite database")
        with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ]
            targets = [
                name
                for name in tables
                if name == "transactions" or name.startswith("dynamic_records_")
            ]
            if len(targets) != 1:
                raise ValueError(f"expected one managed output table, found {len(targets)}")
            table = targets[0]
            row_count = db.execute(
                f"SELECT COUNT(*) FROM {_identifier(table)}"
            ).fetchone()[0]
            if table == "transactions":
                posted_at_index = (
                    db.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                        "AND name = 'idx_transactions_posted_at'"
                    ).fetchone()[0]
                    == 1
                )
                if not posted_at_index:
                    raise ValueError(
                        "committed transactions table is missing its posted-at index"
                    )
            columns = {
                row[1]
                for row in db.execute(f"PRAGMA table_info({_identifier(table)})")
            }
            if args.anonymized_field:
                token_violations = {}
                for field_name in args.anonymized_field:
                    if field_name not in columns:
                        raise ValueError(
                            f"anonymized field {field_name!r} is not in the target table"
                        )
                    prefix, digest_length = _token_shape(table, field_name)
                    token_pattern = prefix + "[0-9A-F]" * digest_length
                    field = _identifier(field_name)
                    token_violations[field_name] = db.execute(
                        f"SELECT COUNT(*) FROM {_identifier(table)} "
                        f"WHERE {field} IS NOT NULL AND "
                        f"(length({field}) != ? OR {field} NOT GLOB ?)",
                        (len(prefix) + digest_length, token_pattern),
                    ).fetchone()[0]

    jsonl_rows: int | None = None
    if args.expected_jsonl_rows is not None:
        jsonl_path = args.output_dir / "target_migration.jsonl"
        if not jsonl_path.is_file():
            raise ValueError("output directory is missing target_migration.jsonl")
        count = 0
        with jsonl_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                json.loads(line)
                count += 1
        if count != args.expected_jsonl_rows:
            raise ValueError(
                f"JSONL row count mismatch: expected {args.expected_jsonl_rows}, found {count}"
            )
        jsonl_rows = count

    report = report_path.read_text(encoding="utf-8")
    chart = re.search(
        r'<svg\b[^>]*aria-label="(?:([\d,]+) valid records and ([\d,]+) invalid records'
        r'|No records were validated)"',
        report,
        re.IGNORECASE,
    )
    checked = re.search(
        r"<dt>\s*Records checked\s*</dt>\s*<dd>([\d,]+)</dd>",
        report,
        re.IGNORECASE,
    )
    report_lower = report.casefold()
    type_mismatch_note_present = (
        "no automatic type conversion was applied" in report_lower
    )
    external_asset = re.search(
        r"(?:src|href)\s*=\s*['\"]\s*(?:https?:)?//"
        r"|url\(\s*['\"]?\s*(?:https?:)?//"
        r"|@import\s+(?:url\(\s*)?['\"]?\s*(?:https?:)?//",
        report_lower,
    )
    if integrity is not None and integrity != "ok":
        raise ValueError(f"SQLite integrity check failed: {integrity}")
    if chart is None or checked is None or "<svg" not in report_lower:
        raise ValueError("HTML report is missing its success/failure chart or record count")
    if "<script" in report_lower or external_asset:
        raise ValueError("HTML report contains a script or external asset reference")
    if args.expect_type_mismatch_note and not type_mismatch_note_present:
        raise ValueError("HTML report is missing the no-conversion type-mismatch note")

    chart_valid = (
        int(chart.group(1).replace(",", "")) if chart.group(1) is not None else 0
    )
    chart_invalid = (
        int(chart.group(2).replace(",", "")) if chart.group(2) is not None else 0
    )
    observed = {
        "records_checked": int(checked.group(1).replace(",", "")),
        "valid_records": chart_valid,
        "invalid_records": chart_invalid,
        "sqlite_rows": row_count,
        "sqlite_integrity": integrity,
        "posted_at_index": posted_at_index,
        "jsonl_rows": jsonl_rows,
        "report_standalone": True,
        "type_mismatch_note_present": type_mismatch_note_present,
        "anonymized_field_token_violations": token_violations,
    }
    expected = {
        "records_checked": args.expected_checked,
        "valid_records": args.expected_valid,
        "invalid_records": args.expected_invalid,
    }
    if not args.skip_sqlite:
        expected["sqlite_rows"] = args.expected_rows
    mismatches = {
        key: {"expected": value, "observed": observed[key]}
        for key, value in expected.items()
        if observed[key] != value
    }
    if mismatches:
        raise ValueError("output counts do not match: " + json.dumps(mismatches))
    if token_violations and any(token_violations.values()):
        raise ValueError("found malformed anonymized values: " + json.dumps(token_violations))

    summary_path = args.output_dir / "airlock_summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"unreadable run summary: {exc}") from exc
        if summary.get("format") != "airlock-db-run-summary":
            raise ValueError("run summary has an unexpected format field")
        records = summary.get("records", {})
        summary_counts = (
            records.get("checked"),
            records.get("valid"),
            records.get("invalid"),
        )
        report_counts = (
            observed["records_checked"],
            observed["valid_records"],
            observed["invalid_records"],
        )
        if summary_counts != report_counts:
            raise ValueError(
                "run summary counts do not match the report: "
                f"summary={summary_counts} report={report_counts}"
            )
        if jsonl_rows is not None and summary.get("storage", {}).get("jsonl_rows") != jsonl_rows:
            raise ValueError("run summary JSONL row count does not match the file")
        observed["summary_verified"] = True
    elif args.allow_missing_summary:
        observed["summary_verified"] = False
    else:
        raise ValueError("output directory is missing airlock_summary.json")

    return {"status": "passed", **observed}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify AirLock DB SQLite and standalone report outputs."
    )
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--expected-checked", type=int, required=True)
    parser.add_argument("--expected-valid", type=int, required=True)
    parser.add_argument("--expected-invalid", type=int, required=True)
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument("--anonymized-field", action="append", default=[])
    parser.add_argument("--expect-type-mismatch-note", action="store_true")
    parser.add_argument("--skip-sqlite", action="store_true")
    parser.add_argument("--expected-jsonl-rows", type=int)
    parser.add_argument("--allow-missing-summary", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if not args.skip_sqlite and args.expected_rows is None:
        parser.error("--expected-rows is required unless --skip-sqlite is used")
    try:
        result = verify(args)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.evidence:
        args.evidence.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
