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
    }
)
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


@dataclass(frozen=True, slots=True)
class ValidationConstraints:
    """Optional business rules applied after the strict target schema."""

    allowed_currencies: frozenset[str] | None = None
    allowed_account_ids: frozenset[str] | None = None
    allowed_transaction_types: frozenset[str] | None = None
    allowed_statuses: frozenset[str] | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None

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
        )
