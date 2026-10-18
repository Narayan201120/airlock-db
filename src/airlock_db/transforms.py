"""Declarative per-field value normalization for dynamic schema configs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

_RULE_NAMES = ("alias", "datetime", "decimal", "lowercase", "trim", "uppercase")
_PARAMETERLESS_RULES = frozenset({"trim", "lowercase", "uppercase"})
_STRING_TYPES = frozenset({"string", "enum"})
_TIME_DIRECTIVES = ("%H", "%I", "%M", "%S", "%f", "%X")
_DIRECTIVE = re.compile(r"%[A-Za-z%]")
_PLAIN_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_MAX_DECIMAL_PLACES = 12


class TransformConfigError(ValueError):
    """Raised when a field's transforms block is invalid."""


class TransformValueError(ValueError):
    """Raised when one value cannot pass one rule."""


@dataclass(frozen=True, slots=True)
class TransformFailure:
    """One field value a rule could not convert."""

    field: str
    rule: str
    message: str
    input_value: Any


@dataclass(frozen=True, slots=True)
class TransformRule:
    """One compiled normalization rule."""

    kind: str
    pattern: str | None = None
    has_time: bool = False
    places: int | None = None
    dot_decimal: bool = True
    aliases: Mapping[str, str] = field(default_factory=dict)

    def label(self) -> str:
        """Human-readable rule name used in issue messages."""

        if self.kind == "datetime":
            return f"datetime({self.pattern})"
        return self.kind

    def canonical(self) -> dict[str, Any]:
        """Return the JSON-safe form that feeds the schema fingerprint."""

        if self.kind == "datetime":
            return {"datetime": self.pattern}
        if self.kind == "decimal":
            return {"decimal": {"dot": self.dot_decimal, "places": self.places}}
        if self.kind == "alias":
            return {"alias": {key: self.aliases[key] for key in sorted(self.aliases)}}
        return {self.kind: True}


def parse_transforms(
    raw: Any,
    *,
    field: str,
    type_name: str,
    enum_values: tuple[str, ...] = (),
) -> tuple[TransformRule, ...]:
    """Validate and compile one field's ``transforms`` list in order."""

    label = f"{field}.transforms"
    if not isinstance(raw, list) or not raw:
        raise TransformConfigError(f"{label} must be a non-empty list of rules")
    rules: list[TransformRule] = []
    for position, entry in enumerate(raw):
        item_label = f"{label}[{position}]"
        if not isinstance(entry, dict) or len(entry) != 1:
            raise TransformConfigError(
                f"{item_label} must be an object with exactly one rule key"
            )
        kind, spec = next(iter(entry.items()))
        if kind not in _RULE_NAMES:
            raise TransformConfigError(
                f"{item_label} has an unknown rule {kind!r}; choose from "
                + ", ".join(_RULE_NAMES)
            )
        if kind in _PARAMETERLESS_RULES:
            if spec is not True:
                raise TransformConfigError(
                    f"{item_label}: {kind} must be true; remove the entry to disable it"
                )
            if type_name not in _STRING_TYPES:
                raise TransformConfigError(
                    f"{item_label}: {kind} is only valid for string or enum fields"
                )
            rules.append(TransformRule(kind))
        elif kind == "datetime":
            rules.append(_parse_datetime(spec, item_label, type_name))
        elif kind == "decimal":
            rules.append(_parse_decimal(spec, item_label, type_name))
        else:
            rules.append(_parse_alias(spec, item_label, field, type_name, enum_values))
    return tuple(rules)


def _parse_datetime(spec: Any, item_label: str, type_name: str) -> TransformRule:
    if type_name not in {"date", "datetime"}:
        raise TransformConfigError(
            f"{item_label}: datetime is only valid for date or datetime fields"
        )
    if not isinstance(spec, str) or not spec:
        raise TransformConfigError(
            f"{item_label}: datetime needs a non-empty strptime pattern"
        )
    if _DIRECTIVE.search(spec) is None:
        raise TransformConfigError(
            f"{item_label}: the datetime pattern needs at least one % directive"
        )
    has_time = any(directive in spec for directive in _TIME_DIRECTIVES)
    if has_time and type_name == "date":
        raise TransformConfigError(
            f"{item_label}: a pattern with time directives needs a datetime field"
        )
    return TransformRule("datetime", pattern=spec, has_time=has_time)


def _parse_decimal(spec: Any, item_label: str, type_name: str) -> TransformRule:
    if type_name != "decimal":
        raise TransformConfigError(
            f"{item_label}: decimal is only valid for decimal fields"
        )
    if not isinstance(spec, dict):
        raise TransformConfigError(
            f"{item_label}: decimal needs an options object; use an empty object for defaults"
        )
    unknown = set(spec) - {"places", "dot"}
    if unknown:
        raise TransformConfigError(
            f"{item_label}: unknown decimal option(s): " + ", ".join(sorted(unknown))
        )
    places = spec.get("places")
    if places is not None and (
        isinstance(places, bool)
        or not isinstance(places, int)
        or not 0 <= places <= _MAX_DECIMAL_PLACES
    ):
        raise TransformConfigError(
            f"{item_label}: places must be an integer from 0 to {_MAX_DECIMAL_PLACES}"
        )
    dot = spec.get("dot", True)
    if not isinstance(dot, bool):
        raise TransformConfigError(f"{item_label}: dot must be true or false")
    return TransformRule("decimal", places=places, dot_decimal=dot)


