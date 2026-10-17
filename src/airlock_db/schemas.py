"""Strict target schemas for AirLock DB migration records.

The boundary accepts a small set of predictable legacy representations (for
example, UUIDs and decimal amounts encoded as strings) and normalizes them.
Unknown fields and ambiguous or lossy values remain validation errors so the
migration pipeline can report them as schema drift.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any
from unicodedata import category
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


_ISO_DATETIME_PATTERN = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{3}|\.\d{6})?(?:[Zz]|[+-]\d{2}:\d{2})\Z"
)


class TransactionType(str, Enum):
    """Supported transaction directions in the target system."""

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"
    REFUND = "REFUND"


class TransactionStatus(str, Enum):
    """Supported lifecycle states in the target system."""

    PENDING = "PENDING"
    POSTED = "POSTED"
    REVERSED = "REVERSED"


AccountId = Annotated[
    str,
    Field(
        strict=True,
        min_length=3,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9_-]{2,31}$",
        description="Uppercase source account identifier.",
    ),
]
CurrencyCode = Annotated[
    str,
    Field(
        strict=True,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
        description="Three-letter uppercase currency code.",
    ),
]
CounterpartyName = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=120),
]
Description = Annotated[
    str,
    Field(strict=True, max_length=500),
]
PostedAt = Annotated[
    datetime,
    Field(strict=True, description="Timezone-aware ISO-8601 timestamp, normalized to UTC."),
]
TransactionAmount = Annotated[
    Decimal,
    Field(
        strict=True,
        gt=Decimal("0"),
        max_digits=14,
        decimal_places=2,
        description="Positive amount with at most 14 digits and 2 decimal places.",
    ),
]


def _clean_text(value: Any) -> Any:
    """Trim strings while refusing hidden control and format characters."""

    if not isinstance(value, str):
        return value
    if value.isprintable():
        return value.strip()
    # Non-printable values either hold refused characters (control, format,
    # surrogate) or accepted ones such as unassigned or private-use code
    # points. Every non-printable ASCII character is a control code point, so
    # the ASCII branch needs no category scan.
    if value.isascii() or any(category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError("must not contain control, format, or surrogate characters")
    return value.strip()


class FinancialTransaction(BaseModel):
    """Validated transaction row ready for target storage.

    String-encoded UUIDs, ISO-8601 timestamps, and decimal amounts are
    normalized at the input boundary. Strings used as identifiers or enum
    values are trimmed; currencies and enum values are case-normalized.
    """

    model_config = ConfigDict(
        extra="forbid",
        # Every string field trims through ``_clean_text`` in its before
        # validator, so the core-level strip would only repeat that work.
        validate_assignment=True,
    )

    transaction_id: UUID
    account_id: AccountId
    posted_at: PostedAt
    amount: TransactionAmount
    currency: CurrencyCode
    transaction_type: TransactionType
    status: TransactionStatus
    counterparty: CounterpartyName
    description: Description | None = None

    @field_validator("transaction_id", mode="before")
    @classmethod
    def parse_transaction_id(cls, value: Any) -> Any:
        value = _clean_text(value)
        if isinstance(value, str):
            try:
                return UUID(value)
            except (ValueError, AttributeError) as exc:
                raise ValueError("must be a valid UUID") from exc
        return value

    @field_validator("posted_at", mode="before")
    @classmethod
    def parse_posted_at(cls, value: Any) -> Any:
        value = _clean_text(value)
        if not isinstance(value, str):
            return value

        if _ISO_DATETIME_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "must use YYYY-MM-DDTHH:MM:SS with an optional fraction and timezone"
            )
        iso_value = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(iso_value)
        except ValueError as exc:
            raise ValueError("must be an ISO-8601 datetime with a timezone") from exc

        # The full-match pattern guarantees a timezone designator, and
        # fromisoformat rejects offsets it cannot represent, so the parsed
        # value always carries a usable timezone here. The after-validator
        # still guards datetime instances that skip this transformation.
        return parsed.astimezone(timezone.utc)

    @field_validator("amount", mode="before")
    @classmethod
    def parse_amount(cls, value: Any) -> Any:
        value = _clean_text(value)
        if isinstance(value, bool):
            raise ValueError("must be a decimal amount, not a boolean")
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, int):
            parsed = Decimal(value)
        elif isinstance(value, float):
            parsed = Decimal(str(value))
        elif isinstance(value, str):
            if "_" in value:
                raise ValueError("must not contain numeric separators")
            try:
                parsed = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("must be a valid decimal amount") from exc
        else:
            return value

        if not parsed.is_finite():
            raise ValueError("must be a finite decimal amount")
        return parsed

    @field_validator("account_id", mode="before")
    @classmethod
    def clean_account_id(cls, value: Any) -> Any:
        return _clean_text(value)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> Any:
        value = _clean_text(value)
        return value.upper() if isinstance(value, str) else value

    @field_validator("transaction_type", "status", mode="before")
    @classmethod
    def normalize_enum_values(cls, value: Any) -> Any:
        value = _clean_text(value)
        return value.upper() if isinstance(value, str) else value

    @field_validator("counterparty", mode="before")
    @classmethod
    def clean_counterparty(cls, value: Any) -> Any:
        return _clean_text(value)

    @field_validator("description", mode="before")
    @classmethod
    def clean_description(cls, value: Any) -> Any:
        value = _clean_text(value)
        if value == "":
            return None
        return value

    @field_validator("posted_at")
    @classmethod
    def require_timezone_and_normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is timezone.utc:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone offset")
        return value.astimezone(timezone.utc)
