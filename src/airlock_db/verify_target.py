"""Reconcile a committed target against the records a run accepted.

``airlock-db verify --input PATH --output-dir DIR`` re-reads the same sources
with the same schema, rebuilds every stored row with the migration's own row
builder, and compares one digest per record against the committed SQLite
target, which is opened read-only. When the database is absent but
``target_migration.jsonl`` is present, the JSONL target is verified the same
way. Changed, missing, and unexpected rows are reported with bounded samples,
so a handoff can be checked without the repository scripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ._version import __version__

VERIFY_FORMAT = "airlock-db-target-verification"
_DEFAULT_LIMIT_SAMPLES = 10
_DEFAULT_TARGET_DATABASE = "target_migration.db"
_DEFAULT_JSONL_TARGET = "target_migration.jsonl"
_SUMMARY_FILENAME = "airlock_summary.json"
_REPORT_FILENAME = "airlock_verify.json"


def _single_character(value: str) -> str:
    if len(value) != 1:
        raise argparse.ArgumentTypeError("must be a single character")
    return value


def _row_digest(values: Sequence[Any]) -> str:
    """Digest one stored row as its SQLite-bound values."""

    document = json.dumps(
        list(values),
        ensure_ascii=True,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    uri = database_path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30.0)


def _fail(message: str, code: int = 1) -> int:
    print(f"airlock-db verify: {message}", file=sys.stderr)
    return code


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def verify_main(argv: Sequence[str] | None = None) -> int:
    from .anonymize import BUILTIN_ANONYMIZED_FIELDS
    from .constraints import ConstraintConfigError, ValidationConstraints
    from .dynamic_store import dynamic_row, quote_identifier
    from .fixed_width import FixedWidthConfigError, FixedWidthLayout
    from .ingestion import DatabaseSource, validate_source_query
    from .pipeline import iter_validated_records
    from .schema_config import DynamicSchema, SchemaConfigError
    from .schemas import FinancialTransaction
    from .sqlite_store import TRANSACTION_COLUMNS, TRANSACTION_TABLE, transaction_row

    parser = argparse.ArgumentParser(
        prog="airlock-db verify",
        description=(
            "Re-read a run's sources and reconcile every accepted record against "
            "the committed SQLite target with per-record digests."
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        action="append",
        default=None,
        metavar="PATH",
        help="an input file or directory from the original run; repeat for more paths",
    )
    parser.add_argument(
        "--source-db",
        action="append",
        default=None,
        metavar="PATH",
        help="a local SQLite database from the original run; repeat for more databases",
    )
    parser.add_argument(
        "--source-table",
        action="append",
        default=None,
        metavar="NAME",
        help="table read from --source-db; repeat in the order used by the run",
    )
    parser.add_argument(
        "--source-query",
        action="append",
        default=None,
        metavar="SQL",
        help="SELECT statement read from --source-db in the original run; repeat in the order used",
    )
    parser.add_argument(
        "--csv-delimiter",
        type=_single_character,
        metavar="CHAR",
        help="delimiter for CSV input; defaults to a comma, or a tab for .tsv files",
    )
    parser.add_argument(
        "--csv-encoding",
        metavar="NAME",
        help="text encoding for CSV input; defaults to utf-8-sig, which tolerates a BOM",
    )
    parser.add_argument(
        "--fixed-width",
        metavar="PATH",
        help="the JSON or YAML fixed-width layout used by the original run",
    )
    parser.add_argument(
        "-c",
        "--constraints",
        metavar="JSON",
        help="the constraints file used by the original run",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="the JSON or YAML schema config used by the original run",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="output",
        metavar="DIR",
        help=(
            "directory that holds target_migration.db; the check report is written "
            "as airlock_verify.json inside it (default: output)"
        ),
    )
    parser.add_argument(
        "--conflict-policy",
        choices=("skip", "replace"),
        default=None,
        help=(
            "duplicate policy used by the original run; defaults to the policy "
            "recorded in airlock_summary.json, or skip"
        ),
    )
    parser.add_argument(
        "--anonymized",
        action="store_true",
        help=(
            "exclude the anonymized fields from the digests; use when "
            "airlock_summary.json is missing"
        ),
    )
    args = parser.parse_args(argv)

    output_directory = Path(args.output_dir)
    database_path = output_directory / _DEFAULT_TARGET_DATABASE
    inputs = [Path(value) for value in (args.input or [])]
    source_tables = tuple(args.source_table or ())
    source_queries = tuple(args.source_query or ())
    if source_tables and not args.source_db:
        parser.error("--source-table requires --source-db")
    if source_queries:
        if source_tables:
            parser.error("--source-query cannot be combined with --source-table")
        if not args.source_db:
            parser.error("--source-query requires --source-db")
        if len(source_queries) != len(args.source_db):
            parser.error("pass exactly one --source-query per --source-db")
        try:
            source_queries = tuple(
                validate_source_query(value) for value in source_queries
            )
        except ValueError as exc:
            parser.error(f"--source-query: {exc}")
    database_sources = [
        DatabaseSource(
            Path(value),
            () if source_queries else source_tables,
            source_queries[index] if source_queries else None,
        )
        for index, value in enumerate(args.source_db or [])
    ]
    sources: list[Any] = [*inputs, *database_sources]
    if not sources:
        parser.error("at least one --input or --source-db is required")
    if args.config and args.constraints:
        return _fail(
            "--constraints cannot be combined with --config; pass the flags the "
            "original run used",
            2,
        )

    fixed_width_layout: FixedWidthLayout | None = None
    if args.fixed_width:
        try:
            fixed_width_layout = FixedWidthLayout.from_file(args.fixed_width)
        except FixedWidthConfigError as exc:
            return _fail(str(exc), 2)
    dynamic_schema: DynamicSchema | None = None
    if args.config:
        try:
            dynamic_schema = DynamicSchema.from_file(args.config)
        except SchemaConfigError as exc:
            return _fail(str(exc), 2)
    constraints: ValidationConstraints | None = None
    if args.constraints:
        try:
            constraints = ValidationConstraints.from_file(args.constraints)
        except ConstraintConfigError as exc:
            return _fail(str(exc), 2)

    summary: dict[str, Any] | None = None
    summary_path = output_directory / _SUMMARY_FILENAME
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                summary = loaded
        except (OSError, ValueError):
            summary = None
    recorded_policy = None
    recorded_anonymized = False
    if summary is not None:
        output = summary.get("output")
        if isinstance(output, dict) and output.get("conflict_policy") in {"skip", "replace"}:
            recorded_policy = output["conflict_policy"]
        recorded_anonymized = bool(summary.get("anonymized"))
    policy = args.conflict_policy or recorded_policy or "skip"
    if args.conflict_policy and recorded_policy and args.conflict_policy != recorded_policy:
        print(
            "airlock-db verify: warning: the run summary recorded conflict policy "
            f"{recorded_policy!r}; verifying with {args.conflict_policy!r}",
            file=sys.stderr,
        )
    anonymized = args.anonymized or recorded_anonymized

    if dynamic_schema is None:
        table_name = TRANSACTION_TABLE
        column_names: tuple[str, ...] = TRANSACTION_COLUMNS
        id_name = "transaction_id"
        excluded_fields = tuple(BUILTIN_ANONYMIZED_FIELDS) if anonymized else ()
    else:
        table_name = dynamic_schema.table_name
        column_names = tuple(field.name for field in dynamic_schema.fields)
        id_name = dynamic_schema.id_field
        excluded_fields = (
            tuple(
                name
                for name in column_names
                if name in dynamic_schema.anonymized_fields
            )
            if anonymized
            else ()
        )
    id_index = column_names.index(id_name)
    kept_indices = tuple(
        index
        for index, name in enumerate(column_names)
        if name not in excluded_fields
    )

    if not database_path.is_file():
        jsonl_path = output_directory / _DEFAULT_JSONL_TARGET
        if not jsonl_path.is_file():
            return _fail(
                f"no target database at {database_path} and no "
                f"{_DEFAULT_JSONL_TARGET} in {output_directory}; nothing to verify"
            )
        # JSONL-only target. The file holds every accepted record in input
        # order, serialized as the model's JSON dump. The recorded conflict
        # policy picks which duplicate id represents it, exactly as SQLite
        # storage does, and the digest covers the non-anonymized fields.
        ledger = sqlite3.connect(":memory:")
        try:
            ledger.execute("PRAGMA journal_mode = OFF")
            ledger.execute("PRAGMA synchronous = OFF")
            ledger.execute(
                "CREATE TABLE expected (id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
            )
            ledger.execute(
                "CREATE TABLE stored (id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
            )
            insert_sql = (
                "INSERT OR IGNORE INTO {table} (id, digest) VALUES (?, ?)"
                if policy == "skip"
                else "INSERT OR REPLACE INTO {table} (id, digest) VALUES (?, ?)"
            )
            insert_expected = insert_sql.format(table="expected")
            insert_stored = insert_sql.format(table="stored")
            accepted = 0
            try:
                for result in iter_validated_records(
                    sources,
                    constraints=constraints,
                    model=dynamic_schema.model if dynamic_schema else FinancialTransaction,
                    transforms=(
                        dynamic_schema.transforms if dynamic_schema is not None else None
                    ),
                    csv_delimiter=args.csv_delimiter,
                    csv_encoding=args.csv_encoding,
                    fixed_width=fixed_width_layout,
                ):
                    record = result.transaction
                    if record is None:
                        continue
                    accepted += 1
                    dumped = record.model_dump(mode="json")
                    values = [dumped[name] for name in column_names]
                    ledger.execute(
                        insert_expected,
                        (
                            str(values[id_index]),
                            _row_digest([values[index] for index in kept_indices]),
                        ),
                    )
            except KeyboardInterrupt:
                return _fail("interrupted before the check completed")

            expected_count = ledger.execute(
                "SELECT COUNT(*) FROM expected"
            ).fetchone()[0]
            collapsed = accepted - expected_count

            stored = 0
            try:
                stream = jsonl_path.open("r", encoding="utf-8")
            except OSError as exc:
                return _fail(f"cannot open {jsonl_path}: {exc}")
            with stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        loaded = json.loads(line)
                    except ValueError as exc:
                        return _fail(
                            f"cannot parse {jsonl_path} line {line_number}: {exc}"
                        )
                    if not isinstance(loaded, dict):
                        return _fail(
                            f"cannot parse {jsonl_path} line {line_number}: "
                            "each line must be a JSON object"
                        )
                    stored += 1
                    values = [loaded.get(name) for name in column_names]
                    ledger.execute(
                        insert_stored,
                        (
                            str(values[id_index]),
                            _row_digest([values[index] for index in kept_indices]),
                        ),
                    )
            stored_ids = ledger.execute("SELECT COUNT(*) FROM stored").fetchone()[0]
            collapsed_stored = stored - stored_ids

            matched = ledger.execute(
                "SELECT COUNT(*) FROM expected AS e JOIN stored AS s ON e.id = s.id "
                "WHERE e.digest = s.digest"
            ).fetchone()[0]
            changed = ledger.execute(
                "SELECT COUNT(*) FROM expected AS e JOIN stored AS s ON e.id = s.id "
                "WHERE e.digest <> s.digest"
            ).fetchone()[0]
            missing = ledger.execute(
                "SELECT COUNT(*) FROM expected WHERE id NOT IN (SELECT id FROM stored)"
            ).fetchone()[0]
            unexpected = ledger.execute(
                "SELECT COUNT(*) FROM stored WHERE id NOT IN (SELECT id FROM expected)"
            ).fetchone()[0]
            changed_samples = [
                {
                    "id": row[0],
                    "expected_digest": row[1][:16],
                    "stored_digest": row[2][:16],
                }
                for row in ledger.execute(
                    "SELECT e.id, e.digest, s.digest FROM expected AS e "
                    "JOIN stored AS s ON e.id = s.id "
                    "WHERE e.digest <> s.digest ORDER BY e.id LIMIT ?",
                    (_DEFAULT_LIMIT_SAMPLES,),
                )
            ]
            missing_samples = [
                {"id": row[0]}
                for row in ledger.execute(
                    "SELECT id FROM expected WHERE id NOT IN (SELECT id FROM stored) "
                    "ORDER BY id LIMIT ?",
                    (_DEFAULT_LIMIT_SAMPLES,),
                )
            ]
            unexpected_samples = [
                {"id": row[0]}
                for row in ledger.execute(
                    "SELECT id FROM stored WHERE id NOT IN (SELECT id FROM expected) "
                    "ORDER BY id LIMIT ?",
                    (_DEFAULT_LIMIT_SAMPLES,),
                )
            ]
        finally:
            ledger.close()

        consistent = changed == 0 and missing == 0 and unexpected == 0

        print(f"Verification target: {jsonl_path} (JSONL target)")
        print(
            f"Sources: {len(inputs)} file or directory path(s), "
            f"{len(database_sources)} SQLite database(s)"
        )
        if summary is not None:
            records = summary.get("records") or {}
            storage = summary.get("storage") or {}
            print(
                "Run summary: "
                f"{records.get('valid')} valid, {records.get('invalid')} invalid; "
                f"JSONL rows {storage.get('jsonl_rows')}"
            )
        print(
            f"Accepted records: {accepted} "
            f"(distinct ids {expected_count}, collapsed by {policy}: {collapsed})"
        )
        print(
            f"Target rows: {stored} "
            f"(distinct ids {stored_ids}, collapsed by {policy}: {collapsed_stored})"
        )
        if excluded_fields:
            print(f"Excluded from digests (anonymized): {', '.join(excluded_fields)}")
        print(
            f"Matched: {matched}; changed: {changed}; missing: {missing}; "
            f"unexpected: {unexpected}"
        )
        if changed_samples:
            print(f"Changed rows (showing first {len(changed_samples)} of {changed}):")
            for sample in changed_samples:
                print(
                    f"  - {sample['id']} (expected {sample['expected_digest']}, "
                    f"stored {sample['stored_digest']})"
                )
        if missing_samples:
            print(f"Missing rows (showing first {len(missing_samples)} of {missing}):")
            for sample in missing_samples:
                print(f"  - {sample['id']}")
        if unexpected_samples:
            print(
                f"Unexpected stored rows (showing first {len(unexpected_samples)} "
                f"of {unexpected}):"
            )
            for sample in unexpected_samples:
                print(f"  - {sample['id']}")
        if consistent:
            print("Verification: consistent")
        else:
            print(
                f"Verification: mismatch (changed {changed}, missing {missing}, "
                f"unexpected {unexpected})"
            )
            if changed and not anonymized:
                print(
                    "Hint: if the original run used --anonymize and no summary exists, "
                    "pass --anonymized"
                )

        report = {
            "format": VERIFY_FORMAT,
            "version": 1,
            "airlock_version": __version__,
            "generated_at": _timestamp(),
            "status": "consistent" if consistent else "mismatch",
            "target": {"jsonl": str(jsonl_path)},
            "conflict_policy": policy,
            "anonymized": anonymized,
            "excluded_fields": list(excluded_fields),
            "records": {
                "accepted": accepted,
                "distinct_ids": expected_count,
                "collapsed": collapsed,
                "stored": stored,
                "stored_ids": stored_ids,
                "collapsed_stored": collapsed_stored,
                "matched": matched,
                "changed": changed,
                "missing": missing,
                "unexpected": unexpected,
            },
            "samples": {
                "changed": changed_samples,
                "missing": missing_samples,
                "unexpected": unexpected_samples,
            },
        }
        report_path = output_directory / _REPORT_FILENAME
        try:
            report_path.write_text(
                json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(
                f"airlock-db verify: warning: could not write {report_path}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        else:
            print(f"Verification report: {report_path}")
        return 0 if consistent else 1

    select_columns = ", ".join(quote_identifier(name) for name in column_names)
    quoted_table = quote_identifier(table_name)
    try:
        connection = _open_read_only(database_path)
    except sqlite3.Error as exc:
        return _fail(f"cannot open {database_path}: {type(exc).__name__}: {exc}")
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if exists is None:
            if dynamic_schema is None:
                detail = "the directory was not produced by the built-in schema"
            else:
                detail = "the directory was produced with a different schema config"
            return _fail(f"table {table_name!r} was not found; {detail}")
        stored_rows = connection.execute(
            f"SELECT {select_columns} FROM {quoted_table}"
        )
        ledger = sqlite3.connect(":memory:")
        try:
            ledger.execute("PRAGMA journal_mode = OFF")
            ledger.execute("PRAGMA synchronous = OFF")
            ledger.execute(
                "CREATE TABLE expected (id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
            )
            insert_expected = (
                "INSERT OR IGNORE INTO expected (id, digest) VALUES (?, ?)"
                if policy == "skip"
                else "INSERT OR REPLACE INTO expected (id, digest) VALUES (?, ?)"
            )
            row_builder = (
                (lambda record: dynamic_row(dynamic_schema, record))
                if dynamic_schema is not None
                else transaction_row
            )
            accepted = 0
            try:
                for result in iter_validated_records(
                    sources,
                    constraints=constraints,
                    model=dynamic_schema.model if dynamic_schema else FinancialTransaction,
                    transforms=(
                        dynamic_schema.transforms if dynamic_schema is not None else None
                    ),
                    csv_delimiter=args.csv_delimiter,
                    csv_encoding=args.csv_encoding,
                    fixed_width=fixed_width_layout,
                ):
                    record = result.transaction
                    if record is None:
                        continue
                    accepted += 1
                    row = row_builder(record)
                    ledger.execute(
                        insert_expected,
                        (
                            str(row[id_index]),
                            _row_digest([row[index] for index in kept_indices]),
                        ),
                    )
            except KeyboardInterrupt:
                return _fail("interrupted before the check completed")

            expected_count = ledger.execute(
                "SELECT COUNT(*) FROM expected"
            ).fetchone()[0]
            collapsed = accepted - expected_count

            matched = 0
            changed = 0
            unexpected = 0
            stored = 0
            changed_samples: list[dict[str, str]] = []
            unexpected_samples: list[dict[str, str]] = []
            try:
                for stored_row in stored_rows:
                    stored += 1
                    row_id = str(stored_row[id_index])
                    digest = _row_digest(
                        [stored_row[index] for index in kept_indices]
                    )
                    expected = ledger.execute(
                        "SELECT digest FROM expected WHERE id = ?", (row_id,)
                    ).fetchone()
                    if expected is None:
                        unexpected += 1
                        if len(unexpected_samples) < _DEFAULT_LIMIT_SAMPLES:
                            unexpected_samples.append({"id": row_id})
                        continue
                    if expected[0] == digest:
                        matched += 1
                    else:
                        changed += 1
                        if len(changed_samples) < _DEFAULT_LIMIT_SAMPLES:
                            changed_samples.append(
                                {
                                    "id": row_id,
                                    "expected_digest": expected[0][:16],
                                    "stored_digest": digest[:16],
                                }
                            )
                    ledger.execute("DELETE FROM expected WHERE id = ?", (row_id,))
            except KeyboardInterrupt:
                return _fail("interrupted before the check completed")

            missing = ledger.execute("SELECT COUNT(*) FROM expected").fetchone()[0]
            missing_samples = [
                {"id": row[0]}
                for row in ledger.execute(
                    "SELECT id FROM expected ORDER BY id LIMIT ?",
                    (_DEFAULT_LIMIT_SAMPLES,),
                )
            ]
        finally:
            ledger.close()
    finally:
        connection.close()

    consistent = changed == 0 and missing == 0 and unexpected == 0

    print(f"Verification target: {database_path} (table {table_name})")
    print(
        f"Sources: {len(inputs)} file or directory path(s), "
        f"{len(database_sources)} SQLite database(s)"
    )
    if summary is not None:
        records = summary.get("records") or {}
        storage = summary.get("storage") or {}
        print(
            "Run summary: "
            f"{records.get('valid')} valid, {records.get('invalid')} invalid; "
            f"storage inserted {storage.get('inserted')}, updated {storage.get('updated')}"
        )
    print(
        f"Accepted records: {accepted} "
        f"(distinct ids {expected_count}, collapsed by {policy}: {collapsed})"
    )
    print(f"Stored rows: {stored}")
    if excluded_fields:
        print(f"Excluded from digests (anonymized): {', '.join(excluded_fields)}")
    print(
        f"Matched: {matched}; changed: {changed}; missing: {missing}; "
        f"unexpected: {unexpected}"
    )
    if changed_samples:
        print(f"Changed rows (showing first {len(changed_samples)} of {changed}):")
        for sample in changed_samples:
            print(
                f"  - {sample['id']} (expected {sample['expected_digest']}, "
                f"stored {sample['stored_digest']})"
            )
    if missing_samples:
        print(f"Missing rows (showing first {len(missing_samples)} of {missing}):")
        for sample in missing_samples:
            print(f"  - {sample['id']}")
    if unexpected_samples:
        print(
            f"Unexpected stored rows (showing first {len(unexpected_samples)} "
            f"of {unexpected}):"
        )
        for sample in unexpected_samples:
            print(f"  - {sample['id']}")
    if consistent:
        print("Verification: consistent")
    else:
        print(
            f"Verification: mismatch (changed {changed}, missing {missing}, "
            f"unexpected {unexpected})"
        )
        if changed and not anonymized:
            print(
                "Hint: if the original run used --anonymize and no summary exists, "
                "pass --anonymized"
            )

    report = {
        "format": VERIFY_FORMAT,
        "version": 1,
        "airlock_version": __version__,
        "generated_at": _timestamp(),
        "status": "consistent" if consistent else "mismatch",
        "target": {"database": str(database_path), "table": table_name},
        "conflict_policy": policy,
        "anonymized": anonymized,
        "excluded_fields": list(excluded_fields),
        "records": {
            "accepted": accepted,
            "distinct_ids": expected_count,
            "collapsed": collapsed,
            "stored": stored,
            "matched": matched,
            "changed": changed,
            "missing": missing,
            "unexpected": unexpected,
        },
        "samples": {
            "changed": changed_samples,
            "missing": missing_samples,
            "unexpected": unexpected_samples,
        },
    }
    report_path = output_directory / _REPORT_FILENAME
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"airlock-db verify: warning: could not write {report_path}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    else:
        print(f"Verification report: {report_path}")
    return 0 if consistent else 1


if __name__ == "__main__":
    sys.exit(verify_main())
