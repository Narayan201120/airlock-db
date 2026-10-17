"""Command-line entry point for local AirLock DB migrations."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .anonymize import RunAnonymizer
from .constraints import ConstraintConfigError, ValidationConstraints
from .dynamic_store import DynamicSQLiteTargetStore
from .ingestion import DatabaseSource
from .jsonl_target import JsonlTargetWriter
from .pipeline import (
    DriftCategory,
    MigrationRecordResult,
    ValidationIssue,
    ValidationMetrics,
    iter_validated_records,
)
from .report import write_html_report
from .report_refresh import refresh_main
from .repair import repair_main
from .repair_log import RepairLogError, RepairLogWriter, constraints_fingerprint
from .schema_config import DynamicSchema, SchemaConfigError
from .schemas import FinancialTransaction
from .sqlite_store import SQLiteTargetStore, read_migration_progress
from .summary import build_run_summary, write_run_summary


logger = logging.getLogger("airlock_db")
_DEFAULT_BATCH_SIZE = 500
_DEFAULT_REPORT_ISSUES = 1000
_MAX_BATCH_SIZE = 10_000
_MAX_REPORT_ISSUES = 10_000
_MAX_REPORT_INPUT_CHARS = 512
_MAX_REPORT_TEXT_CHARS = 512
_BUILTIN_SCHEMA_FINGERPRINT = "financial-transaction-v1"
_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))


def _integer_in_range(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def _single_character(value: str) -> str:
    if len(value) != 1:
        raise argparse.ArgumentTypeError("must be a single character")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airlock-db",
        description="Validate local JSON, JSONL, NDJSON, XML, CSV, or TSV records and migrate valid records to SQLite.",
        epilog=(
            "Use `airlock-db repair --error-log PATH` to review rejected records "
            "interactively, and `airlock-db report --output-dir DIR` to rebuild a "
            "report after repair."
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        action="append",
        default=None,
        metavar="PATH",
        help="input file or directory (JSON, JSONL, NDJSON, XML, CSV, TSV); repeat the option to scan multiple paths",
    )
    parser.add_argument(
        "--source-db",
        action="append",
        default=None,
        metavar="PATH",
        help="local SQLite database read as an input source; repeat for more databases",
    )
    parser.add_argument(
        "--source-table",
        action="append",
        default=None,
        metavar="NAME",
        help="table to read from each --source-db; repeat for several tables; defaults to the only table found",
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
        "-c",
        "--constraints",
        metavar="JSON",
        help="optional JSON file with business rules for the built-in financial schema",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="JSON or YAML file defining a dynamic validation schema",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="output",
        metavar="DIR",
        help="directory for target_migration.db, target_migration.jsonl, and airlock_report.html (default: output)",
    )
    parser.add_argument(
        "--output-format",
        choices=("sqlite", "jsonl", "both"),
        default="sqlite",
        help="target output: SQLite database (default), JSONL migration file, or both",
    )
    parser.add_argument(
        "--commit-every",
        type=_integer_in_range(1, 100_000_000),
        metavar="N",
        help="commit accepted records and save progress every N records (enables resumable runs)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip writing records already committed by an earlier --commit-every run",
    )
    parser.add_argument(
        "--error-log",
        nargs="?",
        const="airlock_errors.jsonl",
        default=None,
        metavar="PATH",
        help=(
            "write rejected rows for interactive repair; raw rejected values may contain PII; "
            "relative PATHs are inside the output directory; with no PATH, use airlock_errors.jsonl"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=_integer_in_range(1, _MAX_BATCH_SIZE),
        default=_DEFAULT_BATCH_SIZE,
        help=f"SQLite write batch size (default: {_DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--max-report-issues",
        type=_integer_in_range(0, _MAX_REPORT_ISSUES),
        default=_DEFAULT_REPORT_ISSUES,
        help=f"maximum issue entries embedded in the HTML report (default: {_DEFAULT_REPORT_ISSUES})",
    )
    parser.add_argument(
        "--conflict-policy",
        choices=("skip", "replace"),
        default="skip",
        help="how SQLite handles an existing record key (default: skip)",
    )
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help="pseudonymize sensitive fields, redact diagnostics, and omit raw rejected values from an optional repair log",
    )
    parser.add_argument("--verbose", action="store_true", help="include informational log messages")
    parser.add_argument("--version", action="version", version=f"airlock-db {__version__}")
    return parser


_REPORT_SCHEMA_FIELDS = frozenset(
    {
        "transaction_id",
        "account_id",
        "posted_at",
        "amount",
        "currency",
        "transaction_type",
        "status",
        "counterparty",
        "description",
    }
)


def _bounded_input_preview(value: object) -> str:
    """Encode only the JSON prefix needed by the bounded report preview."""

    is_small_flat_value = isinstance(value, _JSON_SCALAR_TYPES)
    if isinstance(value, dict) and len(value) <= 8:
        is_small_flat_value = all(
            isinstance(key, str) and isinstance(item, _JSON_SCALAR_TYPES)
            for key, item in value.items()
        )
    elif isinstance(value, list) and len(value) <= 8:
        is_small_flat_value = all(
            isinstance(item, _JSON_SCALAR_TYPES) for item in value
        )

    if is_small_flat_value:
        try:
            rendered = json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                default=repr,
            )
        except Exception:
            return f"<{type(value).__name__} input preview unavailable>"
        if len(rendered) > _MAX_REPORT_INPUT_CHARS:
            return rendered[:_MAX_REPORT_INPUT_CHARS] + "... [truncated]"
        return rendered

    encoder = json.JSONEncoder(
        ensure_ascii=True,
        separators=(",", ":"),
        default=repr,
    )
    chunks: list[str] = []
    rendered_length = 0
    try:
        for chunk in encoder.iterencode(value):
            remaining = _MAX_REPORT_INPUT_CHARS + 1 - rendered_length
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                rendered_length += remaining
                break
            chunks.append(chunk)
            rendered_length += len(chunk)
            if rendered_length > _MAX_REPORT_INPUT_CHARS:
                break
        rendered = "".join(chunks)
    except Exception:
        return f"<{type(value).__name__} input preview unavailable>"

    if len(rendered) > _MAX_REPORT_INPUT_CHARS:
        return rendered[:_MAX_REPORT_INPUT_CHARS] + "... [truncated]"
    return rendered


def _issue_rows(
    result: MigrationRecordResult,
    limit: int,
    anonymizer: RunAnonymizer | None = None,
    schema_fields: frozenset[str] = _REPORT_SCHEMA_FIELDS,
) -> list[dict[str, object]]:
    def bounded_text(value: object, maximum: int = _MAX_REPORT_TEXT_CHARS) -> str:
        rendered = str(value)
        if len(rendered) > maximum:
            return rendered[:maximum] + "... [truncated]"
        return rendered

    rows: list[dict[str, object]] = []
    for issue in result.issues[:limit]:
        if anonymizer is None:
            source = bounded_text(result.source)
            location = [bounded_text(part, 100) for part in issue.location[:16]]
            message = bounded_text(issue.message)
            input_value = (
                _bounded_input_preview(issue.input_value)
                if issue.input_value is not None
                else None
            )
            input_is_serialized = issue.input_value is not None
        else:
            source = anonymizer.source_label(result.source)
            location = [
                part
                if isinstance(part, int)
                else part if part in schema_fields else "<unknown field>"
                for part in issue.location[:16]
            ]
            message = "Validation details redacted in anonymize mode."
            if issue.category is DriftCategory.TYPE_MISMATCH:
                message += " No automatic type conversion was applied."
            input_value = "[REDACTED]" if issue.input_value is not None else None
            input_is_serialized = False

        rows.append(
            {
                "source": source,
                "record_number": result.record_number,
                "code": bounded_text(issue.code, 100),
                "category": issue.category.value,
                "location": location,
                "message": message,
                "input": input_value,
                "input_is_serialized": input_is_serialized,
            }
        )
    return rows


def _add_bounded_issues(
    destination: list[dict[str, object]],
    result: MigrationRecordResult,
    maximum: int,
    anonymizer: RunAnonymizer | None = None,
    schema_fields: frozenset[str] = _REPORT_SCHEMA_FIELDS,
) -> None:
    remaining = max(0, maximum - len(destination))
    if remaining:
        destination.extend(_issue_rows(result, remaining, anonymizer, schema_fields))


def _storage_error_message(error: Exception, anonymizer: RunAnonymizer | None) -> str:
    if anonymizer is not None:
        return "SQLite operation failed; details redacted in anonymize mode."
    return f"{type(error).__name__}: {error}"


def _log_storage_error(
    message: str,
    error: Exception,
    anonymizer: RunAnonymizer | None,
) -> None:
    if anonymizer is not None:
        logger.error("%s (%s)", message, type(error).__name__)
    else:
        logger.exception("%s", message)


def _pipeline_failure(source: str, error: Exception) -> MigrationRecordResult:
    return MigrationRecordResult(
        source=source,
        record_number=None,
        issues=(
            ValidationIssue(
                code="pipeline_error",
                category=DriftCategory.SOURCE_ERROR,
                message=f"Validation stopped unexpectedly: {type(error).__name__}: {error}",
            ),
        ),
        source_incomplete=True,
    )


def _interruption_result() -> MigrationRecordResult:
    return MigrationRecordResult(
        source="<input pipeline>",
        record_number=None,
        issues=(
            ValidationIssue(
                code="interrupted",
                category=DriftCategory.SOURCE_ERROR,
                message="Migration was interrupted before all input records were processed.",
            ),
        ),
        source_incomplete=True,
    )


def _close_store(
    store: SQLiteTargetStore | DynamicSQLiteTargetStore | None,
    anonymizer: RunAnonymizer | None = None,
) -> None:
    if store is None:
        return
    try:
        store.close()
    except sqlite3.Error as exc:
        _log_storage_error("Could not close the SQLite target cleanly", exc, anonymizer)


def _normalized_path(path: str | Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def _same_path(left: str | Path, right: str | Path) -> bool:
    if _normalized_path(left) == _normalized_path(right):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _path_is_within(candidate: str | Path, parent: str | Path) -> bool:
    candidate_path = _normalized_path(candidate)
    parent_path = _normalized_path(parent)
    try:
        return os.path.commonpath((candidate_path, parent_path)) == parent_path
    except ValueError:
        return False


def _is_repair_log_file(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as stream:
            header = json.loads(stream.readline(8192))
        return (
            isinstance(header, dict)
            and header.get("kind") == "header"
            and header.get("format") == "airlock-db-repair-log"
        )
    except (OSError, UnicodeError, ValueError, RecursionError):
        return False


def _source_display_label(source: Path | DatabaseSource) -> str:
    if isinstance(source, DatabaseSource):
        if source.tables:
            return f"{source.path} ({', '.join(source.tables)})"
        return str(source.path)
    return str(source)


def run_migration(args: argparse.Namespace) -> int:
    inputs = [Path(value) for value in (args.input or [])]
    source_tables = tuple(getattr(args, "source_table", None) or ())
    database_sources = [
        DatabaseSource(Path(value), source_tables)
        for value in (args.source_db or [])
    ]
    sources: list[Path | DatabaseSource] = [*inputs, *database_sources]
    output_directory = Path(args.output_dir)
    database_path = output_directory / "target_migration.db"
    jsonl_path = output_directory / "target_migration.jsonl"
    report_path = output_directory / "airlock_report.html"
    summary_path = output_directory / "airlock_summary.json"
    output_format = args.output_format
    use_sqlite = output_format in {"sqlite", "both"}
    use_jsonl = output_format in {"jsonl", "both"}
    anonymizer = RunAnonymizer() if getattr(args, "anonymize", False) else None
    for database_source in database_sources:
        if _same_path(database_source.path, database_path):
            print(
                "airlock-db: --source-db cannot be the migration target database",
                file=sys.stderr,
            )
            return 2
    commit_every = getattr(args, "commit_every", None)
    resume = getattr(args, "resume", False)
    if (commit_every is not None or resume) and not use_sqlite:
        print(
            "airlock-db: --commit-every and --resume require the SQLite target; "
            "use --output-format sqlite or both",
            file=sys.stderr,
        )
        return 2
    if resume and anonymizer is not None:
        print(
            "airlock-db: --resume is not supported with --anonymize; "
            "pseudonyms are run-scoped",
            file=sys.stderr,
        )
        return 2
    if resume and getattr(args, "error_log", None) is not None:
        print(
            "airlock-db: warning: --resume with --error-log writes a repair log "
            "for this run's records only",
            file=sys.stderr,
        )
    dynamic_schema: DynamicSchema | None = None
    if getattr(args, "config", None):
        try:
            dynamic_schema = DynamicSchema.from_file(args.config)
        except SchemaConfigError as exc:
            print(f"airlock-db: {exc}", file=sys.stderr)
            return 2
        if args.constraints:
            print(
                "airlock-db: --constraints cannot be combined with --config; "
                "put these rules in the schema config",
                file=sys.stderr,
            )
            return 2
        if anonymizer is not None and not dynamic_schema.anonymized_fields:
            print(
                "airlock-db: --anonymize with --config requires at least one field "
                "declared with anonymize: true",
                file=sys.stderr,
            )
            return 2
    schema_fields = dynamic_schema.field_names if dynamic_schema else _REPORT_SCHEMA_FIELDS
    record_model = dynamic_schema.model if dynamic_schema else FinancialTransaction
    progress_scope: str | None = None
    if commit_every is not None or resume:
        progress_scope = hashlib.sha256(
            "|".join(
                (
                    "airlock-progress-v1",
                    dynamic_schema.fingerprint
                    if dynamic_schema
                    else _BUILTIN_SCHEMA_FINGERPRINT,
                    args.conflict_policy,
                    *sorted(str(source) for source in sources),
                )
            ).encode("utf-8")
        ).hexdigest()
    logger.info("Starting migration with %d source(s)", len(sources))
    if use_sqlite:
        logger.info(
            "SQLite target %s; batch size %d; duplicate policy %s",
            database_path.name if anonymizer else database_path,
            args.batch_size,
            args.conflict_policy,
        )
    if use_jsonl:
        logger.info(
            "JSONL target %s",
            jsonl_path.name if anonymizer else jsonl_path,
        )

    constraints: ValidationConstraints | None = None
    if args.constraints:
        try:
            constraints = ValidationConstraints.from_file(args.constraints)
        except ConstraintConfigError as exc:
            print(f"airlock-db: {exc}", file=sys.stderr)
            return 2

    try:
        output_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        display_path = output_directory.name if anonymizer else output_directory
        details = type(exc).__name__ if anonymizer else str(exc)
        print(f"airlock-db: cannot create output directory {display_path}: {details}", file=sys.stderr)
        return 2

    repair_log_path: Path | None = None
    repair_log: RepairLogWriter | None = None
    requested_error_log = getattr(args, "error_log", None)
    if requested_error_log is not None:
        configured_log_path = Path(requested_error_log)
        repair_log_path = (
            configured_log_path
            if configured_log_path.is_absolute()
            else output_directory / configured_log_path
        )
        reserved_paths = [database_path, report_path, summary_path]
        if use_jsonl:
            reserved_paths.append(jsonl_path)
        if args.config:
            reserved_paths.append(Path(args.config))
        if args.constraints:
            reserved_paths.append(Path(args.constraints))
        if any(_same_path(repair_log_path, reserved) for reserved in reserved_paths):
            print(
                "airlock-db: --error-log cannot overwrite an input configuration or migration output",
                file=sys.stderr,
            )
            return 2
        for source in inputs:
            same_source = _same_path(source, repair_log_path)
            if same_source:
                print(f"airlock-db: --error-log would overwrite an input file: {source}", file=sys.stderr)
                return 2
            if source.is_dir() and _path_is_within(repair_log_path, source) and repair_log_path.exists():
                if not repair_log_path.is_file() or not _is_repair_log_file(repair_log_path):
                    print(
                        f"airlock-db: --error-log would overwrite a file inside the input directory: {repair_log_path}",
                        file=sys.stderr,
                    )
                    return 2
        for database_source in database_sources:
            if _same_path(database_source.path, repair_log_path):
                print(
                    f"airlock-db: --error-log would overwrite an input database: {database_source.path}",
                    file=sys.stderr,
                )
                return 2

        repair_log = RepairLogWriter(
            repair_log_path,
            schema_name=dynamic_schema.model_name if dynamic_schema else FinancialTransaction.__name__,
            schema_fingerprint=dynamic_schema.fingerprint if dynamic_schema else _BUILTIN_SCHEMA_FINGERPRINT,
            constraints_hash=constraints_fingerprint(constraints),
            anonymized=anonymizer is not None,
            schema_fields=schema_fields,
            source_labeler=(anonymizer.source_label if anonymizer else None),
        )
        try:
            repair_log.open()
        except (OSError, RepairLogError, TypeError, ValueError) as exc:
            log_display = repair_log_path.name if anonymizer else repair_log_path
            details = type(exc).__name__ if anonymizer else str(exc)
            print(f"airlock-db: cannot create repair log {log_display}: {details}", file=sys.stderr)
            return 2
    repair_log_error: str | None = None

    metrics = ValidationMetrics()
    report_issues: list[dict[str, object]] = []
    storage_error: str | None = None
    store: SQLiteTargetStore | DynamicSQLiteTargetStore | None = None
    storage_summary: dict[str, object] = {
        "status": "failed" if use_sqlite else "not used (JSONL output)",
        "database": "target_migration.db" if anonymizer else str(database_path),
        "table": (
            dynamic_schema.table_name if dynamic_schema else "transactions"
        ),
        "identity_label": dynamic_schema.id_field if dynamic_schema else "transaction ID",
        "schema_name": dynamic_schema.model_name if dynamic_schema else "financial transaction",
        "inserted": 0,
        "duplicate_transaction_ids": 0,
        "updated": 0,
        "conflicting_transaction_ids": 0,
        "conflict_policy": args.conflict_policy,
    }

    jsonl_writer: JsonlTargetWriter | None = None
    if use_jsonl:
        try:
            jsonl_writer = JsonlTargetWriter(jsonl_path)
            jsonl_writer.__enter__()
        except OSError as exc:
            display_path = jsonl_path.name if anonymizer else jsonl_path
            details = type(exc).__name__ if anonymizer else str(exc)
            print(
                f"airlock-db: cannot create JSONL target {display_path}: {details}",
                file=sys.stderr,
            )
            return 2

    resumed_records = 0
    stored_since_checkpoint = 0
    last_processed: dict[str, tuple[int, int, float]] = {}
    stat_cache: dict[str, tuple[int, float] | None] = {}
    resume_progress: dict[str, tuple[int, int, float]] = {}

    def _source_identity(source_text: str) -> tuple[int, float] | None:
        if source_text not in stat_cache:
            try:
                info = os.stat(source_text)
                stat_cache[source_text] = (info.st_size, info.st_mtime)
            except OSError:
                stat_cache[source_text] = None
        return stat_cache[source_text]

    def _resume_covers(source_text: str, record_number: int) -> bool:
        entry = resume_progress.get(source_text)
        if entry is None or record_number > entry[0]:
            return False
        identity = _source_identity(source_text)
        return bool(
            identity is not None
            and identity[0] == entry[1]
            and abs(identity[1] - entry[2]) < 1e-6
        )

    pipeline_failed = False
    interrupted = False
    try:
        if use_sqlite and dynamic_schema is not None:
            store = DynamicSQLiteTargetStore(
                database_path,
                dynamic_schema,
                batch_size=args.batch_size,
                conflict_policy=args.conflict_policy,
            )
        elif use_sqlite:
            store = SQLiteTargetStore(
                database_path,
                batch_size=args.batch_size,
                conflict_policy=args.conflict_policy,
            )
        if store is not None:
            store.__enter__()
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        storage_error = _storage_error_message(exc, anonymizer)
        if anonymizer is None:
            logger.exception("Could not initialize SQLite target %s", database_path)
        else:
            logger.error("Could not initialize SQLite target (%s)", type(exc).__name__)
        _close_store(store, anonymizer)
        store = None
    except KeyboardInterrupt:
        interrupted = True
        pipeline_failed = True
        storage_error = "Migration was interrupted while initializing SQLite."
        logger.warning("Migration interrupted during SQLite initialization")
        _close_store(store, anonymizer)
        store = None
        failure = _interruption_result()
        metrics.observe(failure)
        _add_bounded_issues(
            report_issues, failure, args.max_report_issues, anonymizer, schema_fields
        )

    if not interrupted:
        if resume and store is not None and progress_scope is not None:
            try:
                resume_progress = read_migration_progress(database_path, progress_scope)
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Could not read saved migration progress: %s", exc)
            if not resume_progress:
                logger.warning(
                    "No saved progress was found for this run; processing all records"
                )
        try:
            for result in iter_validated_records(
                sources,
                metrics=metrics,
                constraints=constraints,
                source_labeler=(anonymizer.source_label if anonymizer else None),
                model=record_model,
                excluded_paths=(repair_log_path,) if repair_log_path is not None else (),
                csv_delimiter=getattr(args, "csv_delimiter", None),
                csv_encoding=getattr(args, "csv_encoding", None),
            ):
                resumed_here = (
                    resume
                    and result.record_number is not None
                    and _resume_covers(result.source, result.record_number)
                )
                if resumed_here:
                    resumed_records += 1
                if (
                    result.record_number is not None
                    and progress_scope is not None
                    and not resumed_here
                ):
                    previous = last_processed.get(result.source)
                    if previous is None or result.record_number > previous[0]:
                        identity = _source_identity(result.source) or (0, 0.0)
                        last_processed[result.source] = (
                            result.record_number,
                            identity[0],
                            identity[1],
                        )
                if not result.is_valid:
                    _add_bounded_issues(
                        report_issues,
                        result,
                        args.max_report_issues,
                        anonymizer,
                        schema_fields,
                    )
                    if (
                        repair_log is not None
                        and repair_log_error is None
                        and not resumed_here
                    ):
                        try:
                            repair_log.write_result(result)
                        except (OSError, RepairLogError, TypeError, ValueError, RecursionError) as exc:
                            repair_log_error = (
                                "repair log could not be completed; details redacted in anonymize mode"
                                if anonymizer is not None
                                else f"{type(exc).__name__}: {exc}"
                            )
                            if anonymizer is None:
                                logger.exception("Could not write repair log %s", repair_log_path)
                            else:
                                logger.error("Could not write repair log (%s)", type(exc).__name__)
                            repair_log.abort()
                if (
                    result.transaction is not None
                    and not resumed_here
                    and (store is not None or jsonl_writer is not None)
                ):
                    try:
                        if anonymizer is not None and dynamic_schema is not None:
                            record = anonymizer.configured_record(
                                result.transaction, dynamic_schema.anonymized_fields
                            )
                        elif anonymizer is not None:
                            record = anonymizer.transaction(result.transaction)
                        else:
                            record = result.transaction
                        if store is not None:
                            store.add(record)
                            stored_since_checkpoint += 1
                        if jsonl_writer is not None:
                            jsonl_writer.add(record)
                        if (
                            store is not None
                            and commit_every is not None
                            and progress_scope is not None
                            and stored_since_checkpoint >= commit_every
                        ):
                            for progress_source, (
                                record_no,
                                source_size,
                                source_mtime,
                            ) in last_processed.items():
                                store.checkpoint(
                                    progress_scope,
                                    progress_source,
                                    record_no,
                                    int(source_size),
                                    float(source_mtime),
                                )
                            stored_since_checkpoint = 0
                    except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
                        storage_error = _storage_error_message(exc, anonymizer)
                        _log_storage_error(
                            "Target write failed; pending output will be rolled back",
                            exc,
                            anonymizer,
                        )
                        try:
                            if store is not None:
                                store.rollback()
                        except sqlite3.Error as rollback_error:
                            _log_storage_error(
                                "SQLite rollback also failed", rollback_error, anonymizer
                            )
                        _close_store(store, anonymizer)
                        store = None
                        if jsonl_writer is not None:
                            try:
                                jsonl_writer.rollback()
                            except OSError:
                                pass
                            jsonl_writer = None
        except KeyboardInterrupt:
            interrupted = True
            pipeline_failed = True
            logger.warning("Migration interrupted; rolling back uncommitted SQLite rows")
            failure = _interruption_result()
            metrics.observe(failure)
            _add_bounded_issues(
                report_issues, failure, args.max_report_issues, anonymizer, schema_fields
            )
            if store is not None:
                try:
                    store.rollback()
                except sqlite3.Error as rollback_error:
                    _log_storage_error(
                        "SQLite rollback failed after migration interruption",
                        rollback_error,
                        anonymizer,
                    )
                _close_store(store, anonymizer)
                store = None
            storage_error = "Migration interrupted before all input records were processed."
        except Exception as exc:
            pipeline_failed = True
            if anonymizer is None:
                logger.exception("Unexpected failure while iterating validation results")
            else:
                logger.error(
                    "Unexpected validation pipeline failure (%s)", type(exc).__name__
                )
            failure = _pipeline_failure("<input pipeline>", exc)
            metrics.observe(failure)
            _add_bounded_issues(
                report_issues, failure, args.max_report_issues, anonymizer, schema_fields
            )
            if store is not None:
                try:
                    store.rollback()
                except sqlite3.Error as rollback_error:
                    _log_storage_error(
                        "SQLite rollback failed after an input pipeline error",
                        rollback_error,
                        anonymizer,
                    )
                _close_store(store, anonymizer)
                store = None
            storage_error = storage_error or "Validation stopped before all input records were processed."

    if repair_log is not None and repair_log_error is None:
        try:
            repair_log.finish(
                complete=(
                    not interrupted
                    and not pipeline_failed
                    and metrics.file_errors == 0
                    and metrics.incomplete_files == 0
                )
            )
        except (OSError, RepairLogError, ValueError, RecursionError) as exc:
            repair_log_error = (
                "repair log could not be completed; details redacted in anonymize mode"
                if anonymizer is not None
                else f"{type(exc).__name__}: {exc}"
            )
            repair_log.abort()
            if anonymizer is None:
                logger.exception("Could not finish repair log %s", repair_log_path)
            else:
                logger.error("Could not finish repair log (%s)", type(exc).__name__)

    if store is not None:
        try:
            if progress_scope is not None and last_processed:
                for progress_source, (
                    record_no,
                    source_size,
                    source_mtime,
                ) in last_processed.items():
                    store.checkpoint(
                        progress_scope,
                        progress_source,
                        record_no,
                        int(source_size),
                        float(source_mtime),
                    )
            summary = store.commit()
            storage_summary.update(summary.to_dict())
            storage_summary["status"] = "committed"
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            storage_error = _storage_error_message(exc, anonymizer)
            if anonymizer is None:
                _log_storage_error(
                    "Could not commit SQLite target", exc, anonymizer
                )
            else:
                logger.error("Could not commit SQLite target (%s)", type(exc).__name__)
            try:
                store.rollback()
            except sqlite3.Error as rollback_error:
                _log_storage_error(
                    "SQLite rollback also failed", rollback_error, anonymizer
                )
        except KeyboardInterrupt:
            interrupted = True
            pipeline_failed = True
            logger.warning("Migration interrupted while committing SQLite output")
            failure = _interruption_result()
            metrics.observe(failure)
            _add_bounded_issues(
                report_issues, failure, args.max_report_issues, anonymizer, schema_fields
            )
            storage_error = "Migration interrupted during SQLite commit; inspect the target before retrying."
            try:
                store.rollback()
            except sqlite3.Error as rollback_error:
                _log_storage_error(
                    "SQLite rollback also failed after commit interruption",
                    rollback_error,
                    anonymizer,
                )
        finally:
            _close_store(store, anonymizer)

    if jsonl_writer is not None:
        if not interrupted and not pipeline_failed and storage_error is None:
            try:
                jsonl_summary = jsonl_writer.commit()
                storage_summary["jsonl"] = (
                    jsonl_summary.path.name if anonymizer else str(jsonl_summary.path)
                )
                storage_summary["jsonl_rows"] = jsonl_summary.written
                jsonl_writer = None
            except OSError as exc:
                storage_error = _storage_error_message(exc, anonymizer)
                _log_storage_error("Could not write JSONL target", exc, anonymizer)
            except KeyboardInterrupt:
                interrupted = True
                pipeline_failed = True
                logger.warning("Migration interrupted while committing JSONL output")
                storage_error = (
                    "Migration interrupted during the JSONL write; "
                    "the previous target file was preserved."
                )
        if jsonl_writer is not None:
            try:
                jsonl_writer.rollback()
            except OSError:
                pass
            jsonl_writer = None

    if storage_error is not None:
        storage_summary["status"] = "failed"
        storage_summary["error"] = storage_error

    source_labels = [
        anonymizer.source_label(_source_display_label(source))
        if anonymizer
        else _source_display_label(source)
        for source in sources
    ]
    has_validation_issues = bool(
        metrics.invalid_records or metrics.file_errors or metrics.incomplete_files
    )
    unresolved_conflicts = bool(
        args.conflict_policy == "skip"
        and storage_summary.get("conflicting_transaction_ids", 0)
    )
    if interrupted:
        exit_code = 130
    elif repair_log is not None and repair_log_error is not None:
        exit_code = 2
    else:
        exit_code = (
            1
            if has_validation_issues
            or unresolved_conflicts
            or storage_error is not None
            or pipeline_failed
            else 0
        )

    try:
        write_html_report(
            report_path,
            metrics,
            error_rows=report_issues,
            storage=storage_summary,
            sources=source_labels,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        display_path = report_path.name if anonymizer else report_path
        details = type(exc).__name__ if anonymizer else str(exc)
        print(f"airlock-db: could not write HTML report {display_path}: {details}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        logger.warning("Migration interrupted while writing the report; any previous report was preserved")
        return 130

    try:
        write_run_summary(
            summary_path,
            build_run_summary(
                metrics,
                storage=storage_summary,
                sources=source_labels,
                schema_name=(
                    dynamic_schema.model_name
                    if dynamic_schema
                    else FinancialTransaction.__name__
                ),
                schema_fingerprint=(
                    dynamic_schema.fingerprint
                    if dynamic_schema
                    else _BUILTIN_SCHEMA_FINGERPRINT
                ),
                output_format=output_format,
                conflict_policy=args.conflict_policy,
                anonymized=anonymizer is not None,
                exit_code=exit_code,
                issue_rows=report_issues,
                resumed_records=resumed_records,
            ),
        )
    except (OSError, TypeError, ValueError) as exc:
        summary_display = summary_path.name if anonymizer else summary_path
        details = type(exc).__name__ if anonymizer else str(exc)
        print(f"airlock-db: could not write run summary {summary_display}: {details}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        logger.warning("Migration interrupted while writing the run summary; any previous summary was preserved")
        return 130

    print(f"Records checked: {metrics.records_seen}")
    print(f"Valid: {metrics.valid_records}; invalid: {metrics.invalid_records}")
    database_display = database_path.name if anonymizer else database_path
    report_display = report_path.name if anonymizer else report_path
    if use_sqlite:
        print(f"SQLite: {database_display} ({storage_summary.get('status')})")
    else:
        print("SQLite: not used (--output-format jsonl)")
    if use_jsonl:
        jsonl_display = jsonl_path.name if anonymizer else jsonl_path
        if "jsonl_rows" in storage_summary:
            print(f"JSONL: {jsonl_display} ({storage_summary['jsonl_rows']} records)")
        else:
            print(f"JSONL: {jsonl_display} (not written)")
    if resumed_records:
        print(f"Resumed: {resumed_records} records at or below the saved checkpoint were skipped")
    if repair_log is not None and repair_log_error is None:
        suffix = " (redacted; rows cannot be repaired from this log)" if anonymizer else ""
        log_display = repair_log_path.name if anonymizer else repair_log_path
        print(f"Repair log: {log_display}{suffix}")
        if repair_log.truncated:
            print(
                f"WARNING: repair log omitted {repair_log.omitted_records} rejected rows at its configured limit.",
                file=sys.stderr,
            )
    elif repair_log_error is not None:
        print(f"Repair log: failed ({repair_log_error})", file=sys.stderr)
    if storage_summary.get("conflicting_transaction_ids", 0):
        identity_label = (
            dynamic_schema.id_field if dynamic_schema else "transaction ID"
        )
        print(
            f"Changed {identity_label} conflicts: "
            f"{storage_summary['conflicting_transaction_ids']} "
            f"({args.conflict_policy} policy)"
        )
    print(f"Report: {report_display}")
    logger.info(
        "Migration finished: %d valid, %d invalid, %s SQLite rows inserted, %s updated, %s duplicate IDs",
        metrics.valid_records,
        metrics.invalid_records,
        storage_summary.get("inserted", 0),
        storage_summary.get("updated", 0),
        storage_summary.get("duplicate_transaction_ids", 0),
    )

    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "repair":
        logging.basicConfig(
            level=logging.WARNING,
            format="%(levelname)s: %(message)s",
        )
        return repair_main(arguments[1:])
    if arguments and arguments[0] == "report":
        logging.basicConfig(
            level=logging.WARNING,
            format="%(levelname)s: %(message)s",
        )
        return refresh_main(arguments[1:])
    parser = _build_parser()
    args = parser.parse_args(arguments)
    if not args.input and not args.source_db:
        parser.error("at least one --input or --source-db is required")
    if args.source_table and not args.source_db:
        parser.error("--source-table requires --source-db")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    return run_migration(args)
