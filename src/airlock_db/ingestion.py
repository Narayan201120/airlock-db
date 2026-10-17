"""Record-at-a-time readers for JSON, JSONL, and XML migration inputs."""

from __future__ import annotations

import codecs
import json
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, TextIO


logger = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = {".json", ".jsonl", ".ndjson", ".xml"}
_XML_RECORD_TAGS = {"transaction", "record", "row", "item", "entry", "payment"}
_JSON_CHUNK_SIZE = 64 * 1024
_JSONL_LINE_DELIMITER = re.compile(rb"\r\n|\r|\n")
# These are per-record ceilings: file size and total row count remain unbounded.
# Keep them low because Python object graphs can be much larger than source text.
_MAX_JSON_RECORD_CHARS = 512 * 1024
_MAX_JSONL_RECORD_BYTES = 512 * 1024
_MAX_XML_RECORD_CHARS = 512 * 1024
_MAX_XML_RECORD_BYTES = 4 * 1024 * 1024
_MAX_XML_RECORD_NODES = 10_000
_MAX_XML_DEPTH = 64
_MAX_XML_EVENT_GAP_BYTES = 4 * 1024 * 1024
_MAX_XML_DOCUMENT_DEPTH = 128
_MAX_SOURCE_DIRECTORIES = 10_000
_MAX_DIRECTORY_CHILDREN = 10_000
_XML_PARENT_CLEAR_BATCH = 128
_MAX_DUPLICATE_KEY_PREVIEW_CHARS = 120
_MAX_SOURCE_PREVIEW_CHARS = 300
_MAX_FAST_JSON_OBJECT_PAIRS = 128
_INCOMPLETE_JSON_LITERAL_PREFIXES = frozenset(
    {
        "t", "tr", "tru", "f", "fa", "fal", "fals", "n", "nu", "nul",
        "N", "Na", "I", "In", "Inf", "Infi", "Infin", "Infini",
        "Infinit", "-I", "-In", "-Inf", "-Infi", "-Infin",
        "-Infini", "-Infinit", "-Infinity", "-",
    }
)
_INCOMPLETE_JSON_NUMBER = re.compile(
    r"(?:^|[\s,:\[])(?:-|-?(?:0|[1-9][0-9]*)\.|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?[eE][+-]?)\Z"
)
_INCOMPLETE_JSON_NUMBER_SUFFIX = re.compile(r"(?:\.|[eE][+-]?)\Z")


class DuplicateJSONKeyError(ValueError):
    """Raised when an object repeats a key instead of silently overwriting it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.incomplete_array: bool | None = None


class InvalidJSONConstantError(ValueError):
    """Raised when JSON uses a non-standard NaN or Infinity constant."""

    def __init__(self, value: str) -> None:
        super().__init__(f"non-standard JSON constant {value!r}")
        self.incomplete_array: bool | None = None


class JSONTruncatedError(ValueError):
    """Raised when JSON ends before the current value is complete."""


class JSONRecordTooLargeError(ValueError):
    """Raised when one JSON/JSONL record exceeds the configured read bound."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.incomplete_array: bool | None = None


class XMLRecordTooLargeError(ValueError):
    """Raised when an XML record exceeds parser or conversion limits."""


class XMLPrologTooLargeError(ValueError):
    """Raised when the XML prolog exceeds the bounded DTD inspection window."""


class XMLSubtreeTooLargeError(ValueError):
    """Raised when non-record XML content exceeds a streaming read bound."""


def _json_decode_needs_more(error: JSONDecodeError, text: str, start: int) -> bool:
    """Identify JSON decoder errors that a later stream chunk can complete."""

    relative_position = error.pos - start
    available_length = len(text) - start
    if relative_position >= available_length:
        return True
    if "unterminated string" in error.msg.casefold():
        return True
    if error.msg == "Expecting value":
        return text[error.pos:] in _INCOMPLETE_JSON_LITERAL_PREFIXES
    if error.msg == "Expecting ',' delimiter":
        fragment = text[start:]
        match = _INCOMPLETE_JSON_NUMBER.search(fragment)
        return bool(match and match.start() <= relative_position <= match.end())
    return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Most source records are small. Let CPython build their dictionaries in C,
    # while keeping the incremental path for large objects to bound duplicate
    # detection work and temporary memory on malformed rows.
    if len(pairs) <= _MAX_FAST_JSON_OBJECT_PAIRS:
        result = dict(pairs)
        if len(result) == len(pairs):
            return result
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                preview = key[:_MAX_DUPLICATE_KEY_PREVIEW_CHARS]
                suffix = "..." if len(key) > len(preview) else ""
                raise DuplicateJSONKeyError(
                    f"duplicate JSON object key {preview!r}{suffix}"
                )
            seen.add(key)
        raise AssertionError("duplicate keys were not found in the JSON object")

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            preview = key[:_MAX_DUPLICATE_KEY_PREVIEW_CHARS]
            suffix = "..." if len(key) > len(preview) else ""
            raise DuplicateJSONKeyError(
                f"duplicate JSON object key {preview!r}{suffix}"
            )
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class SourceIssue:
    """An input, encoding, or parsing problem associated with one source."""

    code: str
    message: str
    input_value: Any = None
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class InputRecord:
    """One decoded input value, or a recoverable issue for that input row."""

    source: str
    record_number: int | None
    value: Any = None
    issue: SourceIssue | None = None


