"""AirLock DB schema and local-first migration utilities."""

from .schemas import FinancialTransaction, TransactionStatus, TransactionType
from .ingestion import InputRecord, SourceIssue, iter_source_records
from .pipeline import (
    DriftCategory,
    MigrationRecordResult,
    ValidationIssue,
    ValidationMetrics,
    iter_validated_records,
)
from .schema_config import DynamicSchema, SchemaConfigError

__all__ = [
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
