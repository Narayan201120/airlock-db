"""Interactive repair workflow for records in an AirLock DB repair log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import BaseModel

from .anonymize import RunAnonymizer
from .constraints import ConstraintConfigError, ValidationConstraints
from .dynamic_store import DynamicSQLiteTargetStore
from .pipeline import ValidationIssue, validate_record_value
from .repair_log import (
    RepairLogError,
    RepairLogReader,
    constraints_fingerprint,
)
from .repair_progress import RepairProgressReader
from .schema_config import DynamicSchema, SchemaConfigError
from .schemas import FinancialTransaction
from .sqlite_store import SQLiteTargetStore


_BUILTIN_SCHEMA_FINGERPRINT = "financial-transaction-v1"
_MAX_CORRECTION_BYTES = 8 * 1024 * 1024
_MAX_STAGING_ENTRY_BYTES = _MAX_CORRECTION_BYTES + 4096
_MAX_REPAIR_STAGING_BYTES = 256 * 1024 * 1024


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        if os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right)):
            return True
    except OSError:
        pass
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _build_repair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} repair",
        description="Review rejected records, correct or drop them, and write valid rows to SQLite.",
    )
    parser.add_argument("--error-log", required=True, metavar="PATH", help="JSONL log created by a migration")
    parser.add_argument("--config", metavar="PATH", help="JSON or YAML schema used by the migration")
    parser.add_argument("--constraints", metavar="JSON", help="same business-rule file used by the migration")
    parser.add_argument("--output-db", metavar="PATH", help="SQLite target (default: target_migration.db beside the error log)")
    parser.add_argument("--batch-size", type=_positive_integer, default=500, help="SQLite write batch size")
    parser.add_argument("--conflict-policy", choices=("skip", "replace"), default="skip")
    parser.add_argument("--anonymize", action="store_true", help="pseudonymize configured sensitive fields before storage")
    return parser


def _positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if result < 1 or result > 10_000:
        raise argparse.ArgumentTypeError("must be between 1 and 10000")
    return result


def _parse_correction(value: str) -> dict[str, Any]:
    if len(value.encode("utf-8")) > _MAX_CORRECTION_BYTES:
        raise ValueError("correction exceeds the 8 MiB limit")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate key in correction: {key}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> Any:
        raise ValueError(f"non-standard JSON constant is not supported: {constant}")

    parsed = json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=Decimal,
    )
    if not isinstance(parsed, dict):
        raise ValueError("the corrected record must be a JSON object")
    return parsed


def _parse_placeholder(value: str) -> Any:
    if len(value.encode("utf-8")) > _MAX_CORRECTION_BYTES:
        raise ValueError("placeholder exceeds the 8 MiB limit")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate key in placeholder: {key}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> Any:
        raise ValueError(f"non-standard JSON constant is not supported: {constant}")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=Decimal,
    )


def _spool_safe_record(value: Any) -> dict[str, Any]:
    """Keep Decimal values typed across the temporary JSONL staging file."""
    decimal_paths: list[list[str | int]] = []

    def convert(item: Any, path: list[str | int]) -> Any:
        if isinstance(item, Decimal):
            decimal_paths.append(path)
            return str(item)
        if isinstance(item, dict):
            return {key: convert(child, [*path, key]) for key, child in item.items()}
        if isinstance(item, list):
            return [convert(child, [*path, index]) for index, child in enumerate(item)]
        return item

    return {"record": convert(value, []), "decimal_paths": decimal_paths}


def _write_staged_decision(spool: Any, decision: dict[str, Any], total_bytes: int) -> int:
    decision_fingerprint = _decision_fingerprint(decision)
    staged = {**decision, "decision_fingerprint": decision_fingerprint}
    line = json.dumps(staged, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    line_bytes = len(line.encode("utf-8")) + 1
    if line_bytes > _MAX_STAGING_ENTRY_BYTES:
        raise ValueError("repair decision exceeds the staging entry limit")
    if total_bytes + line_bytes > _MAX_REPAIR_STAGING_BYTES:
        raise ValueError("repair staging exceeds the 256 MiB limit")
    spool.write(line + "\n")
    return total_bytes + line_bytes


def _decision_fingerprint(decision: dict[str, Any]) -> str:
    canonical = json.dumps(
        decision,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _restore_spooled_decimals(value: Any, paths: list[list[str | int]]) -> Any:
    for path in paths:
        if not path:
            raise ValueError("invalid staged decimal path")
        parent = value
        for part in path[:-1]:
            parent = parent[part]
        key = path[-1]
        original = parent[key]
        if not isinstance(original, str):
            raise ValueError("invalid staged decimal value")
        parent[key] = Decimal(original)
    return value


def _read_prompt(input_fn: Callable[[str], str], prompt: str) -> str | None:
    try:
        return input_fn(prompt)
    except EOFError:
        return None


def _render(value: Any, *, indent: int | None = None, maximum: int = 4000) -> str:
    rendered = json.dumps(value, ensure_ascii=True, indent=indent, default=str)
    if len(rendered) > maximum:
        return rendered[:maximum] + "\n... [display truncated]"
    return rendered


def _display_issues(issues: Sequence[dict[str, Any]], *, redacted: bool = False) -> None:
    if not issues:
        print("No validation trace was recorded for this row.")
        return
    for index, issue in enumerate(issues, start=1):
        rendered_issue = dict(issue)
        if redacted:
            rendered_issue["input"] = "[REDACTED]" if issue.get("input") is not None else None
            rendered_issue["context"] = None
            rendered_issue["message"] = "Validation details redacted in anonymize mode."
        print(f"  {index}. {_render(rendered_issue, maximum=2000)}")


def _write_store(
    database: Path,
    schema: DynamicSchema | None,
    batch_size: int,
    conflict_policy: str,
) -> SQLiteTargetStore | DynamicSQLiteTargetStore:
    if schema is None:
        store: SQLiteTargetStore | DynamicSQLiteTargetStore = SQLiteTargetStore(
            database, batch_size=batch_size, conflict_policy=conflict_policy
        )
    else:
        store = DynamicSQLiteTargetStore(
            database,
            schema,
            batch_size=batch_size,
            conflict_policy=conflict_policy,
        )
    store.__enter__()
    return store


def _trace_for_retry(issues: Sequence[ValidationIssue]) -> list[dict[str, Any]]:
    return [issue.to_dict() for issue in issues]


def run_repair(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
) -> int:
    dynamic_schema: DynamicSchema | None = None
    try:
        if args.config:
            dynamic_schema = DynamicSchema.from_file(args.config)
        if dynamic_schema is not None and args.constraints:
            raise SchemaConfigError("--constraints cannot be combined with --config")
        if args.anonymize and dynamic_schema is not None and not dynamic_schema.anonymized_fields:
            raise SchemaConfigError("--anonymize with --config requires fields marked anonymize: true")
        constraints = (
            ValidationConstraints.from_file(args.constraints)
            if args.constraints
            else None
        )
    except (SchemaConfigError, ConstraintConfigError) as exc:
        print(f"airlock-db repair: {exc}", file=sys.stderr)
        return 2

    expected_schema_fingerprint = (
        dynamic_schema.fingerprint if dynamic_schema else _BUILTIN_SCHEMA_FINGERPRINT
    )
    expected_schema_name = (
        dynamic_schema.model_name if dynamic_schema else FinancialTransaction.__name__
    )
    log_path = Path(args.error_log)
    database_path = (
        Path(args.output_db)
        if args.output_db
        else log_path.parent / "target_migration.db"
    )
    protected_inputs = [log_path]
    if args.config:
        protected_inputs.append(Path(args.config))
    if args.constraints:
        protected_inputs.append(Path(args.constraints))
    if any(_same_path(database_path, protected) for protected in protected_inputs):
        print(
            "airlock-db repair: --output-db cannot overwrite the repair log, schema config, or constraints file",
            file=sys.stderr,
        )
        return 2

    try:
        reader = RepairLogReader(log_path)
        reader.__enter__()
    except (OSError, RepairLogError) as exc:
        print(f"airlock-db repair: cannot read repair log: {exc}", file=sys.stderr)
        return 2

    header = reader.header or {}
    if header.get("anonymized"):
        reader.close()
        print(
            "airlock-db repair: this log was created with --anonymize; rejected input rows were omitted for privacy",
            file=sys.stderr,
        )
        return 2
    if header.get("schema_fingerprint") != expected_schema_fingerprint:
        reader.close()
        print(
            "airlock-db repair: schema does not match the one recorded in the error log; supply the original --config",
            file=sys.stderr,
        )
        return 2
    if header.get("constraints_fingerprint") != constraints_fingerprint(constraints):
        reader.close()
        print(
            "airlock-db repair: business constraints do not match the migration error log; supply the original --constraints file",
            file=sys.stderr,
        )
        return 2

    try:
        for _ in reader:
            pass
        validated_trailer = reader.trailer
        validated_fingerprint = reader.fingerprint
        reader.close()
        reader = RepairLogReader(log_path)
        reader.__enter__()
        if reader.header != header:
            raise RepairLogError("repair log changed while it was being checked")
    except (OSError, RepairLogError) as exc:
        reader.close()
        print(f"airlock-db repair: cannot validate repair log: {exc}", file=sys.stderr)
        return 2
    if validated_trailer is None:
        reader.close()
        print("airlock-db repair: repair log has no completion marker", file=sys.stderr)
        return 2

    model: type[BaseModel] = dynamic_schema.model if dynamic_schema else FinancialTransaction
    sensitive_fields = (
        dynamic_schema.anonymized_fields
        if dynamic_schema
        else frozenset({"account_id", "counterparty", "description"})
    )
    anonymizer = RunAnonymizer() if args.anonymize else None
    store: SQLiteTargetStore | DynamicSQLiteTargetStore | None = None
    summary: Any = None
    repaired = 0
    dropped = 0
    unrepairable = 0
    skipped_conflicts = 0
    quit_requested = False
    resumed = 0
    staged_decisions = 0
    staging_bytes = 0
    progress_reader: RepairProgressReader | None = None
    progress_namespace = hashlib.sha256(
        f"{validated_fingerprint}:{args.conflict_policy}:"
        f"{'anonymized' if anonymizer is not None else 'plain'}".encode("utf-8")
    ).hexdigest()

    try:
        progress_reader = RepairProgressReader(
            database_path,
            progress_namespace,
            entry_count=validated_trailer["records"],
        )
        progress_reader.__enter__()
        next_progress = progress_reader.next()
    except (OSError, sqlite3.Error, ValueError) as exc:
        if progress_reader is not None:
            progress_reader.close()
        reader.close()
        details = type(exc).__name__ if anonymizer is not None else str(exc)
        print(f"airlock-db repair: cannot read repair progress: {details}", file=sys.stderr)
        return 2

    display_log_path = log_path.name if anonymizer is not None else log_path
    display_database_path = database_path.name if anonymizer is not None else database_path
    print(f"Repair log: {display_log_path}")
    print(f"Schema: {expected_schema_name}; target: {display_database_path}")
    print("Each rejected row will be reviewed interactively. Type q to finish this session.")
    if not validated_trailer["complete"]:
        print("Warning: the source migration did not finish; this log covers only its processed records.")
    if validated_trailer["truncated"]:
        print(
            "Warning: the repair log reached its size or record limit; "
            f"{validated_trailer['omitted_records']} rejected rows were not recorded."
        )

    try:
        spool = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", newline="\n")
    except OSError as exc:
        reader.close()
        progress_reader.close()
        print(f"airlock-db repair: cannot create temporary repair staging file: {exc}", file=sys.stderr)
        return 2

    try:
        for entry_number, entry in enumerate(reader, start=1):
            if next_progress is not None and next_progress[0] < entry_number:
                raise sqlite3.DatabaseError("repair progress entries are not ordered")
            if next_progress is not None and next_progress[0] == entry_number:
                resumed += 1
                next_progress = progress_reader.next()
                continue

            if not entry["repairable"]:
                unrepairable += 1
                source_text = (
                    anonymizer.source_label(entry["source"])
                    if anonymizer is not None
                    else _render(entry["source"])
                )
                print(
                    f"\nSkipping non-repairable entry from {source_text}"
                    f" (record {entry['record_number']}): "
                    f"{_render(entry.get('reason') or 'no raw row available')}"
                )
                if entry.get("issues_truncated"):
                    print("  The error trace was truncated to keep the log entry bounded.")
                _display_issues(entry["issues"], redacted=anonymizer is not None)
                continue

            candidate = entry["record"]
            source_text = (
                anonymizer.source_label(entry["source"])
                if anonymizer is not None
                else _render(entry["source"])
            )
            print("\nRejected record")
            print(f"  source: {source_text}")
            print(f"  record: {entry['record_number']}")
            print(
                "  input: [REDACTED in anonymize mode]"
                if anonymizer is not None
                else f"  input: {_render(candidate, indent=2)}"
            )
            print("  Pydantic trace:")
            _display_issues(entry["issues"], redacted=anonymizer is not None)
            if entry.get("issues_truncated"):
                print("  The validation trace was truncated to keep the log entry bounded.")

            while True:
                action_text = _read_prompt(
                    input_fn,
                    "[e] edit full JSON, [p] set placeholder, [r] remove field, [d] drop, [q] finish: ",
                )
                if action_text is None:
                    action = "q"
                else:
                    action = action_text.strip().casefold()
                if action in {"q", "quit"}:
                    quit_requested = True
                    break
                if action in {"d", "drop"}:
                    staging_bytes = _write_staged_decision(
                        spool,
                        {"entry_number": entry_number, "outcome": "dropped"},
                        staging_bytes,
                    )
                    staged_decisions += 1
                    dropped += 1
                    print("Row dropped by operator.")
                    break
                if action in {"e", "edit"}:
                    correction_text = _read_prompt(
                        input_fn,
                        "Enter the complete corrected record as one JSON object: ",
                    )
                    if correction_text is None:
                        quit_requested = True
                        break
                    try:
                        candidate = _parse_correction(correction_text)
                    except (ValueError, RecursionError) as exc:
                        print(f"Invalid correction: {exc}")
                        continue
                elif action in {"p", "placeholder", "default"}:
                    if not isinstance(candidate, dict):
                        print("This input is not an object. Use edit and enter a complete JSON object.")
                        continue
                    field_input = _read_prompt(input_fn, "Field name to set: ")
                    if field_input is None:
                        quit_requested = True
                        break
                    field_name = field_input.strip()
                    if field_name not in model.model_fields:
                        print("Unknown field. Valid fields: " + ", ".join(model.model_fields))
                        continue
                    placeholder_text = _read_prompt(
                        input_fn,
                        "Placeholder JSON value (quote text, e.g. \"UNKNOWN\"): ",
                    )
                    if placeholder_text is None:
                        quit_requested = True
                        break
                    try:
                        placeholder = _parse_placeholder(placeholder_text)
                    except (ValueError, RecursionError) as exc:
                        print(f"Invalid placeholder: {exc}")
                        continue
                    candidate = dict(candidate)
                    candidate[field_name] = placeholder
                elif action in {"r", "remove"}:
                    if not isinstance(candidate, dict):
                        print("This input is not an object. Use edit and enter a complete JSON object.")
                        continue
                    field_input = _read_prompt(input_fn, "Field name to remove: ")
                    if field_input is None:
                        quit_requested = True
                        break
                    field_name = field_input.strip()
                    if field_name not in candidate:
                        print("That field is not present in this rejected record.")
                        continue
                    candidate = dict(candidate)
                    del candidate[field_name]
                else:
                    print("Choose e, p, r, d, or q.")
                    continue

                try:
                    record, issues = validate_record_value(
                        candidate,
                        model=model,
                        constraints=constraints,
                    )
                except Exception as exc:
                    details = type(exc).__name__ if anonymizer is not None else f"{type(exc).__name__}: {exc}"
                    print(f"Validation could not complete: {details}")
                    continue
                if record is None:
                    print("The correction still fails validation:")
                    _display_issues(
                        _trace_for_retry(issues), redacted=anonymizer is not None
                    )
                    print("Update the record and retry, or drop it.")
                    continue

                staged_candidate = candidate
                if anonymizer is not None:
                    anonymized_record = (
                        anonymizer.configured_record(record, sensitive_fields)
                        if dynamic_schema is not None
                        else anonymizer.transaction(record)
                    )
                    staged_candidate = dict(candidate)
                    for field_name in sensitive_fields:
                        if field_name in staged_candidate:
                            staged_candidate[field_name] = getattr(
                                anonymized_record, field_name
                            )
                staged = _spool_safe_record(staged_candidate)
                staging_bytes = _write_staged_decision(
                    spool,
                    {
                        "entry_number": entry_number,
                        "outcome": "corrected",
                        **staged,
                    },
                    staging_bytes,
                )
                staged_decisions += 1
                repaired += 1
                print("Corrected record passed validation and was staged with its resume checkpoint.")
                break

            if quit_requested:
                print("Repair session ended at your request; remaining log entries were not reviewed.")
                break

        if quit_requested:
            for _ in reader:
                pass

        if reader.fingerprint != validated_fingerprint:
            raise RepairLogError("repair log changed while it was being reviewed")

        reader.close()
        progress_reader.close()
        if staged_decisions:
            spool.seek(0)
            store = _write_store(
                database_path,
                dynamic_schema,
                args.batch_size,
                args.conflict_policy,
            )
            for line in spool:
                try:
                    decision = json.loads(line, parse_float=Decimal)
                    decision_fingerprint = decision.pop("decision_fingerprint")
                    entry_number = decision["entry_number"]
                    outcome = decision["outcome"]
                    if (
                        not isinstance(decision_fingerprint, str)
                        or decision_fingerprint != _decision_fingerprint(decision)
                    ):
                        raise ValueError("staged decision fingerprint does not match its payload")
                    if (
                        isinstance(entry_number, bool)
                        or not isinstance(entry_number, int)
                        or entry_number < 1
                    ):
                        raise ValueError("invalid staged entry number")
                    if outcome == "corrected":
                        candidate = _restore_spooled_decimals(
                            decision["record"], decision["decimal_paths"]
                        )
                        record, issues = validate_record_value(
                            candidate,
                            model=model,
                            constraints=constraints,
                        )
                        if record is None:
                            raise ValueError("a staged correction no longer passes schema validation")
                        store.add(record)
                    elif outcome != "dropped":
                        raise ValueError("invalid staged repair outcome")
                    store.record_repair_progress(
                        progress_namespace,
                        entry_number,
                        outcome,
                        decision_fingerprint,
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid staged repair decision: {exc}") from exc
            summary = store.commit()
            skipped_conflicts = summary.conflicting_transaction_ids
    except KeyboardInterrupt:
        if store is not None:
            try:
                store.rollback()
            except sqlite3.Error:
                pass
        print("\nRepair interrupted; uncommitted corrections were rolled back.", file=sys.stderr)
        return 130
    except Exception as exc:
        if store is not None:
            try:
                store.rollback()
            except sqlite3.Error:
                pass
        details = type(exc).__name__ if anonymizer is not None else f"{type(exc).__name__}: {exc}"
        if staged_decisions:
            print(
                f"{repaired} corrected rows and {dropped} drop decisions were staged but not committed; "
                "resolve the reported issue before rerunning repair.",
                file=sys.stderr,
            )
        print(f"airlock-db repair: {details}", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            try:
                store.close()
            except sqlite3.Error:
                pass
        reader.close()
        if progress_reader is not None:
            progress_reader.close()
        spool.close()
    print(
        f"Repair complete: {repaired} corrected, {dropped} dropped, "
        f"{unrepairable} non-repairable entries."
    )
    if resumed:
        print(f"Resumed: {resumed} correction or drop decisions already saved in SQLite.")
    if store is not None:
        print(
            f"SQLite: {display_database_path} ({summary.inserted} inserted, "
            f"{summary.duplicate_transaction_ids} duplicates, {summary.updated} updated; "
            f"{staged_decisions} repair decisions saved)"
        )
    else:
        print("SQLite: no new database changes")
    if skipped_conflicts and args.conflict_policy == "skip":
        print(f"Changed record conflicts skipped: {skipped_conflicts}")
        return 1
    return 0


def repair_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_repair_parser()
    args = parser.parse_args(argv)
    return run_repair(args)