@dataclass(frozen=True, slots=True)
class DatabaseSource:
    """One table of a local SQLite database, used as a read-only input source."""

    path: Path
    table: str | None = None


class _JSONStream:
    """Incrementally decode one JSON value at a time from a text stream."""

    def __init__(self, stream: TextIO, chunk_size: int = _JSON_CHUNK_SIZE) -> None:
        self._stream = stream
        self._chunk_size = chunk_size
        self._buffer = ""
        self._position = 0
        self._eof = False
        self._decoder = json.JSONDecoder(
            parse_float=Decimal,
            parse_constant=self._reject_constant,
            object_pairs_hook=_unique_object,
        )

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise InvalidJSONConstantError(value)

    def _fill(self) -> None:
        if self._position:
            self._buffer = self._buffer[self._position :]
            self._position = 0
        chunk = self._stream.read(self._chunk_size)
        if chunk:
            self._buffer += chunk
        else:
            self._eof = True

    def _skip_whitespace(self) -> None:
        while True:
            while self._position < len(self._buffer) and self._buffer[self._position].isspace():
                self._position += 1
            if self._position < len(self._buffer) or self._eof:
                return
            self._fill()

    def peek(self) -> str | None:
        self._skip_whitespace()
        if self._position >= len(self._buffer):
            return None
        return self._buffer[self._position]

    def consume(self, expected: str) -> None:
        actual = self.peek()
        if actual != expected:
            raise JSONDecodeError(
                f"expected {expected!r}", self._buffer[self._position :], 0
            )
        self._position += 1

    def parse_value(self) -> Any:
        self._skip_whitespace()
        value_start = self._position
        while True:
            available_length = len(self._buffer) - value_start
            if available_length <= _MAX_JSON_RECORD_CHARS + 1:
                decode_text = self._buffer
                decode_start = value_start
                bounded_slice = False
            else:
                decode_text = self._buffer[
                    value_start : value_start + _MAX_JSON_RECORD_CHARS + 1
                ]
                decode_start = 0
                bounded_slice = True

            try:
                value, decoded_end = self._decoder.raw_decode(
                    decode_text, decode_start
                )
            except JSONDecodeError as exc:
                needs_more = _json_decode_needs_more(
                    exc, decode_text, decode_start
                )
                local_error = JSONDecodeError(
                    exc.msg,
                    decode_text[decode_start:],
                    exc.pos - decode_start,
                )
                if needs_more and available_length > _MAX_JSON_RECORD_CHARS:
                    raise JSONRecordTooLargeError(
                        f"JSON record exceeds {_MAX_JSON_RECORD_CHARS} characters"
                    ) from exc
                if self._eof:
                    if exc.pos >= len(decode_text) or "unterminated string" in exc.msg.casefold():
                        raise JSONTruncatedError(str(local_error)) from exc
                    raise local_error from exc
                if not needs_more:
                    raise local_error from exc
                self._fill()
                value_start = self._position
                continue

            value_length = decoded_end if bounded_slice else decoded_end - value_start
            if value_length > _MAX_JSON_RECORD_CHARS:
                raise JSONRecordTooLargeError(
                    f"JSON record exceeds {_MAX_JSON_RECORD_CHARS} characters"
                )
            if (
                not self._eof
                and type(value) in {int, Decimal}
                and _INCOMPLETE_JSON_NUMBER_SUFFIX.fullmatch(
                    decode_text[decoded_end:]
                )
            ):
                self._fill()
                value_start = self._position
                continue
            if (
                not self._eof
                and decoded_end == len(decode_text)
                and type(value) in {int, Decimal, bool, type(None)}
            ):
                # A scalar at the current boundary may continue as a number, or
                # may need its delimiter before its syntax can be judged.
                self._fill()
                value_start = self._position
                continue
            self._position = value_start + decoded_end if bounded_slice else decoded_end
            return value

    def finish(self) -> None:
        trailing = self.peek()
        if trailing is not None:
            raise JSONDecodeError(
                "unexpected content after JSON value",
                self._buffer[self._position :],
                0,
            )


# A single, reused decoder avoids rebuilding JSONDecoder configuration for every
# JSONL/NDJSON record while preserving Decimal, constant rejection, and hooks.
_JSONL_JSON_DECODER = json.JSONDecoder(
    parse_float=Decimal,
    parse_constant=_JSONStream._reject_constant,
    object_pairs_hook=_unique_object,
)


