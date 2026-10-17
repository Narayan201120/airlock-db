"""Run-scoped pseudonymization for financial migration output."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable

from pydantic import BaseModel

from .schemas import FinancialTransaction

BUILTIN_ANONYMIZED_FIELDS: tuple[str, ...] = (
    "account_id",
    "counterparty",
    "description",
)


class RunAnonymizer:
    """Pseudonymize identifiers with a random key that lives only for this run.

    Equal values in the same field map to the same token during one migration,
    which keeps joins useful. A new key is generated for every process run and
    is never written to the database or report. The transaction UUID is retained
    as the target's idempotency key; account IDs, counterparties, and descriptions
    are pseudonymized.
    """

    __slots__ = ("__key",)

    def __init__(self) -> None:
        self.__key = secrets.token_bytes(32)

    def _digest(self, field: str, value: str) -> str:
        message = field.encode("utf-8") + b"\0" + value.encode("utf-8")
        return hmac.new(self.__key, message, hashlib.sha256).hexdigest()

    def source_label(self, source: str) -> str:
        """Return a run-scoped label without exposing the original path."""

        return f"source-{self._digest('source', source)[:16]}"

    def transaction(self, value: FinancialTransaction) -> FinancialTransaction:
        """Return a storage copy with identity-bearing strings pseudonymized."""

        updates = {
            name: value.__dict__[name]
            for name in FinancialTransaction.model_fields
        }
        updates.update({
            "account_id": "A" + self._digest("account_id", value.account_id)[:31].upper(),
            "counterparty": "CP-" + self._digest("counterparty", value.counterparty)[:32].upper(),
            "description": (
                "DESC-" + self._digest("description", value.description)[:48].upper()
                if value.description is not None
                else None
            ),
        })
        # This object already passed FinancialTransaction validation, and each
        # token below is deliberately shaped to satisfy the corresponding
        # field constraints. Supplying every declared field preserves the
        # field-set behavior of the previous dump/validate round trip.
        anonymized = value.model_copy(update=updates)
        # Copy only declared model fields, as model_dump() did. This also avoids
        # carrying manually-added attributes into a strict target record.
        anonymized.__dict__.clear()
        anonymized.__dict__.update(updates)
        return anonymized

    def configured_record(
        self,
        value: BaseModel,
        sensitive_fields: Iterable[str],
    ) -> BaseModel:
        """Return a dynamic model copy with declared sensitive strings tokenized."""

        model_fields = type(value).model_fields
        updates = {name: getattr(value, name) for name in model_fields}
        fields = frozenset(name for name in sensitive_fields if name in model_fields)
        for name in fields:
            raw_value = updates[name]
            if raw_value is None:
                continue
            if type(raw_value) is not str:
                raise ValueError(f"sensitive field {name!r} must contain a string")
            updates[name] = self._digest(name, raw_value)[:32].upper()
        # DynamicSchema permits anonymization only for strings without regex or
        # enum constraints and whose length limits accept a 32-character token.
        # All declared fields are included to preserve dump/validate field-set
        # behavior; unknown instance attributes cannot enter this update map.
        anonymized = value.model_copy(update=updates)
        anonymized.__dict__.clear()
        anonymized.__dict__.update(updates)
        return anonymized
