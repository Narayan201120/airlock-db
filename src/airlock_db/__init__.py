"""AirLock DB schema and local-first migration utilities.

The heavy submodules are exposed lazily: importing this package stays cheap
for entry points like ``--version`` while ``from airlock_db import X`` keeps
working exactly as before (PEP 562).
"""

from __future__ import annotations

from typing import Any

from ._version import __version__

__all__ = [
    "__version__",
    "DriftCategory",
    "DynamicSchema",
    "FinancialTransaction",
    "InputRecord",
    "MigrationRecordResult",
    "SourceIssue",
    "SchemaConfigError",
    "TransactionStatus",
    "TransactionType",
    "ValidationIssue",
    "ValidationMetrics",
    "iter_source_records",
    "iter_validated_records",
]

_LAZY_EXPORTS = {
    "DriftCategory": ".pipeline",
    "DynamicSchema": ".schema_config",
    "FinancialTransaction": ".schemas",
    "InputRecord": ".ingestion",
    "MigrationRecordResult": ".pipeline",
    "SchemaConfigError": ".schema_config",
    "SourceIssue": ".ingestion",
    "TransactionStatus": ".schemas",
    "TransactionType": ".schemas",
    "ValidationIssue": ".pipeline",
    "ValidationMetrics": ".pipeline",
    "iter_source_records": ".ingestion",
    "iter_validated_records": ".pipeline",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_path, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
