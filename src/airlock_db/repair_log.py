"""Bounded JSONL audit log for rejected records and later repair sessions."""

from __future__ import annotations

import json
import hashlib
import itertools
import os
import re
import tempfile
from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator
from uuid import UUID

from .constraints import FieldRule, ValidationConstraints
from .pipeline import DriftCategory, MigrationRecordResult


_FORMAT_NAME = "airlock-db-repair-log"
_FORMAT_VERSION = 2
_MAX_ENTRY_BYTES = 8 * 1024 * 1024
_MAX_LOG_BYTES = 256 * 1024 * 1024
_MAX_LOG_RECORDS = 100_000
_MAX_ISSUES_PER_ENTRY = 100
_TRAILER_RESERVE_BYTES = 1024
_DECIMAL_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")


class RepairLogError(ValueError):
    """Raised when a repair log is malformed, incomplete, or too large."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    raise TypeError(f"unsupported repair log value: {type(value).__name__}")


# Process-unique marker counter for in-flight Decimal literals. Markers never
# reach the file; they are replaced with the raw literals before the write,
# so a cheap counter is enough to keep concurrent markers distinct.
_line_marker_counter = itertools.count()


class _RepairLogCapacityReached(Exception):
    """Signal that the total sidecar size limit would be exceeded."""


def _encode_line(
    value: dict[str, Any],
    destination: BinaryIO,
    *,
    max_total_bytes: int | None = None,
) -> int:
    """Encode one object with a hard line-size limit."""

    decimal_literals: list[str] = []
    prefix = ""

    def encode_default(candidate: Any) -> Any:
        if isinstance(candidate, Decimal):
            literal = str(candidate)
            if not candidate.is_finite() or _DECIMAL_NUMBER.fullmatch(literal) is None:
                raise TypeError("non-finite Decimal cannot be represented in repair JSON")
            nonlocal prefix
            if not prefix:
                prefix = f"__AIRLOCK_DB_DECIMAL_{next(_line_marker_counter)}_"
            marker = f"{prefix}{len(decimal_literals)}__"
            decimal_literals.append(literal)
            return marker
        return _json_default(candidate)

    # One C-level encode instead of a per-chunk Python loop. Inputs are
    # already bounded by the reader ceilings, so the transient string stays
    # small; the 8 MiB cap is enforced on the result below.
    text = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
        default=encode_default,
    )
    if decimal_literals:
        for index, literal in enumerate(decimal_literals):
            marker_json = json.dumps(f"{prefix}{index}__", ensure_ascii=True)
            text = text.replace(marker_json, literal)
    encoded = text.encode("ascii")
    if len(encoded) + 1 > _MAX_ENTRY_BYTES:
        raise RepairLogError("repair log entry exceeds the 8 MiB limit")
    encoded_size = len(encoded) + 1
    if (
        max_total_bytes is not None
        and destination.tell() + encoded_size > max_total_bytes
    ):
        raise _RepairLogCapacityReached
    destination.write(encoded)
    destination.write(b"\n")
    return encoded_size


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RepairLogError(f"duplicate key in repair log entry: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise RepairLogError(f"non-standard JSON constant in repair log: {value}")


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


def _canonical_field_rule(rule: FieldRule) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if rule.field is not None:
        payload["field"] = rule.field
    if rule.enum:
        items = [
            _canonical_decimal(value) if isinstance(value, Decimal) else value
            for value in rule.enum
        ]
        payload["enum"] = sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if rule.min is not None:
        payload["min"] = _canonical_decimal(rule.min)
    if rule.max is not None:
        payload["max"] = _canonical_decimal(rule.max)
    if rule.pattern is not None:
        payload["pattern"] = rule.pattern
    if rule.required_any_of:
        payload["required_any_of"] = sorted(rule.required_any_of)
    return payload


def constraints_fingerprint(constraints: ValidationConstraints | None) -> str:
    if constraints is None:
        return "none"
    payload: dict[str, Any] = {}
    for field in fields(constraints):
        name = field.name
        if name == "field_rules":
            # Old logs predate field rules, so an empty rule set must stay out
            # of the payload and keep every existing digest unchanged.
            if constraints.field_rules:
                payload[name] = sorted(
                    (_canonical_field_rule(rule) for rule in constraints.field_rules),
                    key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
                )
            continue
        value = getattr(constraints, name)
        if isinstance(value, frozenset):
            payload[name] = sorted(value)
        elif isinstance(value, Decimal):
            payload[name] = _canonical_decimal(value)
        else:
            payload[name] = value
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RepairLogWriter:
    """Write rejected records one at a time without buffering the log."""

    def __init__(
        self,
        path: str | Path,
        *,
        schema_name: str,
        schema_fingerprint: str,
        constraints_hash: str,
        anonymized: bool,
        schema_fields: Iterable[str],
        source_labeler: Callable[[str], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.schema_name = schema_name
        self.schema_fingerprint = schema_fingerprint
        self.constraints_hash = constraints_hash
        self.anonymized = anonymized
        self.schema_fields = frozenset(schema_fields)
        self.source_labeler = source_labeler
        if anonymized and source_labeler is None:
            raise ValueError("an anonymized repair log requires a source labeler")
        self._stream: BinaryIO | None = None
        self._temporary_path: Path | None = None
        self._entry_count = 0
        self._omitted_count = 0
        self._truncated = False
        self._closed = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def omitted_records(self) -> int:
        return self._omitted_count

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            delete=False,
        )
        self._stream = temporary
        self._temporary_path = Path(temporary.name)
        header = {
            "kind": "header",
            "format": _FORMAT_NAME,
            "version": _FORMAT_VERSION,
            "schema_name": self.schema_name,
            "schema_fingerprint": self.schema_fingerprint,
            "constraints_fingerprint": self.constraints_hash,
            "anonymized": self.anonymized,
        }
        try:
            _encode_line(header, self._stream)
        except BaseException:
            self._stream.close()
            self._stream = None
            try:
                if self._temporary_path is not None:
                    self._temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._temporary_path = None
            raise

    def _issues(self, result: MigrationRecordResult) -> list[dict[str, Any]]:
        if not self.anonymized:
            traces: list[dict[str, Any]] = []
            for issue in result.issues[:_MAX_ISSUES_PER_ENTRY]:
                trace = issue.to_dict()
                current = result.raw_record
                found = True
                for part in issue.location:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    elif isinstance(current, (list, tuple)) and isinstance(part, int) and 0 <= part < len(current):
                        current = current[part]
                    else:
                        found = False
                        break
                if found and issue.location and issue.code != "missing":
                    trace["input"] = current
                else:
                    trace["input"] = None
                traces.append(trace)
            return traces
        redacted: list[dict[str, Any]] = []
        for issue in result.issues[:_MAX_ISSUES_PER_ENTRY]:
            location = [
                part if isinstance(part, int) else (
                    part if part in self.schema_fields else "<unknown field>"
                )
                for part in issue.location
            ]
            redacted.append(
                {
                    "code": issue.code,
                    "category": issue.category.value,
                    "location": location,
                    "message": "Validation details redacted in anonymize mode.",
                    "input": "[REDACTED]" if issue.input_value is not None else None,
                    "context": None,
                }
            )
        return redacted

    def write_result(self, result: MigrationRecordResult) -> None:
        if self._stream is None or self._closed:
            raise RuntimeError("repair log is not open")
        if result.is_valid:
            return
        if self._truncated or self._entry_count >= _MAX_LOG_RECORDS:
            self._truncated = True
            self._omitted_count += 1
            return
        source = (
            self.source_labeler(result.source)
            if self.anonymized and self.source_labeler is not None
            else result.source
        )
        source = source[:4096]
        repairable = (
            not self.anonymized
            and result.record_number is not None
            and not (
                result.raw_record is None
                and any(issue.category is DriftCategory.SOURCE_ERROR for issue in result.issues)
            )
        )
        issues_truncated = len(result.issues) > _MAX_ISSUES_PER_ENTRY
        entry: dict[str, Any] = {
            "kind": "record",
            "source": source,
            "record_number": result.record_number,
            "repairable": repairable,
            "record": result.raw_record if repairable else None,
            "reason": None if repairable else (
                "raw input omitted because the migration used anonymize mode"
                if self.anonymized
                else "source-level error has no parsed record to repair"
            ),
            "source_incomplete": result.source_incomplete,
            "issues": self._issues(result),
            "issues_truncated": issues_truncated,
        }
        try:
            encoded_size = _encode_line(
                entry,
                self._stream,
                max_total_bytes=_MAX_LOG_BYTES - _TRAILER_RESERVE_BYTES,
            )
        except _RepairLogCapacityReached:
            self._truncated = True
            self._omitted_count += 1
            return
        except (RepairLogError, TypeError, ValueError, RecursionError) as exc:
            entry["repairable"] = False
            entry["record"] = None
            entry["reason"] = (
                "rejected record or error trace exceeds the 8 MiB log-entry limit"
                if isinstance(exc, RepairLogError)
                else "raw input could not be represented safely as JSON"
            )
            entry["issues"] = [
                {
                    "code": issue.code[:100],
                    "category": issue.category.value,
                    "location": [
                        part[:128] if isinstance(part, str) else part
                        for part in issue.location[:32]
                    ],
                    "message": (
                        "Validation details redacted in anonymize mode."
                        if self.anonymized
                        else issue.message[:512]
                    ),
                    "input": "[OMITTED]" if issue.input_value is not None else None,
                    "context": None,
                }
                for issue in result.issues[:_MAX_ISSUES_PER_ENTRY]
            ]
            entry["issues_truncated"] = True
            try:
                encoded_size = _encode_line(
                    entry,
                    self._stream,
                    max_total_bytes=_MAX_LOG_BYTES - _TRAILER_RESERVE_BYTES,
                )
            except _RepairLogCapacityReached:
                self._truncated = True
                self._omitted_count += 1
                return
        self._entry_count += 1

    def finish(self, *, complete: bool) -> None:
        if self._closed:
            return
        if self._stream is None:
            self._closed = True
            return
        try:
            _encode_line(
                {
                    "kind": "end",
                    "records": self._entry_count,
                    "complete": complete,
                    "truncated": self._truncated,
                    "omitted_records": self._omitted_count,
                },
                self._stream,
            )
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None
            if self._temporary_path is None:
                raise RepairLogError("repair log temporary file is unavailable")
            os.replace(self._temporary_path, self.path)
            self._temporary_path = None
        finally:
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
            self._stream = None
            self._closed = True

    def abort(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        self._closed = True
        try:
            if self._temporary_path is not None:
                self._temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._temporary_path = None


class RepairLogReader:
    """Stream and validate a repair log without loading it all into memory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._stream: BinaryIO | None = None
        self.header: dict[str, Any] | None = None
        self.trailer: dict[str, Any] | None = None
        self._record_count = 0
        self._bytes_read = 0
        self._digest = hashlib.sha256()
        self._fingerprint: str | None = None

    def __enter__(self) -> RepairLogReader:
        try:
            self._stream = self.path.open("rb")
            header = self._read_object()
            if (
                header.get("kind") != "header"
                or header.get("format") != _FORMAT_NAME
                or isinstance(header.get("version"), bool)
                or not isinstance(header.get("version"), int)
                or header.get("version") != _FORMAT_VERSION
                or not isinstance(header.get("schema_name"), str)
                or not isinstance(header.get("schema_fingerprint"), str)
                or not isinstance(header.get("constraints_fingerprint"), str)
                or not isinstance(header.get("anonymized"), bool)
            ):
                raise RepairLogError("unsupported or invalid repair log header")
            self.header = header
            return self
        except OSError:
            self.close()
            raise
        except BaseException:
            self.close()
            raise

    def _read_object(self) -> dict[str, Any]:
        if self._stream is None:
            raise RepairLogError("repair log is not open")
        line = self._stream.readline(_MAX_ENTRY_BYTES + 2)
        if not line:
            raise RepairLogError("repair log ended before its completion marker")
        self._digest.update(line)
        self._bytes_read += len(line)
        if self._bytes_read > _MAX_LOG_BYTES:
            raise RepairLogError("repair log exceeds the 256 MiB limit")
        if len(line) > _MAX_ENTRY_BYTES + 1 or not line.endswith(b"\n"):
            raise RepairLogError("repair log contains an oversized or incomplete line")
        try:
            value = json.loads(
                line[:-1].decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=Decimal,
            )
        except RepairLogError:
            raise
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise RepairLogError(f"invalid JSON in repair log: {exc}") from exc
        if not isinstance(value, dict):
            raise RepairLogError("each repair log line must be a JSON object")
        return value

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if self._stream is None:
            raise RepairLogError("repair log is not open")
        while True:
            value = self._read_object()
            kind = value.get("kind")
            if kind == "record":
                if (
                    not isinstance(value.get("source"), str)
                    or (value.get("record_number") is not None
                        and (isinstance(value.get("record_number"), bool)
                             or not isinstance(value.get("record_number"), int)))
                    or not isinstance(value.get("repairable"), bool)
                    or not isinstance(value.get("issues"), list)
                    or len(value["issues"]) > _MAX_ISSUES_PER_ENTRY
                    or not isinstance(value.get("issues_truncated"), bool)
                    or any(not isinstance(issue, dict) for issue in value["issues"])
                ):
                    raise RepairLogError("invalid rejected-record entry in repair log")
                if value["repairable"] and "record" not in value:
                    raise RepairLogError("repairable log entry is missing its record")
                if self._record_count >= _MAX_LOG_RECORDS:
                    raise RepairLogError("repair log exceeds the 100,000 record limit")
                self._record_count += 1
                yield value
                continue
            if kind != "end":
                raise RepairLogError("repair log contains an unknown entry kind")
            if (
                isinstance(value.get("records"), bool)
                or not isinstance(value.get("records"), int)
                or value["records"] != self._record_count
                or not isinstance(value.get("complete"), bool)
                or not isinstance(value.get("truncated"), bool)
                or isinstance(value.get("omitted_records"), bool)
                or not isinstance(value.get("omitted_records"), int)
                or value["omitted_records"] < 0
                or value["truncated"] != (value["omitted_records"] > 0)
            ):
                raise RepairLogError("repair log completion marker does not match its records")
            trailing = self._stream.read(1)
            if trailing:
                self._digest.update(trailing)
                raise RepairLogError("repair log contains data after its completion marker")
            self.trailer = value
            self._fingerprint = self._digest.hexdigest()
            return

    @property
    def fingerprint(self) -> str:
        if self._fingerprint is None:
            raise RepairLogError("repair log fingerprint is not available before full validation")
        return self._fingerprint

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
