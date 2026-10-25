"""Validate schema, constraints, and fixed-width layout files without input.

`airlock-db check` loads the same files a migration or repair session would
load, applies the same cross-file rules, and prints the counts and fingerprints
needed to compare a configuration with an existing run. It reads no records and
writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .constraints import ConstraintConfigError, ValidationConstraints
from .fixed_width import FixedWidthConfigError, FixedWidthLayout
from .repair_log import constraints_fingerprint
from .schema_config import DynamicSchema, SchemaConfigError
from .schemas import FinancialTransaction

_BUILTIN_SCHEMA_FINGERPRINT = "financial-transaction-v1"
_BUILTIN_RULE_NAMES = (
    "allowed_currencies",
    "allowed_account_ids",
    "allowed_transaction_types",
    "allowed_statuses",
    "min_amount",
    "max_amount",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airlock-db check",
        description=(
            "Validate a schema, constraints file, or fixed-width layout without "
            "reading input or writing files."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="JSON or YAML schema layout to validate",
    )
    parser.add_argument(
        "--constraints",
        metavar="JSON",
        help="JSON constraints file to validate",
    )
    parser.add_argument(
        "--fixed-width",
        metavar="PATH",
        help="JSON or YAML fixed-width layout to validate",
    )
    return parser


def _fail(message: object) -> int:
    print(f"airlock-db: {message}", file=sys.stderr)
    return 2


def check_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not (args.config or args.constraints or args.fixed_width):
        parser.error(
            "at least one of --config, --constraints, or --fixed-width is required"
        )

    dynamic_schema: DynamicSchema | None = None
    if args.config:
        try:
            dynamic_schema = DynamicSchema.from_file(args.config)
        except SchemaConfigError as exc:
            return _fail(exc)

    constraints: ValidationConstraints | None = None
    if args.constraints:
        try:
            constraints = ValidationConstraints.from_file(args.constraints)
        except ConstraintConfigError as exc:
            return _fail(exc)

    if (
        dynamic_schema is not None
        and constraints is not None
        and constraints.has_builtin_keys()
    ):
        return _fail(
            "--constraints cannot be combined with --config unless the file "
            "contains only field_rules; put these rules in the schema config"
        )

    layout: FixedWidthLayout | None = None
    if args.fixed_width:
        try:
            layout = FixedWidthLayout.from_file(args.fixed_width)
        except FixedWidthConfigError as exc:
            return _fail(exc)

    schema_fields = (
        len(dynamic_schema.fields)
        if dynamic_schema is not None
        else len(FinancialTransaction.model_fields)
    )
    transform_count = (
        sum(len(rules) for rules in dynamic_schema.transforms.values())
        if dynamic_schema is not None
        else 0
    )
    if dynamic_schema is not None:
        print(
            f"Schema: {dynamic_schema.model_name} "
            f"({schema_fields} fields, {transform_count} transforms, "
            f"fingerprint {dynamic_schema.fingerprint})"
        )
    else:
        print(
            f"Schema: built-in financial transaction "
            f"({schema_fields} fields, fingerprint {_BUILTIN_SCHEMA_FINGERPRINT})"
        )

    if constraints is None:
        builtin_rules = 0
        field_rules = 0
        print("Constraints: none")
    else:
        builtin_rules = sum(
            getattr(constraints, name) is not None for name in _BUILTIN_RULE_NAMES
        )
        field_rules = len(constraints.field_rules)
        print(
            f"Constraints: {builtin_rules} built-in rules, "
            f"{field_rules} field rules "
            f"(fingerprint {constraints_fingerprint(constraints)})"
        )

    if layout is None:
        layout_fields = 0
        print("Layout: none")
    else:
        layout_fields = len(layout.fields)
        print(
            f"Layout: {layout_fields} fields, encoding {layout.encoding}, "
            f"skip_lines {layout.skip_lines}, record_length {layout.record_length}"
        )

    print("Check: configuration is valid. No files were written.")
    print(
        f"check_ok schema={'dynamic' if dynamic_schema else 'builtin'} "
        f"schema_fields={schema_fields} transforms={transform_count} "
        f"builtin_rules={builtin_rules} field_rules={field_rules} "
        f"layout_fields={layout_fields}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(check_main())