def _iter_json_values(stream: TextIO) -> Generator[Any, None, None]:
    """Yield a top-level JSON array one member at a time, or a single JSON value."""

    reader = _JSONStream(stream)
    first = reader.peek()
    if first is None:
        raise JSONDecodeError("empty JSON input", "", 0)

    if first != "[":
        try:
            value = reader.parse_value()
        except (InvalidJSONConstantError, DuplicateJSONKeyError, JSONRecordTooLargeError) as exc:
            exc.incomplete_array = False
            raise
        yield value
        reader.finish()
        return

    reader.consume("[")
    next_character = reader.peek()
    if next_character == "]":
        reader.consume("]")
        reader.finish()
        return

    while True:
        try:
            value = reader.parse_value()
        except (InvalidJSONConstantError, DuplicateJSONKeyError, JSONRecordTooLargeError) as exc:
            exc.incomplete_array = True
            raise
        yield value
        separator = reader.peek()
        if separator is None:
            raise JSONDecodeError(
                "unexpected end of JSON array",
                reader._buffer[reader._position :],
                0,
            )
        if separator == "]":
            reader.consume("]")
            reader.finish()
            return
        if separator != ",":
            raise JSONDecodeError(
                "expected ',' or ']' after array member",
                reader._buffer[reader._position :],
                0,
            )
        reader.consume(",")


def _source_issue(
    path: Path,
    record_number: int | None,
    code: str,
    exc: Exception,
    *,
    input_value: Any = None,
    incomplete: bool = False,
) -> InputRecord:
    try:
        message = str(exc)
    except Exception:
        message = type(exc).__name__
    return InputRecord(
        source=str(path),
        record_number=record_number,
        issue=SourceIssue(
            code=code,
            message=message,
            input_value=input_value,
            incomplete=incomplete,
        ),
    )


def _iter_json_file(
    path: Path,
    *,
    source_labeler: Callable[[str], str] | None = None,
) -> Generator[InputRecord, None, None]:
    number = 0
    try:
        with path.open("r", encoding="utf-8-sig", errors="strict") as stream:
            for value in _iter_json_values(stream):
                number += 1
                yield InputRecord(str(path), number, value=value)
        if number == 0:
            yield InputRecord(
                str(path),
                None,
                issue=SourceIssue("no_records", "JSON input contains no records."),
            )
    except UnicodeError as exc:
        yield _source_issue(path, None, "encoding_error", exc, incomplete=number > 0)
    except OSError as exc:
        yield _source_issue(path, None, "io_error", exc, incomplete=number > 0)
    except DuplicateJSONKeyError as exc:
        yield _source_issue(path, number + 1, "duplicate_json_key", exc)
        if exc.incomplete_array:
            yield _source_issue(
                path,
                None,
                "json_parse_aborted",
                ValueError(f"JSON parsing stopped; remaining array records were not processed: {exc}"),
                incomplete=True,
            )
    except JSONRecordTooLargeError as exc:
        yield _source_issue(path, number + 1, "record_too_large", exc)
        if exc.incomplete_array:
            yield _source_issue(
                path,
                None,
                "json_parse_aborted",
                ValueError(f"JSON parsing stopped; remaining array records were not processed: {exc}"),
                incomplete=True,
            )
    except InvalidJSONConstantError as exc:
        yield _source_issue(path, number + 1, "invalid_json_constant", exc)
        if exc.incomplete_array:
            yield _source_issue(
                path,
                None,
                "json_parse_aborted",
                ValueError(
                    f"JSON parsing stopped; remaining array records were not processed: {exc}"
                ),
                incomplete=True,
            )
    except JSONTruncatedError as exc:
        yield _source_issue(
            path,
            None,
            "json_parse_aborted",
            exc,
            incomplete=True,
        )
    except (JSONDecodeError, ValueError, RecursionError) as exc:
        if number == 0:
            yield _source_issue(path, None, "json_decode_error", exc)
        else:
            yield _source_issue(
                path,
                None,
                "json_parse_aborted",
                ValueError(f"JSON parsing stopped; remaining records were not processed: {exc}"),
                incomplete=True,
            )
    except Exception as exc:
        if source_labeler is None:
            logger.exception("Unexpected JSON reader failure for %s", path)
        else:
            logger.error(
                "Unexpected JSON reader failure for %s (%s)",
                source_labeler(str(path)),
                type(exc).__name__,
            )
        yield _source_issue(
            path, None, "json_reader_error", exc, incomplete=number > 0
        )


def _iter_bounded_jsonl_lines(
    stream: Any,
) -> Generator[tuple[bytes | None, int, bytes, bool], None, None]:
    """Split LF, CRLF, and bare-CR lines while bounding each retained line."""

    line_parts: list[bytes] = []
    preview = bytearray()
    line_size = 0
    oversized = False
    line_has_bytes = False
    skip_lf = False

    def append_segment(segment: bytes) -> None:
        nonlocal line_size, oversized, line_has_bytes
        if not segment:
            return
        line_has_bytes = True
        line_size += len(segment)
        if len(preview) < _MAX_SOURCE_PREVIEW_CHARS:
            preview.extend(segment[: _MAX_SOURCE_PREVIEW_CHARS - len(preview)])
        if not oversized:
            if line_size <= _MAX_JSONL_RECORD_BYTES:
                line_parts.append(segment)
            else:
                oversized = True
                line_parts.clear()

    def finish_line() -> tuple[bytes | None, int, bytes, bool]:
        nonlocal line_parts, preview, line_size, oversized, line_has_bytes
        raw = None if oversized else b"".join(line_parts)
        result = (raw, line_size, bytes(preview), oversized)
        line_parts = []
        preview = bytearray()
        line_size = 0
        oversized = False
        line_has_bytes = False
        return result

    while True:
        chunk = stream.read(_JSON_CHUNK_SIZE)
        if not chunk:
            break
        if skip_lf:
            if chunk.startswith(b"\n"):
                chunk = chunk[1:]
            skip_lf = False
        if not chunk:
            continue

        segment_start = 0
        for delimiter in _JSONL_LINE_DELIMITER.finditer(chunk):
            append_segment(chunk[segment_start : delimiter.start()])
            if delimiter.group() == b"\r" and delimiter.end() == len(chunk):
                skip_lf = True
            yield finish_line()
            segment_start = delimiter.end()
        append_segment(chunk[segment_start:])

    if line_has_bytes:
        yield finish_line()


