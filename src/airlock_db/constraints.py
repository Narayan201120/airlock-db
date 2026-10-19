"""Load optional, local business constraints for migration validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class ConstraintConfigError(ValueError):
    """Raised when a constraints file is malformed or unsupported."""


_ACCOUNT_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_ALLOWED_KEYS = frozenset(
    {
        "allowed_currencies",
        "allowed_account_ids",
        "allowed_transaction_types",
        "allowed_statuses",
        "min_amount",
        "max_amount",
        "field_rules",
    }
)
_RULE_KEYS = frozenset({"field", "enum", "min", "max", "pattern", "required_any_of"})
_RULE_FIELD_KEYS = ("enum", "min", "max", "pattern")
_TRANSACTION_TYPES = frozenset({"DEBIT", "CREDIT", "REFUND"})
_STATUSES = frozenset({"PENDING", "POSTED", "REVERSED"})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConstraintConfigError(f"duplicate constraints key: {key}")
        result[key] = value
    return result


def _parse_choice_set(
    name: str,
    value: Any,
    *,
    pattern: re.Pattern[str] | None = None,
    allowed: frozenset[str] | None = None,
    uppercase: bool = True,
) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ConstraintConfigError(f"{name} must be a non-empty JSON array of strings")

    choices: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConstraintConfigError(f"every value in {name} must be a string")
        choice = item.strip().upper() if uppercase else item.strip()
        if not choice or any(ord(char) < 32 for char in choice):
            raise ConstraintConfigError(f"{name} contains an empty or invalid string")
        if pattern is not None and pattern.fullmatch(choice) is None:
            raise ConstraintConfigError(f"invalid value {item!r} in {name}")
        if allowed is not None and choice not in allowed:
            options = ", ".join(sorted(allowed))
            raise ConstraintConfigError(
                f"unsupported value {item!r} in {name}; choose from {options}"
            )
        choices.append(choice)
    if len(set(choices)) != len(choices):
        raise ConstraintConfigError(f"{name} contains duplicate values")
    return frozenset(choices)


def _parse_amount(name: str, value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ConstraintConfigError(f"{name} must be a decimal string or JSON number")
    try:
        amount = Decimal(value) if isinstance(value, (int, Decimal)) else Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConstraintConfigError(f"{name} must be a valid decimal amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise ConstraintConfigError(f"{name} must be a finite amount greater than zero")
    return amount


def _canonical_decimal(value: Decimal) -> str:
    parts = value.as_tuple()
    digits = list(parts.digits)
    exponent = parts.exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return "0"
    coefficient = str(digits[0])
    if len(digits) > 1:
        coefficient += "." + "".join(str(digit) for digit in digits[1:])
    adjusted_exponent = exponent + len(digits) - 1
    sign = "-" if parts.sign else ""
    return f"{sign}{coefficient}e{adjusted_exponent}"


def _enum_comparison_key(value: Any) -> tuple[str, str]:
    """Canonical key that mirrors the runtime enum comparison rule."""

    if isinstance(value, bool):
        return ("text", str(value))
    if isinstance(value, str):
        text = value.strip()
        try:
            number = Decimal(text)
        except InvalidOperation:
            number = None
        if number is not None and number.is_finite():
            return ("number", _canonical_decimal(number))
        return ("text", text)
    if isinstance(value, (int, Decimal)):
        number = Decimal(value)
        if number.is_finite():
            return ("number", _canonical_decimal(number))
    return ("text", str(value))


def _parse_rule_bound(label: str, name: str, field_name: str, value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ConstraintConfigError(f"{label} {name} for field {field_name!r} must be a JSON number")
    number = Decimal(value)
    if not number.is_finite():
        raise ConstraintConfigError(f"{label} {name} for field {field_name!r} must be a finite number")
    return number


def _parse_field_rules(value: Any) -> tuple[FieldRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConstraintConfigError("field_rules must be a JSON array")

    rules: list[FieldRule] = []
    seen_fields: dict[str, int] = {}
    for index, entry in enumerate(value):
        label = f"field_rules[{index}]"
        if not isinstance(entry, dict):
            raise ConstraintConfigError(f"{label} must be a JSON object")
        unknown = set(entry) - _RULE_KEYS
        if unknown:
            raise ConstraintConfigError(
                f"{label} has unknown key(s): " + ", ".join(sorted(unknown))
            )
        if "required_any_of" in entry and "field" in entry:
            raise ConstraintConfigError(
                f'{label} must declare "field" or "required_any_of", not both'
            )
        if "required_any_of" in entry:
            ignored = [key for key in _RULE_FIELD_KEYS if key in entry]
            if ignored:
                raise ConstraintConfigError(
                    f"{label} required_any_of cannot be combined with "
                    + ", ".join(ignored)
                )
            names = entry["required_any_of"]
            if not isinstance(names, list) or not names:
                raise ConstraintConfigError(
                    f"{label} required_any_of must be a non-empty JSON array of field names"
                )
            parsed: list[str] = []
            for name in names:
                if not isinstance(name, str) or not name.strip():
                    raise ConstraintConfigError(
                        f"{label} required_any_of must contain only non-empty field names"
                    )
                parsed.append(name.strip())
            if len(set(parsed)) != len(parsed):
                raise ConstraintConfigError(
                    f"{label} required_any_of contains duplicate field names"
                )
            rules.append(FieldRule(required_any_of=tuple(parsed)))
            continue
        if "field" not in entry:
            raise ConstraintConfigError(f'{label} must declare "field" or "required_any_of"')
        field_name = entry["field"]
        if not isinstance(field_name, str) or not field_name.strip():
            raise ConstraintConfigError(f"{label} field must be a non-empty string")
        field_name = field_name.strip()
        if field_name in seen_fields:
            raise ConstraintConfigError(
                f"{label} duplicates the rules for field {field_name!r} "
                f"(already declared by field_rules[{seen_fields[field_name]}])"
            )
        if not any(key in entry for key in _RULE_FIELD_KEYS):
            raise ConstraintConfigError(
                f"{label} for field {field_name!r} must declare at least one of "
                "enum, min, max, pattern"
            )

        enum_values: tuple[Any, ...] = ()
        if "enum" in entry:
            raw_enum = entry["enum"]
            if not isinstance(raw_enum, list) or not raw_enum:
                raise ConstraintConfigError(
                    f"{label} enum for field {field_name!r} must be a non-empty JSON array of scalars"
                )
            items: list[Any] = []
            seen_values: set[tuple[str, str]] = set()
            for item in raw_enum:
                if isinstance(item, (dict, list)) or item is None:
                    raise ConstraintConfigError(
                        f"{label} enum for field {field_name!r} must contain only scalar values"
                    )
                key = _enum_comparison_key(item)
                if key in seen_values:
                    raise ConstraintConfigError(
                        f"{label} enum for field {field_name!r} contains a duplicate value {item!r}"
                    )
                seen_values.add(key)
                items.append(item)
            enum_values = tuple(items)

        minimum = (
            _parse_rule_bound(label, "min", field_name, entry["min"])
            if "min" in entry
            else None
        )
        maximum = (
            _parse_rule_bound(label, "max", field_name, entry["max"])
            if "max" in entry
            else None
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ConstraintConfigError(
                f"{label} min must not exceed max for field {field_name!r}"
            )

        pattern_text: str | None = None
        if "pattern" in entry:
            raw_pattern = entry["pattern"]
            if not isinstance(raw_pattern, str) or not raw_pattern:
                raise ConstraintConfigError(
                    f"{label} pattern for field {field_name!r} must be a non-empty string"
                )
            try:
                re.compile(raw_pattern)
            except re.error as exc:
                raise ConstraintConfigError(
                    f"{label} pattern for field {field_name!r} is not a valid "
                    f"regular expression: {exc}"
                ) from exc
            pattern_text = raw_pattern

        seen_fields[field_name] = index
        rules.append(
            FieldRule(
                field=field_name,
                enum=enum_values,
                min=minimum,
                max=maximum,
                pattern=pattern_text,
            )
        )
    return tuple(rules)


@dataclass(frozen=True, slots=True)
class FieldRule:
    """One per-field constraint set, or one required-any-of group."""

    field: str | None = None
    enum: tuple[Any, ...] = ()
    min: Decimal | None = None
    max: Decimal | None = None
    pattern: str | None = None
    required_any_of: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationConstraints:
    """Optional business rules applied after the strict target schema."""

    allowed_currencies: frozenset[str] | None = None
    allowed_account_ids: frozenset[str] | None = None
    allowed_transaction_types: frozenset[str] | None = None
    allowed_statuses: frozenset[str] | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    field_rules: tuple[FieldRule, ...] = ()

    def has_builtin_keys(self) -> bool:
        """Return True when the file configures any built-in schema rule."""

        return (
            self.allowed_currencies is not None
            or self.allowed_account_ids is not None
            or self.allowed_transaction_types is not None
            or self.allowed_statuses is not None
            or self.min_amount is not None
            or self.max_amount is not None
        )

    @classmethod
    def from_file(cls, path: str | Path) -> ValidationConstraints:
        """Read a UTF-8 JSON policy file without consulting the network."""

        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ConstraintConfigError(f"cannot read constraints file {source}: {exc}") from exc
        try:
            raw = json.loads(
                text,
                parse_float=Decimal,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ConstraintConfigError(f"non-standard JSON constant: {value}")
                ),
                object_pairs_hook=_unique_object,
            )
        except (json.JSONDecodeError, InvalidOperation, RecursionError) as exc:
            raise ConstraintConfigError(f"invalid JSON in constraints file {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConstraintConfigError("the constraints file root must be a JSON object")
        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            raise ConstraintConfigError(
                "unknown constraints key(s): " + ", ".join(sorted(unknown))
            )

        min_amount = _parse_amount("min_amount", raw.get("min_amount"))
        max_amount = _parse_amount("max_amount", raw.get("max_amount"))
        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            raise ConstraintConfigError("min_amount must not exceed max_amount")

        return cls(
            allowed_currencies=_parse_choice_set(
                "allowed_currencies", raw.get("allowed_currencies"), pattern=_CURRENCY_PATTERN
            ),
            allowed_account_ids=_parse_choice_set(
                "allowed_account_ids", raw.get("allowed_account_ids"), pattern=_ACCOUNT_PATTERN
            ),
            allowed_transaction_types=_parse_choice_set(
                "allowed_transaction_types",
                raw.get("allowed_transaction_types"),
                allowed=_TRANSACTION_TYPES,
            ),
            allowed_statuses=_parse_choice_set(
                "allowed_statuses", raw.get("allowed_statuses"), allowed=_STATUSES
            ),
            min_amount=min_amount,
            max_amount=max_amount,
            field_rules=_parse_field_rules(raw.get("field_rules")),
        )