def _parse_alias(
    spec: Any,
    item_label: str,
    field: str,
    type_name: str,
    enum_values: tuple[str, ...],
) -> TransformRule:
    if type_name != "enum":
        raise TransformConfigError(f"{item_label}: alias is only valid for enum fields")
    if not isinstance(spec, dict) or not spec:
        raise TransformConfigError(
            f"{item_label}: alias needs a mapping of source values to declared values"
        )
    for source, target in spec.items():
        if not isinstance(source, str) or not source:
            raise TransformConfigError(
                f"{item_label}: alias source values must be non-empty strings"
            )
        if not isinstance(target, str) or target not in enum_values:
            raise TransformConfigError(
                f"{item_label}: alias target {target!r} is not a declared value of {field}"
            )
    return TransformRule("alias", aliases=dict(spec))


def normalize_record(
    value: Any,
    transforms: Mapping[str, tuple[TransformRule, ...]],
) -> tuple[Any, tuple[str, ...], tuple[TransformFailure, ...]]:
    """Apply configured rules to one raw record before validation.

    Returns the normalized record, the names of fields whose values changed,
    and one failure per field a rule could not convert. A record with failures
    is returned unchanged so the caller can report the original input.
    """

    if not isinstance(value, Mapping):
        return value, (), ()
    failures: list[TransformFailure] = []
    changed: list[str] = []
    normalized: dict[str, Any] | None = None
    for field_name, rules in transforms.items():
        if field_name not in value:
            continue
        original = value[field_name]
        current = original
        for rule in rules:
            try:
                current = _apply_rule(rule, current)
            except TransformValueError as exc:
                failures.append(
                    TransformFailure(field_name, rule.label(), str(exc), original)
                )
                break
        else:
            if current != original:
                if normalized is None:
                    normalized = dict(value)
                normalized[field_name] = current
                changed.append(field_name)
    if failures:
        return value, (), tuple(failures)
    if normalized is None:
        return value, (), ()
    return normalized, tuple(changed), ()


def _apply_rule(rule: TransformRule, value: Any) -> Any:
    if rule.kind == "trim":
        return value.strip() if isinstance(value, str) else value
    if rule.kind == "lowercase":
        return value.lower() if isinstance(value, str) else value
    if rule.kind == "uppercase":
        return value.upper() if isinstance(value, str) else value
    if rule.kind == "datetime":
        return _apply_datetime(rule, value)
    if rule.kind == "decimal":
        return _apply_decimal(rule, value)
    if isinstance(value, str):
        return rule.aliases.get(value, value)
    return value


def _apply_datetime(rule: TransformRule, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    assert rule.pattern is not None
    try:
        parsed = datetime.strptime(value, rule.pattern)
    except ValueError as exc:
        raise TransformValueError(
            f"{value!r} does not match the pattern {rule.pattern!r}"
        ) from exc
    return parsed.isoformat() if rule.has_time else parsed.date().isoformat()


def _apply_decimal(rule: TransformRule, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    decimal_separator = "." if rule.dot_decimal else ","
    group_separator = "," if rule.dot_decimal else "."
    body = value.strip()
    sign = ""
    if body[:1] in {"+", "-"}:
        sign, body = body[0], body[1:]
    if body.count(decimal_separator) > 1:
        raise TransformValueError("more than one decimal separator")
    integer_part, separator, fraction = body.partition(decimal_separator)
    if group_separator in fraction:
        raise TransformValueError("group separator inside the fractional part")
    groups = integer_part.split(group_separator)
    if any(not group.isdigit() for group in groups):
        raise TransformValueError("non-digit characters in the integer part")
    if len(groups) > 1 and not (
        1 <= len(groups[0]) <= 3 and all(len(group) == 3 for group in groups[1:])
    ):
        raise TransformValueError("group separator is not in three-digit groups")
    cleaned = sign + "".join(groups) + ("." + fraction if separator else "")
    if _PLAIN_DECIMAL.fullmatch(cleaned) is None:
        raise TransformValueError(f"{value!r} is not a plain decimal number")
    parsed = Decimal(cleaned)
    if rule.places is not None:
        try:
            parsed = parsed.quantize(
                Decimal(1).scaleb(-rule.places), rounding=ROUND_HALF_UP
            )
        except InvalidOperation as exc:
            raise TransformValueError(
                f"cannot round {value!r} to {rule.places} decimal places"
            ) from exc
    return str(parsed)