def _iter_json_lines(
    path: Path,
    *,
    source_labeler: Callable[[str], str] | None = None,
) -> Generator[InputRecord, None, None]:
    line_number = 0
    saw_record_line = False
    bom_pending = True
    try:
        with path.open("rb") as stream:
            for raw_line, line_size, preview_bytes, oversized in _iter_bounded_jsonl_lines(stream):
                line_number += 1
                if bom_pending and line_size:
                    if raw_line is not None and raw_line.startswith(codecs.BOM_UTF8):
                        raw_line = raw_line[len(codecs.BOM_UTF8) :]
                    bom_pending = False
                preview = preview_bytes.decode("utf-8", errors="replace")
                if line_size > _MAX_SOURCE_PREVIEW_CHARS:
                    preview += "... [truncated]"
                if oversized:
                    saw_record_line = True
                    yield InputRecord(
                        str(path),
                        line_number,
                        issue=SourceIssue(
                            "record_too_large",
                            f"JSONL record exceeds {_MAX_JSONL_RECORD_BYTES} bytes",
                            input_value=preview,
                        ),
                    )
                    continue
                try:
                    assert raw_line is not None
                    line = raw_line.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    saw_record_line = True
                    yield _source_issue(
                        path,
                        line_number,
                        "encoding_error",
                        exc,
                        input_value=preview,
                    )
                    continue
                if not line.strip():
                    continue
                saw_record_line = True
                try:
                    value = _JSONL_JSON_DECODER.decode(line)
                except DuplicateJSONKeyError as exc:
                    yield _source_issue(
                        path, line_number, "duplicate_json_key", exc, input_value=preview
                    )
                    continue
                except InvalidJSONConstantError as exc:
                    yield _source_issue(
                        path,
                        line_number,
                        "invalid_json_constant",
                        exc,
                        input_value=preview,
                    )
                    continue
                except (JSONDecodeError, ValueError, RecursionError) as exc:
                    yield _source_issue(
                        path, line_number, "json_decode_error", exc, input_value=preview
                    )
                    continue
                yield InputRecord(str(path), line_number, value=value)
        if not saw_record_line:
            yield InputRecord(
                str(path),
                None,
                issue=SourceIssue("no_records", "JSONL input contains no records."),
            )
    except UnicodeError as exc:
        yield _source_issue(path, None, "encoding_error", exc, incomplete=line_number > 0)
    except OSError as exc:
        yield _source_issue(path, None, "io_error", exc, incomplete=line_number > 0)
    except Exception as exc:
        if source_labeler is None:
            logger.exception("Unexpected JSONL reader failure for %s", path)
        else:
            logger.error(
                "Unexpected JSONL reader failure for %s (%s)",
                source_labeler(str(path)),
                type(exc).__name__,
            )
        yield _source_issue(
            path, None, "json_reader_error", exc, incomplete=line_number > 0
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _is_directory_link(path: Path) -> bool:
    """Detect symlink and junction directories, including on older Windows."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True

    # Python versions before Path.is_junction() still resolve Windows
    # junctions through realpath. Compare with the resolved parent so a user
    # selecting a symlinked root does not make every child look linked.
    expected = os.path.join(os.path.realpath(path.parent), path.name)
    return os.path.normcase(os.path.realpath(path)) != os.path.normcase(expected)


def _namespace_uri(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def _xml_field_name(
    name: str,
    record_namespace: str,
    *,
    is_attribute: bool = False,
) -> str:
    if name.startswith("{") and "}" in name:
        namespace, local = name[1:].split("}", 1)
        if namespace == record_namespace and not is_attribute:
            return local.casefold()
        return f"{{{namespace}}}{local.casefold()}"
    return name.casefold()


def _insert_xml_value(target: dict[str, Any], key: str, value: Any) -> None:
    if key not in target:
        target[key] = value
    elif isinstance(target[key], list):
        target[key].append(value)
    else:
        target[key] = [target[key], value]


def _xml_element_value(
    element: ET.Element,
    *,
    record_namespace: str,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    if budget is None:
        budget = [0, 0]
    if depth > _MAX_XML_DEPTH:
        raise XMLRecordTooLargeError(f"XML record exceeds {_MAX_XML_DEPTH} nested levels")
    budget[0] += 1
    if budget[0] > _MAX_XML_RECORD_NODES:
        raise XMLRecordTooLargeError(f"XML record exceeds {_MAX_XML_RECORD_NODES} elements")

    def count_text(text: str | None) -> None:
        if text is not None:
            budget[1] += len(text)
            if budget[1] > _MAX_XML_RECORD_CHARS:
                raise XMLRecordTooLargeError(
                    f"XML record exceeds {_MAX_XML_RECORD_CHARS} characters"
                )

    children = list(element)
    if not children and not element.attrib:
        count_text(element.text)
        return element.text

    value: dict[str, Any] = {}
    for name, text in element.attrib.items():
        count_text(name)
        count_text(text)
        _insert_xml_value(
            value,
            _xml_field_name(name, record_namespace, is_attribute=True),
            text,
        )
    if element.text and element.text.strip():
        count_text(element.text)
        value["#text"] = element.text
    for child in children:
        _insert_xml_value(
            value,
            _xml_field_name(child.tag, record_namespace),
            _xml_element_value(
                child,
                record_namespace=record_namespace,
                depth=depth + 1,
                budget=budget,
            ),
        )
        if child.tail and child.tail.strip():
            count_text(child.tail)
            _insert_xml_value(value, "#text", child.tail)
    return value


def _discard_xml_element(
    element: ET.Element,
    parent: ET.Element | None,
    stack: list[ET.Element],
    cleanup_counts: list[int],
) -> None:
    """Clear completed content and amortize removal from its parent's child list."""

    element.clear()
    if parent is None:
        return
    try:
        parent_index = stack.index(parent)
    except ValueError:
        return
    cleanup_counts[parent_index] += 1
    if cleanup_counts[parent_index] >= _XML_PARENT_CLEAR_BATCH:
        stack[parent_index].clear()
        cleanup_counts[parent_index] = 0


def _xml_text_issue(path: Path, value: str | None, message: str) -> InputRecord | None:
    if not value or not value.strip():
        return None
    preview = value[:_MAX_SOURCE_PREVIEW_CHARS]
    if len(value) > _MAX_SOURCE_PREVIEW_CHARS:
        preview += "... [truncated]"
    return InputRecord(
        str(path),
        None,
        issue=SourceIssue("xml_text_outside_record", message, input_value=preview),
    )


class _BoundedXMLReader:
    """Bound record bytes and parser event gaps.

    ElementTree delivers events after reading a parser chunk, so the record byte
    limit can include up to one read of read-ahead slack.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.bytes_read = 0
        self.record_start: int | None = None
        self.record_nodes = 0
        self.event_mark = 0

    def mark_event(self) -> None:
        self.event_mark = self.bytes_read

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self.bytes_read += len(chunk)
        if (
            self.record_start is not None
            and self.bytes_read - self.record_start > _MAX_XML_RECORD_BYTES
        ):
            raise XMLRecordTooLargeError(
                f"XML record exceeds {_MAX_XML_RECORD_BYTES} source bytes"
            )
        if self.bytes_read - self.event_mark > _MAX_XML_EVENT_GAP_BYTES:
            if self.record_start is not None:
                raise XMLRecordTooLargeError(
                    "XML record contains a text or markup token exceeding "
                    f"{_MAX_XML_EVENT_GAP_BYTES} source bytes"
                )
            raise XMLSubtreeTooLargeError(
                "XML text or markup token exceeds "
                f"{_MAX_XML_EVENT_GAP_BYTES} source bytes"
            )
        return chunk


def _iter_xml_file(
    path: Path,
    *,
    source_labeler: Callable[[str], str] | None = None,
) -> Generator[InputRecord, None, None]:
    record_number = 0
    stack: list[ET.Element] = []
    cleanup_counts: list[int] = []
    outside_text_checked: list[bool] = []
    pending_outside_tails: list[tuple[ET.Element, ET.Element | None, str]] = []
    record_depth = 0
    saw_xml_element = False
    try:
        with path.open("rb") as stream:
            try:
                has_doctype = _xml_prolog_contains_doctype(stream)
            except UnicodeError as exc:
                yield _source_issue(path, None, "encoding_error", exc)
                return
            if has_doctype:
                yield InputRecord(
                    str(path),
                    None,
                    issue=SourceIssue(
                        "xml_doctype_forbidden",
                        "XML documents with DOCTYPE declarations are not accepted.",
                    ),
                )
                return
            reader = _BoundedXMLReader(stream)
            for event, element in ET.iterparse(reader, events=("start", "end")):
                for tail_element, parent, message in pending_outside_tails:
                    issue = _xml_text_issue(path, tail_element.tail, message)
                    _discard_xml_element(tail_element, parent, stack, cleanup_counts)
                    if issue is not None:
                        yield issue
                pending_outside_tails.clear()
                if event == "start":
                    saw_xml_element = True
                    if stack and record_depth == 0 and not outside_text_checked[-1]:
                        outside_text_checked[-1] = True
                        issue = _xml_text_issue(
                            path,
                            stack[-1].text,
                            "Non-whitespace text appears outside an XML record.",
                        )
                        if issue is not None:
                            yield issue
                    stack.append(element)
                    cleanup_counts.append(0)
                    outside_text_checked.append(False)
                    is_record_tag = _local_name(element.tag) in _XML_RECORD_TAGS
                    has_record_ancestor = record_depth > 0
                    is_record_start = is_record_tag and not has_record_ancestor
                    if len(stack) > _MAX_XML_DOCUMENT_DEPTH:
                        error = (
                            XMLRecordTooLargeError(
                                "XML parser depth safety limit "
                                f"({_MAX_XML_DOCUMENT_DEPTH} levels) was exceeded inside "
                                "a record; remaining source rows cannot be processed"
                            )
                            if reader.record_start is not None or is_record_start
                            else XMLSubtreeTooLargeError(
                                "XML document exceeds the parser depth safety limit "
                                f"({_MAX_XML_DOCUMENT_DEPTH} levels)"
                            )
                        )
                        raise error
                    if is_record_start:
                        reader.record_start = reader.bytes_read
                        reader.record_nodes = 1
                    elif reader.record_start is not None and has_record_ancestor:
                        reader.record_nodes += 1
                        if reader.record_nodes > _MAX_XML_RECORD_NODES:
                            raise XMLRecordTooLargeError(
                                f"XML record exceeds {_MAX_XML_RECORD_NODES} elements"
                            )
                    if is_record_tag:
                        record_depth += 1
                    reader.mark_event()
                    continue

                tag = _local_name(element.tag)
                is_record_tag = tag in _XML_RECORD_TAGS
                is_record = is_record_tag and record_depth == 1
                has_record_ancestor = record_depth > (1 if is_record_tag else 0)
                outside_text = (
                    element.text
                    if (
                        not has_record_ancestor
                        and not is_record_tag
                        and not outside_text_checked[-1]
                    )
                    else None
                )
                if is_record:
                    record_number += 1
                    try:
                        value = _xml_element_value(
                            element,
                            record_namespace=_namespace_uri(element.tag),
                        )
                    except XMLRecordTooLargeError as exc:
                        result = _source_issue(path, record_number, "record_too_large", exc)
                    else:
                        result = InputRecord(str(path), record_number, value=value)
                    pending_outside_tails.append(
                        (
                            element,
                            stack[-2] if len(stack) > 1 else None,
                            "Non-whitespace text follows an XML record.",
                        )
                    )
                elif not has_record_ancestor:
                    # Discard completed wrappers outside records so unknown or
                    # deeply nested XML does not accumulate as a full tree.
                    pending_outside_tails.append(
                        (
                            element,
                            stack[-2] if len(stack) > 1 else None,
                            "Non-whitespace text appears between XML elements outside a record.",
                        )
                    )

                if stack:
                    stack.pop()
                    cleanup_counts.pop()
                    outside_text_checked.pop()
                if is_record_tag:
                    record_depth -= 1
                if is_record:
                    reader.record_start = None
                    reader.record_nodes = 0
                reader.mark_event()
                if is_record:
                    yield result
                elif outside_text and outside_text.strip():
                    text_preview = outside_text[:_MAX_SOURCE_PREVIEW_CHARS]
                    if len(outside_text) > _MAX_SOURCE_PREVIEW_CHARS:
                        text_preview += "... [truncated]"
                    yield InputRecord(
                        str(path),
                        None,
                        issue=SourceIssue(
                            "xml_text_outside_record",
                            "Non-whitespace text appears outside an XML record.",
                            input_value=text_preview,
                        ),
                    )
            for tail_element, parent, message in pending_outside_tails:
                issue = _xml_text_issue(path, tail_element.tail, message)
                _discard_xml_element(tail_element, parent, stack, cleanup_counts)
                if issue is not None:
                    yield issue
    except XMLRecordTooLargeError as exc:
        yield _source_issue(path, record_number + 1, "record_too_large", exc)
        yield _source_issue(
            path,
            None,
            "xml_parse_aborted",
            ValueError(
                f"XML parsing stopped; remaining records were not processed: {exc}"
            ),
            incomplete=True,
        )
        return
    except ET.ParseError as exc:
        message = str(exc).casefold()
        reached_unexpected_eof = saw_xml_element and any(
            marker in message
            for marker in ("no element found", "unexpected end", "unclosed token", "unclosed cdata")
        )
        if record_number == 0 and not reached_unexpected_eof:
            yield _source_issue(path, None, "xml_parse_error", exc)
        else:
            yield _source_issue(
                path,
                None,
                "xml_parse_aborted",
                ValueError(
                    f"XML parsing stopped; remaining records were not processed: {exc}"
                ),
                incomplete=True,
            )
        return
    except XMLPrologTooLargeError as exc:
        yield _source_issue(path, None, "xml_prolog_too_large", exc)
        return
    except XMLSubtreeTooLargeError as exc:
        yield _source_issue(
            path,
            None,
            "xml_parse_aborted",
            ValueError(
                f"XML parsing stopped; remaining records were not processed: {exc}"
            ),
            incomplete=True,
        )
        return
    except UnicodeError as exc:
        yield _source_issue(path, None, "encoding_error", exc, incomplete=record_number > 0)
        return
    except OSError as exc:
        yield _source_issue(path, None, "io_error", exc, incomplete=record_number > 0)
        return
    except Exception as exc:
        if source_labeler is None:
            logger.exception("Unexpected XML reader failure for %s", path)
        else:
            logger.error(
                "Unexpected XML reader failure for %s (%s)",
                source_labeler(str(path)),
                type(exc).__name__,
            )
        yield _source_issue(
            path, None, "xml_reader_error", exc, incomplete=record_number > 0
        )
        return

    if record_number == 0:
        yield InputRecord(
            str(path),
            None,
            issue=SourceIssue(
                "no_records",
                "XML input contains no transaction, record, row, item, entry, or payment elements.",
            ),
        )


def _xml_prolog_contains_doctype(stream: Any) -> bool:
    """Reject a DTD in the prolog without scanning document text or CDATA."""

    max_prolog_bytes = 1024 * 1024
    prefix = stream.read(max_prolog_bytes)
    stream.seek(0)
    if prefix.startswith(b"\x00\x00\xfe\xff"):
        text = prefix[4:].decode("utf-32-be", errors="strict")
    elif prefix.startswith(b"\xff\xfe\x00\x00"):
        text = prefix[4:].decode("utf-32-le", errors="strict")
    elif prefix.startswith(b"\xfe\xff"):
        text = prefix[2:].decode("utf-16-be", errors="strict")
    elif prefix.startswith(b"\xff\xfe"):
        text = prefix[2:].decode("utf-16-le", errors="strict")
    elif prefix.startswith(b"\xef\xbb\xbf"):
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text = decoder.decode(prefix[3:], final=False)
    elif prefix.startswith(b"\x00\x00\x00<"):
        text = prefix.decode("utf-32-be", errors="strict")
    elif prefix.startswith(b"<\x00\x00\x00"):
        text = prefix.decode("utf-32-le", errors="strict")
    elif prefix.startswith(b"\x00<\x00"):
        text = prefix.decode("utf-16-be", errors="strict")
    elif prefix.startswith(b"<\x00"):
        text = prefix.decode("utf-16-le", errors="strict")
    else:
        # XML markup tokens are ASCII-compatible in the remaining common encodings.
        text = prefix.decode("latin-1")

    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if text.startswith("<?", position):
            end = text.find("?>", position + 2)
            if end < 0:
                if len(prefix) == max_prolog_bytes:
                    raise XMLPrologTooLargeError(
                        f"XML prolog exceeds {max_prolog_bytes} bytes"
                    )
                return False
            position = end + 2
            continue
        if text.startswith("<!--", position):
            end = text.find("-->", position + 4)
            if end < 0:
                if len(prefix) == max_prolog_bytes:
                    raise XMLPrologTooLargeError(
                        f"XML prolog exceeds {max_prolog_bytes} bytes"
                    )
                return False
            position = end + 3
            continue
        if text.startswith("<!DOCTYPE", position):
            return True
        if text[position] == "<":
            return False
        return False
    if len(prefix) == max_prolog_bytes:
        raise XMLPrologTooLargeError(
            f"XML prolog exceeds {max_prolog_bytes} bytes"
        )
    return False


def _normalized_source_path(path: str | Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def _iter_source_paths(
    path: Path,
    excluded_paths: frozenset[str],
) -> Generator[Path | InputRecord, None, None]:
    try:
        if path.is_file():
            if _normalized_source_path(path) in excluded_paths:
                yield InputRecord(
                    str(path), None,
                    issue=SourceIssue(
                        "generated_output_excluded",
                        "Generated migration output is excluded from input records.",
                    ),
                )
                return
            if path.suffix.casefold() in SUPPORTED_SUFFIXES:
                yield path
            else:
                yield InputRecord(
                    str(path), None,
                    issue=SourceIssue("unsupported_file_type", f"Unsupported input suffix: {path.suffix or '(none)'}"),
                )
            return
        if not path.is_dir():
            yield InputRecord(
                str(path), None,
                issue=SourceIssue("source_not_found", "Input path does not exist or is not accessible."),
            )
            return
    except OSError as exc:
        yield _source_issue(path, None, "io_error", exc)
        return

    pending = [path]
    visited_directories: set[str] = set()
    found_supported_file = False
    while pending:
        current = pending.pop()
        try:
            resolved = os.path.normcase(os.path.realpath(current))
        except OSError as exc:
            yield _source_issue(current, None, "io_error", exc)
            continue
        if resolved in visited_directories:
            continue
        if len(visited_directories) >= _MAX_SOURCE_DIRECTORIES:
            yield InputRecord(
                str(current),
                None,
                issue=SourceIssue(
                    "directory_limit",
                    f"Directory scan stopped at {_MAX_SOURCE_DIRECTORIES} directories.",
                ),
            )
            break
        visited_directories.add(resolved)
        try:
            children: list[Path] = []
            too_many_children = False
            with os.scandir(current) as entries:
                for entry in entries:
                    if len(children) >= _MAX_DIRECTORY_CHILDREN:
                        too_many_children = True
                        break
                    children.append(Path(entry.path))
        except OSError as exc:
            yield _source_issue(current, None, "io_error", exc)
            continue
        if too_many_children:
            yield InputRecord(
                str(current),
                None,
                issue=SourceIssue(
                    "directory_entry_limit",
                    f"Directory has more than {_MAX_DIRECTORY_CHILDREN} entries and was skipped.",
                ),
            )
            continue
        children.sort(key=lambda item: item.name.casefold())

        child_directories: list[Path] = []
        for child in children:
            try:
                if child.is_dir() and _is_directory_link(child):
                    yield InputRecord(
                        str(child),
                        None,
                        issue=SourceIssue(
                            "directory_link_skipped",
                            "Directory symbolic link or junction was skipped; linked directories are not traversed.",
                        ),
                    )
                    continue
                if child.is_symlink():
                    if child.suffix.casefold() in SUPPORTED_SUFFIXES:
                        found_supported_file = True
                        yield InputRecord(
                            str(child),
                            None,
                            issue=SourceIssue(
                                "symlink_skipped",
                                "Symbolic link was skipped while scanning the input directory.",
                            ),
                        )
                    continue
                if child.is_dir():
                    child_directories.append(child)
                elif child.is_file() and child.suffix.casefold() in SUPPORTED_SUFFIXES:
                    if _normalized_source_path(child) in excluded_paths:
                        continue
                    found_supported_file = True
                    yield child
            except OSError as exc:
                yield _source_issue(child, None, "io_error", exc)
        available_slots = _MAX_SOURCE_DIRECTORIES - len(visited_directories) - len(pending)
        if len(child_directories) > available_slots:
            yield InputRecord(
                str(current),
                None,
                issue=SourceIssue(
                    "directory_limit",
                    f"Directory scan is limited to {_MAX_SOURCE_DIRECTORIES} directories.",
                ),
            )
            child_directories = child_directories[:max(0, available_slots)]
        pending.extend(reversed(child_directories))

    if not found_supported_file:
        yield InputRecord(
            str(path), None,
            issue=SourceIssue("no_supported_files", "No supported JSON, JSONL, NDJSON, or XML files were found."),
        )


def _quote_sql_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _iter_database_records(
    source: DatabaseSource,
    *,
    source_labeler: Callable[[str], str] | None = None,
) -> Generator[InputRecord, None, None]:
    """Yield rows from one table of a local SQLite database, one at a time.

    The database is opened read-only. Column names act exactly like JSON
    object keys: missing columns are missing-field errors, extra columns are
    extra-field errors, and values pass through the same strict validation.
    """

    path = Path(source.path)
    base_label = str(path)
    if not path.is_file():
        yield InputRecord(
            base_label,
            None,
            issue=SourceIssue(
                "source_db_missing", f"database file was not found: {base_label}"
            ),
        )
        return

    connection: sqlite3.Connection | None = None
    record_number = 0
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if source.table is not None:
            if source.table not in tables:
                yield InputRecord(
                    base_label,
                    None,
                    issue=SourceIssue(
                        "source_db_table_missing",
                        f"table {source.table!r} was not found in {base_label}",
                    ),
                )
                return
            chosen = source.table
        elif len(tables) == 1:
            chosen = tables[0]
        elif not tables:
            yield InputRecord(
                base_label,
                None,
                issue=SourceIssue(
                    "source_db_empty", f"no tables were found in {base_label}"
                ),
            )
            return
        else:
            preview = ", ".join(tables[:20])
            yield InputRecord(
                base_label,
                None,
                issue=SourceIssue(
                    "source_db_table_required",
                    f"multiple tables found in {base_label}; "
                    f"pass --source-table to choose one: {preview}",
                ),
            )
            return

        label = f"{base_label} ({chosen})"
        if source_labeler is not None:
            label = source_labeler(label)
        cursor = connection.execute(f"SELECT * FROM {_quote_sql_identifier(chosen)}")
        columns = [item[0] for item in cursor.description or ()]
        for row in cursor:
            record_number += 1
            yield InputRecord(label, record_number, value=dict(zip(columns, row)))
    except (sqlite3.Error, OSError) as exc:
        yield InputRecord(
            base_label,
            None,
            issue=SourceIssue(
                "source_db_error",
                f"SQLite read failed: {type(exc).__name__}: {exc}",
                incomplete=record_number > 0,
            ),
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def iter_source_records(
    source: str | Path | DatabaseSource,
    *,
    source_labeler: Callable[[str], str] | None = None,
    excluded_paths: Iterable[str | Path] = (),
) -> Generator[InputRecord, None, None]:
    """Yield decoded records from one file, directory, or database table.

    JSON arrays are decoded incrementally. JSONL/NDJSON syntax errors are
    isolated to their physical line so subsequent lines can still be read.
    File-level and database-level failures are yielded as records with an
    attached ``issue``.
    """

    if isinstance(source, DatabaseSource):
        yield from _iter_database_records(source, source_labeler=source_labeler)
        return

    path = Path(source)
    excluded = frozenset(_normalized_source_path(item) for item in excluded_paths)
    for entry in _iter_source_paths(path, excluded):
        if isinstance(entry, InputRecord):
            yield entry
            continue
        suffix = entry.suffix.casefold()
        if suffix == ".json":
            records = _iter_json_file(entry, source_labeler=source_labeler)
        elif suffix in {".jsonl", ".ndjson"}:
            records = _iter_json_lines(entry, source_labeler=source_labeler)
        elif suffix == ".xml":
            records = _iter_xml_file(entry, source_labeler=source_labeler)
        else:
            continue
        try:
            for record in records:
                yield record
        finally:
            records.close()
