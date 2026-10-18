"""Load fixed-width record layouts and split positional text records."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_io import ConfigLoadError, load_config_object

_MAX_LAYOUT_BYTES = 256 * 1024
_MAX_LAYOUT_FIELDS = 256
_ALLOWED_LAYOUT_KEYS = frozenset({"encoding", "skip_lines", "fields"})
_ALLOWED_FIELD_KEYS = frozenset({"name", "start", "length"})


class FixedWidthConfigError(ValueError):
    """Raised when a fixed-width layout file is invalid."""


@dataclass(frozen=True, slots=True)
class FixedWidthField:
    """One named 1-based span of a fixed-width record."""

    name: str
    start: int
    length: int

    @property
    def end(self) -> int:
        """The 1-based inclusive end column of the span."""

        return self.start + self.length - 1

    def slice(self, line: str) -> str:
        """Return this span of one text line, padding cut off."""

        return line[self.start - 1 : self.end].strip()


@dataclass(frozen=True, slots=True)
class FixedWidthLayout:
    """One record layout: an encoding, optional banner lines, and spans."""

    encoding: str
    skip_lines: int
    fields: tuple[FixedWidthField, ...]

    @property
    def record_length(self) -> int:
        """The column the last span ends at."""

        return self.fields[-1].end

    def split(self, line: str) -> dict[str, str | None]:
        """Split one text line into field values.

        An all-padding span becomes None, matching how the CSV reader treats
        an empty cell.
        """

        return {
            field.name: (field.slice(line) or None) for field in self.fields
        }

    @classmethod
    def from_file(cls, path: str | Path) -> FixedWidthLayout:
        """Load a JSON or YAML layout and validate its spans."""

        try:
            raw = load_config_object(
                path,
                label="fixed-width layout",
                max_bytes=_MAX_LAYOUT_BYTES,
            )
        except ConfigLoadError as exc:
            raise FixedWidthConfigError(str(exc)) from exc
        try:
            return _compile_layout(raw)
        except FixedWidthConfigError:
            raise
        except Exception as exc:
            raise FixedWidthConfigError(
                f"invalid fixed-width layout {path}: {exc}"
            ) from exc


def _compile_layout(raw: Any) -> FixedWidthLayout:
    if not isinstance(raw, dict):
        raise FixedWidthConfigError(
            "fixed-width layout must be a JSON or YAML object"
        )
    unknown = set(raw) - _ALLOWED_LAYOUT_KEYS
    if unknown:
        raise FixedWidthConfigError(
            "unknown fixed-width layout key(s): " + ", ".join(sorted(unknown))
        )
    encoding = raw.get("encoding", "utf-8-sig")
    if not isinstance(encoding, str) or not encoding:
        raise FixedWidthConfigError(
            "fixed-width layout encoding must be a non-empty string"
        )
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise FixedWidthConfigError(f"unknown encoding {encoding!r}") from exc
    skip_lines = raw.get("skip_lines", 0)
    if (
        isinstance(skip_lines, bool)
        or not isinstance(skip_lines, int)
        or skip_lines < 0
    ):
        raise FixedWidthConfigError(
            "fixed-width layout skip_lines must be a non-negative integer"
        )
    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, list) or not fields_raw:
        raise FixedWidthConfigError(
            "fixed-width layout needs a non-empty fields list"
        )
    if len(fields_raw) > _MAX_LAYOUT_FIELDS:
        raise FixedWidthConfigError(
            f"fixed-width layout has more than {_MAX_LAYOUT_FIELDS} fields"
        )
    fields: list[FixedWidthField] = []
    seen: set[str] = set()
    previous_end = 0
    for position, entry in enumerate(fields_raw):
        label = f"fields[{position}]"
        if not isinstance(entry, dict):
            raise FixedWidthConfigError(
                f"{label} must be an object with name, start, and length"
            )
        unknown = set(entry) - _ALLOWED_FIELD_KEYS
        if unknown:
            raise FixedWidthConfigError(
                f"{label} has unknown key(s): " + ", ".join(sorted(unknown))
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise FixedWidthConfigError(f"{label} needs a non-empty name")
        if name in seen:
            raise FixedWidthConfigError(
                f"{label} repeats the field name {name!r}"
            )
        start = entry.get("start")
        if isinstance(start, bool) or not isinstance(start, int) or start < 1:
            raise FixedWidthConfigError(
                f"{label} start must be an integer of 1 or more"
            )
        length = entry.get("length")
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or length < 1
        ):
            raise FixedWidthConfigError(
                f"{label} length must be an integer of 1 or more"
            )
        if start <= previous_end:
            raise FixedWidthConfigError(
                f"{label} starts at {start} and overlaps or precedes the "
                f"span ending at column {previous_end}"
            )
        seen.add(name)
        fields.append(FixedWidthField(name, start, length))
        previous_end = start + length - 1
    return FixedWidthLayout(
        encoding=encoding,
        skip_lines=skip_lines,
        fields=tuple(fields),
    )
