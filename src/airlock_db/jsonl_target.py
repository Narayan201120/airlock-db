"""Atomic JSONL target writer for validated migration records."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class JsonlSummary:
    """Result of a committed JSONL target."""

    path: Path
    written: int


class JsonlTargetWriter:
    """Write accepted records to one JSONL file and commit it atomically.

    Records stream to a temporary file beside the target while the migration
    runs. ``commit`` flushes, fsyncs, and renames the file into place, so a
    crash leaves either the previous file or no file, never a partial one.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.written = 0
        self._stream: TextIO | None = None
        self._temporary_path: Path | None = None

    def __enter__(self) -> JsonlTargetWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        self._stream = stream
        self._temporary_path = Path(stream.name)
        return self

    def add(self, record: BaseModel) -> None:
        if self._stream is None:
            raise RuntimeError("JSONL target is not open")
        self._stream.write(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        self.written += 1

    def commit(self) -> JsonlSummary:
        if self._stream is None or self._temporary_path is None:
            raise RuntimeError("JSONL target is not open")
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._stream = None
        assert self._temporary_path is not None
        os.replace(self._temporary_path, self.path)
        self._temporary_path = None
        return JsonlSummary(self.path, self.written)

    def rollback(self) -> None:
        self._discard()

    def close(self) -> None:
        self._discard()

    def _discard(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        temporary, self._temporary_path = self._temporary_path, None
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
