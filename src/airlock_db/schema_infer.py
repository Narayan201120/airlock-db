"""Draft a dynamic-schema config from observed records.

``airlock-db infer --input PATH`` streams records with the same readers as a
migration, classifies every observed field value, and writes a readable draft
in JSON or YAML. The draft loads with ``--config`` and is meant to be reviewed
and tightened by hand.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema_config import valid_field_name, valid_model_name

_DEFAULT_LIMIT = 10_000
_MAX_FIELDS = 256
_MAX_ISSUE_EXAMPLES = 3
_MAX_SKIPPED_LISTED = 10
_DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_TEXT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$"
)
_DECIMAL_TEXT = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")
_UUID_TEXT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_KIND_ORDER = (
    "boolean",
    "integer",
    "number",
    "decimal",
    "decimal_text",
    "uuid",
    "date",
    "datetime",
    "string",
    "object",
)
_KIND_LABELS = {"decimal_text": "decimal string"}
_TYPE_FOR_KIND = {
    "boolean": "boolean",
    "integer": "integer",
    "number": "number",
    "decimal": "decimal",
    "decimal_text": "decimal",
    "uuid": "uuid",
    "date": "date",
    "datetime": "datetime",
    "string": "string",
    "object": "string",
}
# Numeric kinds: a decimal field accepts every one of them.
_NUMERIC_KINDS = frozenset({"integer", "number", "decimal", "decimal_text"})
# Text kinds: every observed value is a Python string, so a string field
# accepts them all.
_TEXT_KINDS = frozenset({"uuid", "date", "datetime", "decimal_text", "string"})
_ID_NAMES = ("id", "guid", "uuid", "record_id")


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _single_character(value: str) -> str:
    if len(value) != 1:
        raise argparse.ArgumentTypeError("must be a single character")
    return value


def _classify_text(text: str) -> str:
    if _DATE_TEXT.fullmatch(text):
        try:
            date.fromisoformat(text)
        except ValueError:
            return "string"
        return "date"
    if _DATETIME_TEXT.fullmatch(text):
        normalised = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            datetime.fromisoformat(normalised)
        except ValueError:
            return "string"
        return "datetime"
    if _DECIMAL_TEXT.fullmatch(text):
        return "decimal_text"
    if _UUID_TEXT.fullmatch(text):
        return "uuid"
    return "string"


def _classify(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, str):
        return _classify_text(value)
    if isinstance(value, (dict, list)):
        return "object"
    return "string"


def _describe(kinds: dict[str, int]) -> str:
    ordered = sorted(kinds.items(), key=lambda item: (-item[1], _KIND_ORDER.index(item[0])))
    return ", ".join(
        f"{_KIND_LABELS.get(kind, kind)} ({count})" for kind, count in ordered
    )


@dataclass
class _FieldStats:
    nulls: int = 0
    kinds: dict[str, int] = field(default_factory=dict)

    def record(self, kind: str) -> None:
        self.kinds[kind] = self.kinds.get(kind, 0) + 1


def _scan_records(
    records: Iterable[Any],
    limit: int,
) -> tuple[dict[str, _FieldStats], int, int, int, list[str]]:
    """Collect per-field statistics from decoded input records."""

    fields: dict[str, _FieldStats] = {}
    scanned = 0
    skipped_issues = 0
    skipped_non_objects = 0
    issue_examples: list[str] = []
    for record in records:
        if limit and scanned >= limit:
            break
        if getattr(record, "issue", None) is not None:
            skipped_issues += 1
            if len(issue_examples) < _MAX_ISSUE_EXAMPLES:
                issue = record.issue
                issue_examples.append(f"{issue.code}: {issue.message}")
            continue
        value = record.value
        if not isinstance(value, dict):
            skipped_non_objects += 1
            continue
        scanned += 1
        for name, stats in fields.items():
            if name not in value:
                stats.nulls += 1
        for name, item in value.items():
            if not isinstance(name, str):
                continue
            stats = fields.get(name)
            if stats is None:
                stats = _FieldStats(nulls=scanned - 1)
                fields[name] = stats
            if item is None:
                stats.nulls += 1
            else:
                stats.record(_classify(item))
    return fields, scanned, skipped_issues, skipped_non_objects, issue_examples


def _resolve(stats: _FieldStats) -> tuple[str, bool, str | None]:
    """Return the drafted type, whether it conflicts, and a review note."""

    kinds = {kind: count for kind, count in stats.kinds.items() if count}
    if not kinds:
        return "string", False, "always null in the scanned records"
    present = set(kinds)
    if present <= _TEXT_KINDS:
        if len(present) == 1:
            kind = next(iter(present))
            return _TYPE_FOR_KIND[kind], False, None
        return "string", False, f"mixed text formats ({_describe(kinds)}); drafted as string"
    if "object" in present:
        return (
            "string",
            True,
            "some values are nested objects or lists, which a layout cannot type; drafted as string",
        )
    if present <= _NUMERIC_KINDS:
        if "decimal" in present or "decimal_text" in present:
            return "decimal", False, None
        if "number" in present:
            return "number", False, None
        return "integer", False, None
    dominant = max(kinds, key=lambda kind: (kinds[kind], -_KIND_ORDER.index(kind)))
    drafted = _TYPE_FOR_KIND[dominant]
    return drafted, True, f"type conflict: {_describe(kinds)}; drafted as {drafted}"


def _pick_id_field(fields: dict[str, _FieldStats], order: list[str]) -> str:
    """Prefer a named identity field with no nulls, then the first full field."""

    named: list[tuple[int, int, int, str]] = []
    for position, name in enumerate(order):
        stats = fields[name]
        if stats.nulls:
            continue
        folded = name.casefold()
        if folded in _ID_NAMES:
            rank = _ID_NAMES.index(folded)
        elif folded.endswith("_id"):
            rank = len(_ID_NAMES)
        else:
            continue
        named.append((0 if "uuid" in stats.kinds else 1, rank, position, name))
    if named:
        named.sort()
        return named[0][3]
    for name in order:
        if not fields[name].nulls:
            return name
    return order[0]


def _render_yaml(
    model_name: str,
    id_field: str,
    order: list[str],
    type_names: dict[str, str],
    required: dict[str, bool],
    notes: dict[str, str],
    scanned: int,
    source: str,
) -> str:
    lines = [
        f"# AirLock DB schema draft from {scanned} scanned record(s) in {source}.",
        "# Review the notes, then migrate:",
        "#   python main.py --input <source> --config <this file> --output-dir output",
        "version: 1",
        f"model_name: {model_name}",
        f"id_field: {id_field}",
        "strict: false",
        "strip_whitespace: false",
        "fields:",
    ]
    for name in order:
        note = notes.get(name)
        if note:
            lines.append(f"  # {note}")
        lines.append(f"  {name}:")
        lines.append(f"    type: {type_names[name]}")
        lines.append(f"    required: {'true' if required[name] else 'false'}")
    return "\n".join(lines) + "\n"


def _render_json(
    model_name: str,
    id_field: str,
    order: list[str],
    type_names: dict[str, str],
    required: dict[str, bool],
) -> str:
    layout = {
        "version": 1,
        "model_name": model_name,
        "id_field": id_field,
        "strict": False,
        "strip_whitespace": False,
        "fields": {
            name: {"type": type_names[name], "required": required[name]} for name in order
        },
    }
    return json.dumps(layout, indent=2) + "\n"


def _fail(message: str) -> int:
    print(f"airlock-db infer: {message}", file=sys.stderr)
    return 1


def infer_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="airlock-db infer",
        description=(
            "Read records with the normal input readers and write a draft "
            "dynamic-schema config for review."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        metavar="PATH",
        help="input file or directory to scan (same formats as a migration, including .gz and .zip)",
    )
    parser.add_argument(
        "--output",
        default="schema_draft.yaml",
        metavar="PATH",
        help="draft file to write; .yaml, .yml, or .json (default: schema_draft.yaml)",
    )
    parser.add_argument(
        "--model-name",
        default="InferredRecord",
        metavar="NAME",
        help="model_name for the drafted layout (default: InferredRecord)",
    )
    parser.add_argument(
        "--id-field",
        metavar="NAME",
        help="field to use as the record identity; defaults to a detected identity field",
    )
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        default=_DEFAULT_LIMIT,
        metavar="N",
        help=f"maximum records to scan; 0 scans everything (default: {_DEFAULT_LIMIT})",
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
        help="JSON or YAML layout that reads every input file as fixed-width records instead of by suffix",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace the output file when it already exists",
    )
    args = parser.parse_args(argv)

    from .fixed_width import FixedWidthConfigError, FixedWidthLayout
    from .ingestion import iter_source_records

    fixed_width_layout: FixedWidthLayout | None = None
    if args.fixed_width:
        try:
            fixed_width_layout = FixedWidthLayout.from_file(args.fixed_width)
        except FixedWidthConfigError as exc:
            return _fail(str(exc))

    if not valid_model_name(args.model_name):
        return _fail("model_name must be a simple Python class name")
    output_path = Path(args.output)
    suffix = output_path.suffix.casefold()
    if suffix not in {".json", ".yaml", ".yml"}:
        return _fail("--output must end in .json, .yaml, or .yml")
    if output_path.exists() and not args.overwrite:
        return _fail(f"{output_path} already exists; pass --overwrite to replace it")

    records = iter_source_records(
        args.input,
        excluded_paths=[output_path],
        csv_delimiter=args.csv_delimiter,
        csv_encoding=args.csv_encoding,
        fixed_width=fixed_width_layout,
    )
    try:
        fields, scanned, skipped_issues, skipped_non_objects, issue_examples = _scan_records(
            records, args.limit
        )
    finally:
        records.close()

    if scanned == 0:
        detail = f" ({'; '.join(issue_examples)})" if issue_examples else ""
        return _fail(f"no records were read from {args.input}{detail}")

    representable: list[str] = []
    skipped_names: list[tuple[str, str]] = []
    for name in fields:
        if valid_field_name(name):
            representable.append(name)
        else:
            skipped_names.append(
                (name, "not a valid dynamic-schema field name (letters, digits, underscore; no keywords)")
            )
    if len(representable) > _MAX_FIELDS:
        return _fail(
            f"the input has {len(representable)} usable fields; a dynamic schema supports at most {_MAX_FIELDS}"
        )

    id_field = args.id_field
    if id_field is not None:
        if id_field not in representable:
            return _fail(f"--id-field {id_field!r} was not observed in the scanned records")
    else:
        id_field = _pick_id_field(fields, representable)

    type_names: dict[str, str] = {}
    required: dict[str, bool] = {}
    notes: dict[str, str] = {}
    conflicts: dict[str, str] = {}
    for name in representable:
        drafted, conflict, note = _resolve(fields[name])
        type_names[name] = drafted
        required[name] = fields[name].nulls == 0
        if note:
            notes[name] = note
        if conflict:
            conflicts[name] = note or "review by hand"
    required[id_field] = True

    if suffix == ".json":
        text = _render_json(args.model_name, id_field, representable, type_names, required)
    else:
        text = _render_yaml(
            args.model_name,
            id_field,
            representable,
            type_names,
            required,
            notes,
            scanned,
            str(args.input),
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")

    optional = sum(1 for name in representable if not required[name])
    print(f"Draft written: {output_path} ({'json' if suffix == '.json' else 'yaml'})")
    print(f"Source: {args.input}")
    print(
        f"Records scanned: {scanned} (skipped {skipped_issues} issue row(s), "
        f"{skipped_non_objects} non-object value(s))"
    )
    print(f"Fields: {len(representable)} (required {len(representable) - optional}, optional {optional})")
    print(f"id_field: {id_field}")
    if skipped_names:
        print("Skipped field names:")
        for name, reason in skipped_names[:_MAX_SKIPPED_LISTED]:
            print(f"  - {name!r}: {reason}")
        if len(skipped_names) > _MAX_SKIPPED_LISTED:
            print(f"  ... and {len(skipped_names) - _MAX_SKIPPED_LISTED} more")
    if notes:
        print("Notes:")
        for name in representable:
            if name in notes and name not in conflicts:
                print(f"  - {name}: {notes[name]}")
    if conflicts:
        print("Conflicts (rows with other value shapes will be reported as invalid):")
        for name in representable:
            if name in conflicts:
                print(f"  - {name}: {conflicts[name]}")
    else:
        print("Conflicts: none")
    print(
        f'Next: python main.py --input "{args.input}" --config "{output_path}" '
        "--output-dir output"
    )
    return 0


if __name__ == "__main__":
    sys.exit(infer_main())
