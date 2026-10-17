"""Record-at-a-time validation pipeline for AirLock DB inputs."""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Generator
from unicodedata import category
from uuid import UUID

from pydantic import BaseModel, ValidationError

from .constraints import ValidationConstraints
from .ingestion import InputRecord, iter_source_records
from .schemas import FinancialTransaction


logger = logging.getLogger(__name__)
_MAX_PREVIEW_CHARS = 300
_MAX_LOGGED_RECORD_ERRORS = 100
_MAX_SERIALIZED_DEPTH = 128
_NO_TYPE_CONVERSION_NOTE = "No automatic type conversion was applied."


class DriftCategory(str, Enum):
    """Broad failure classes used by migration summaries and reports."""

    MISSING_FIELD = "missing_field"
    EXTRA_FIELD = "extra_field"
    TYPE_MISMATCH = "type_mismatch"
    NEW_ENUM_VALUE = "new_enum_value"
    VALUE_CONSTRAINT = "value_constraint"
    SOURCE_ERROR = "source_error"


_SCHEMA_DRIFT_CATEGORIES = frozenset(
    {
        DriftCategory.MISSING_FIELD,
        DriftCategory.EXTRA_FIELD,
        DriftCategory.TYPE_MISMATCH,
        DriftCategory.NEW_ENUM_VALUE,
    }
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A JSON-safe description of one failed field or source record."""

    code: str
    category: DriftCategory
    message: str
    location: tuple[str | int, ...] = ()
    input_value: Any = None
    context: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "location": list(self.location),
            "message": self.message,
            "input": self.input_value,
            "context": self.context,
        }


@dataclass(frozen=True, slots=True)
class MigrationRecordResult:
    """The validation result for one input row or one source-level failure."""

    source: str
    record_number: int | None
    raw_record: Any = None
    transaction: BaseModel | None = None
    issues: tuple[ValidationIssue, ...] = ()
    source_incomplete: bool = False

    def __post_init__(self) -> None:
        if (self.transaction is None and not self.issues) or (
            self.transaction is not None and self.issues
        ):
            raise ValueError("a result must contain either a transaction or one or more issues")

    @property
    def is_valid(self) -> bool:
        return self.transaction is not None and not self.issues

    @property
    def has_schema_drift(self) -> bool:
        return any(issue.category in _SCHEMA_DRIFT_CATEGORIES for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "record_number": self.record_number,
            "valid": self.is_valid,
            "source_incomplete": self.source_incomplete,
            "input": _to_json_serializable(self.raw_record),
            "transaction": (
                self.transaction.model_dump(mode="json")
                if self.transaction is not None
                else None
            ),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class ValidationMetrics:
    """Incremental counts for later reports.

    Row totals exclude source-level statuses. ``file_errors`` counts
    source-level issue records that did not stop parsing; ``incomplete_files``
    counts sources where parsing stopped before the end. Drift and error counts
    represent issue occurrences, so one invalid row can contribute more than
    one count.
    """

    records_seen: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    file_errors: int = 0
    incomplete_files: int = 0
    drift_counts: Counter[str] = field(default_factory=Counter)
    error_counts: Counter[str] = field(default_factory=Counter)

    def observe(self, result: MigrationRecordResult) -> None:
        is_file_error = (
            result.record_number is None
            and any(issue.category is DriftCategory.SOURCE_ERROR for issue in result.issues)
        )
        if is_file_error and result.source_incomplete:
            self.incomplete_files += 1
        elif is_file_error:
            self.file_errors += 1
        else:
            self.records_seen += 1
            if result.is_valid:
                self.valid_records += 1
            else:
                self.invalid_records += 1
        for issue in result.issues:
            self.error_counts[issue.code] += 1
            if issue.category in _SCHEMA_DRIFT_CATEGORIES:
                self.drift_counts[issue.category.value] += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_seen": self.records_seen,
            "valid_records": self.valid_records,
            "invalid_records": self.invalid_records,
            "file_errors": self.file_errors,
            "incomplete_files": self.incomplete_files,
            "drift_counts": dict(sorted(self.drift_counts.items())),
            "error_counts": dict(sorted(self.error_counts.items())),
        }


def _to_json_safe(value: Any, depth: int = 0) -> Any:
    """Convert validation input values to bounded, JSON-serializable data."""

    if depth > 8:
        return "<maximum nesting depth reached>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        # Every Cc, Cf, and Cs code point is non-printable, so a printable
        # string has nothing to escape and its preview is a plain slice.
        if value.isprintable():
            if len(value) > _MAX_PREVIEW_CHARS:
                return value[:_MAX_PREVIEW_CHARS] + "... [truncated]"
            return value
        rendered_parts: list[str] = []
        rendered_size = 0
        truncated = False
        for char in value:
            escaped = _escape_unsafe_unicode_char(char)
            if rendered_size + len(escaped) > _MAX_PREVIEW_CHARS:
                truncated = True
                break
            rendered_parts.append(escaped)
            rendered_size += len(escaped)
        rendered = "".join(rendered_parts)
        return rendered + "... [truncated]" if truncated else rendered
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return _to_json_safe(value.value, depth + 1)
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                safe["<truncated>"] = f"{len(value) - index} more item(s)"
                break
            safe_key = _to_json_safe(str(key), depth + 1)
            safe[str(safe_key)] = _to_json_safe(item, depth + 1)
        return safe
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [_to_json_safe(item, depth + 1) for item in value[:100]]
        if len(value) > 100:
            items.append(f"<truncated {len(value) - 100} more item(s)>")
        return items
    try:
        return _to_json_safe(repr(value), depth + 1)
    except Exception:
        return f"<{type(value).__name__}>"


def _escape_unsafe_unicode_char(char: str) -> str:
    """Render control, format, and surrogate code points as JSON-safe text."""

    if category(char) not in {"Cc", "Cf", "Cs"}:
        return char
    codepoint = ord(char)
    return f"\\u{codepoint:04x}" if codepoint <= 0xFFFF else f"\\U{codepoint:08x}"


def _escape_unsafe_unicode(value: str) -> str:
    # Cc, Cf, and Cs code points are all non-printable, so a printable string
    # is returned unchanged instead of rebuilt one character at a time.
    if value.isprintable():
        return value
    return "".join(_escape_unsafe_unicode_char(char) for char in value)


def _to_json_serializable(
    value: Any,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    """Convert raw input without preview truncation, guarding pathological cycles."""

    if depth > _MAX_SERIALIZED_DEPTH:
        return "<maximum nesting depth reached>"
    if isinstance(value, Enum):
        return _to_json_serializable(value.value, depth + 1, seen)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _escape_unsafe_unicode(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        visited = seen if seen is not None else set()
        marker = id(value)
        if marker in visited:
            return "<cyclic reference>"
        visited.add(marker)
        try:
            return {
                _escape_unsafe_unicode(str(key)): _to_json_serializable(
                    item, depth + 1, visited
                )
                for key, item in value.items()
            }
        finally:
            visited.remove(marker)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        visited = seen if seen is not None else set()
        marker = id(value)
        if marker in visited:
            return "<cyclic reference>"
        visited.add(marker)
        try:
            return [_to_json_serializable(item, depth + 1, visited) for item in value]
        finally:
            visited.remove(marker)
    try:
        return repr(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _category_for_error(code: str) -> DriftCategory:
    if code == "missing":
        return DriftCategory.MISSING_FIELD
    if code == "extra_forbidden":
        return DriftCategory.EXTRA_FIELD
    if code == "enum":
        return DriftCategory.NEW_ENUM_VALUE
    if code.endswith("_type") or code.endswith("_parsing"):
        return DriftCategory.TYPE_MISMATCH
    return DriftCategory.VALUE_CONSTRAINT


def _validation_issues(error: ValidationError) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for item in error.errors(include_url=False):
        location = tuple(
            part if isinstance(part, (str, int)) else str(part)
            for part in item.get("loc", ())
        )
        code = str(item.get("type", "validation_error"))
        category = _category_for_error(code)
        message = str(_to_json_safe(item.get("msg", "Validation failed")))
        if category is DriftCategory.TYPE_MISMATCH:
            message = f"{message.rstrip('.')}. {_NO_TYPE_CONVERSION_NOTE}"
        issues.append(
            ValidationIssue(
                code=code,
                category=category,
                message=message,
                location=location,
                input_value=(
                    None if code == "missing" else _to_json_safe(item.get("input"))
                ),
                context=_to_json_serializable(item.get("ctx"))
                if item.get("ctx") is not None
                else None,
            )
        )
    return tuple(issues)


def _source_result(item: InputRecord) -> MigrationRecordResult:
    assert item.issue is not None
    return MigrationRecordResult(
        source=item.source,
        record_number=item.record_number,
        issues=(
            ValidationIssue(
                code=item.issue.code,
                category=DriftCategory.SOURCE_ERROR,
                message=str(_to_json_safe(item.issue.message)),
                input_value=_to_json_safe(item.issue.input_value),
            ),
        ),
        source_incomplete=item.issue.incomplete,
    )


def _configured_constraint_issues(
    transaction: FinancialTransaction,
    constraints: ValidationConstraints | None,
) -> tuple[ValidationIssue, ...]:
    if constraints is None:
        return ()

    issues: list[ValidationIssue] = []
    choices = (
        ("currency", transaction.currency, constraints.allowed_currencies),
        ("account_id", transaction.account_id, constraints.allowed_account_ids),
        (
            "transaction_type",
            transaction.transaction_type.value,
            constraints.allowed_transaction_types,
        ),
        ("status", transaction.status.value, constraints.allowed_statuses),
    )
    for field_name, value, allowed in choices:
        if allowed is not None and value not in allowed:
            issues.append(
                ValidationIssue(
                    code="configured_constraint",
                    category=DriftCategory.VALUE_CONSTRAINT,
                    message=f"{field_name} is not included in the configured allowed values",
                    location=(field_name,),
                    input_value=value,
                )
            )
    if constraints.min_amount is not None and transaction.amount < constraints.min_amount:
        issues.append(
            ValidationIssue(
                code="configured_constraint",
                category=DriftCategory.VALUE_CONSTRAINT,
                message=f"amount is below the configured minimum of {constraints.min_amount}",
                location=("amount",),
                input_value=str(transaction.amount),
            )
        )
    if constraints.max_amount is not None and transaction.amount > constraints.max_amount:
        issues.append(
            ValidationIssue(
                code="configured_constraint",
                category=DriftCategory.VALUE_CONSTRAINT,
                message=f"amount exceeds the configured maximum of {constraints.max_amount}",
                location=("amount",),
                input_value=str(transaction.amount),
            )
        )
    return tuple(issues)


def validate_record_value(
    value: Any,
    *,
    model: type[BaseModel] = FinancialTransaction,
    constraints: ValidationConstraints | None = None,
) -> tuple[BaseModel | None, tuple[ValidationIssue, ...]]:
    """Validate one candidate value using the same schema and rules as ingestion."""

    try:
        record = model.__pydantic_validator__.validate_python(value)
    except ValidationError as exc:
        return None, _validation_issues(exc)
    if constraints is not None and not isinstance(record, FinancialTransaction):
        raise TypeError("business constraints are only supported by the built-in schema")
    issues = _configured_constraint_issues(record, constraints)
    if issues:
        return None, issues
    return record, ()


def _validate_input(
    item: InputRecord,
    constraints: ValidationConstraints | None = None,
    source_labeler: Callable[[str], str] | None = None,
    model: type[BaseModel] = FinancialTransaction,
) -> MigrationRecordResult:
    if item.issue is not None:
        return _source_result(item)

    try:
        transaction, issues = validate_record_value(
            item.value, model=model, constraints=constraints
        )
    except Exception as exc:
        source = source_labeler(item.source) if source_labeler else item.source
        if source_labeler is None:
            logger.exception(
                "Unexpected validation failure for %s record %s",
                source,
                item.record_number,
            )
        else:
            logger.error(
                "Unexpected validation failure for %s record %s (%s)",
                source,
                item.record_number,
                type(exc).__name__,
            )
        message = f"Unexpected {type(exc).__name__} while validating this record."
        return MigrationRecordResult(
            source=item.source,
            record_number=item.record_number,
            raw_record=item.value,
            issues=(
                ValidationIssue(
                    code="unexpected_validation_error",
                    category=DriftCategory.SOURCE_ERROR,
                    message=message,
                    input_value=_to_json_safe(item.value),
                ),
            ),
        )

    return MigrationRecordResult(
        source=item.source,
        record_number=item.record_number,
        raw_record=item.value,
        transaction=transaction if not issues else None,
        issues=issues,
    )


def iter_validated_records(
    sources: str | Path | Iterable[str | Path],
    *,
    metrics: ValidationMetrics | None = None,
    constraints: ValidationConstraints | None = None,
    source_labeler: Callable[[str], str] | None = None,
    model: type[BaseModel] = FinancialTransaction,
    excluded_paths: Iterable[str | Path] = (),
) -> Generator[MigrationRecordResult, None, None]:
    """Validate records from one or more files/directories without stopping on bad rows.

    Consumers receive one result at a time. Valid models are available on
    ``result.transaction``; invalid row and file failures carry structured
    issues with Pydantic locations and schema-drift categories. If ``metrics``
    is supplied, it is updated before each result is yielded.
    """

    source_list: Iterable[str | Path]
    if isinstance(sources, (str, Path)):
        source_list = (sources,)
    else:
        source_list = sources
    excluded_path_list = tuple(excluded_paths)

    logged_errors = 0
    suppressed_errors = 0
    try:
        for source in source_list:
            records = iter_source_records(
                source,
                source_labeler=source_labeler,
                excluded_paths=excluded_path_list,
            )
            try:
                for item in records:
                    result = _validate_input(
                        item, constraints, source_labeler, model
                    )
                    if not result.is_valid:
                        if logged_errors < _MAX_LOGGED_RECORD_ERRORS:
                            codes = ",".join(issue.code for issue in result.issues)
                            mismatch_fields = tuple(
                                dict.fromkeys(
                                    ".".join(str(part) for part in issue.location)
                                    for issue in result.issues
                                    if issue.category is DriftCategory.TYPE_MISMATCH
                                    and issue.location
                                )
                            )
                            has_type_mismatch = any(
                                issue.category is DriftCategory.TYPE_MISMATCH
                                for issue in result.issues
                            )
                            if mismatch_fields:
                                mismatch_note = (
                                    f" (type mismatch in {', '.join(mismatch_fields)}; "
                                    "no automatic conversion applied)"
                                )
                            elif has_type_mismatch:
                                mismatch_note = (
                                    " (type mismatch; no automatic conversion applied)"
                                )
                            else:
                                mismatch_note = ""
                            logger.warning(
                                "Validation issue for %s record %s: %s%s",
                                source_labeler(result.source)
                                if source_labeler is not None
                                else result.source,
                                result.record_number if result.record_number is not None else "(file)",
                                codes,
                                mismatch_note,
                            )
                            logged_errors += 1
                        else:
                            suppressed_errors += 1
                    if metrics is not None:
                        metrics.observe(result)
                    yield result
            finally:
                records.close()
    finally:
        if suppressed_errors:
            logger.warning(
                "Suppressed %s additional per-record validation log entries; "
                "full results remain available from the iterator and metrics.",
                suppressed_errors,
            )
