"""Streaming source manifest: one label, size, and SHA-256 per source file.

The migration records an entry when it starts reading each file, and
``verify`` re-collects the entries and compares them before trusting the row
digests, so a swapped file of the same shape cannot pass silently. Each
digest is one streaming read in bounded chunks, and anonymized runs keep
labels only.
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

_CHUNK_BYTES = 1 << 20


def file_digest(path: Path) -> tuple[int, str]:
    """Return the ``(size, sha256)`` of one file, read in bounded chunks."""

    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()


class HashingBinaryReader:
    """Wrap a binary stream and feed a SHA-256 while it is read.

    The digest covers the exact raw bytes pulled from the underlying
    stream, once, in order. The wrapper is intentionally non-seekable so
    a rewind cannot hash the same bytes twice; XML and other readers
    that once rewound now chain a prefix instead. Call ``drain`` before
    ``complete`` so an early stop still hashes the whole file.
    """

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self._hasher = hashlib.sha256()
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    def drain(self) -> None:
        while True:
            chunk = self._raw.read(_CHUNK_BYTES)
            if not chunk:
                break
            self._hasher.update(chunk)
            self._size += len(chunk)

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        if data:
            self._hasher.update(data)
            self._size += len(data)
        return data

    def read1(self, size: int = -1) -> bytes:
        read1 = getattr(self._raw, "read1", None)
        if callable(read1):
            try:
                data = read1(size)
            except Exception:
                data = self._raw.read(size)
        else:
            data = self._raw.read(size)
        if data:
            self._hasher.update(data)
            self._size += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        n = self._raw.readinto(buffer)  # type: ignore[attr-defined]
        if n:
            self._hasher.update(bytes(buffer[:n]))
            self._size += n
        return n

    def readinto1(self, buffer: Any) -> int:
        readinto1 = getattr(self._raw, "readinto1", None)
        if callable(readinto1):
            try:
                n = readinto1(buffer)
            except Exception:
                n = self._raw.readinto(buffer)  # type: ignore[attr-defined]
        else:
            n = self._raw.readinto(buffer)  # type: ignore[attr-defined]
        if n:
            self._hasher.update(bytes(buffer[:n]))
            self._size += n
        return n

    def readline(self, size: int = -1) -> bytes:
        data = self._raw.readline(size)  # type: ignore[attr-defined]
        if data:
            self._hasher.update(data)
            self._size += len(data)
        return data

    def readlines(self, hint: int = -1) -> list[bytes]:
        lines = self._raw.readlines(hint)  # type: ignore[attr-defined]
        for line in lines:
            if line:
                self._hasher.update(line)
                self._size += len(line)
        return lines

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def seek(self, *args: Any, **kwargs: Any) -> Any:
        raise io.UnsupportedOperation("hashing stream is not seekable")

    def tell(self) -> int:
        return self._size

    def flush(self) -> None:
        pass

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        return bool(getattr(self._raw, "closed", False))

    def __enter__(self) -> HashingBinaryReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> bytes:
        chunk = self.read(8192)
        if not chunk:
            raise StopIteration
        return chunk


class SourceManifestCollector:
    """Collect one manifest entry per source file the readers consume."""

    def __init__(self, *, anonymized: bool = False) -> None:
        self.entries: list[dict[str, Any]] = []
        self._anonymized = anonymized

    def record(self, path: Path, label: str) -> None:
        """Append one entry for a file about to be read.

        Anonymized runs keep the label only, because the digest would leak a
        fingerprint of the client's file. A file that cannot be opened yet
        keeps a label-only entry; the reader reports its own error.
        """

        entry: dict[str, Any] = {"label": label}
        if not self._anonymized:
            try:
                size, digest = file_digest(path)
            except OSError:
                pass
            else:
                entry["size"] = size
                entry["sha256"] = digest
        self.entries.append(entry)

    def begin(self, path: Path, label: str) -> int | None:
        """Reserve one label-only entry and return its index for ``complete``.

        Single-pass readers call this before opening a file, stream the
        file once through :class:`HashingBinaryReader`, then call
        ``complete`` with the observed size and digest. Anonymized runs
        return ``None`` because there is nothing to fill in. A file that
        cannot be opened keeps its label-only entry, matching ``record``.
        """

        self.entries.append({"label": label})
        if self._anonymized:
            return None
        return len(self.entries) - 1

    def complete(self, index: int | None, size: int, sha256: str) -> None:
        """Fill a reserved entry with the streamed size and digest."""

        if index is None:
            return
        try:
            entry = self.entries[index]
        except IndexError:
            return
        entry["size"] = size
        entry["sha256"] = sha256


@dataclass(frozen=True, slots=True)
class SourceManifestComparison:
    """Difference between a recorded manifest and the sources re-read now."""

    matched: int
    changed: tuple[str, ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.changed or self.missing or self.extra)


def _comparison_key(label: str) -> str:
    """Normalize a file label so relative and absolute spellings agree."""

    try:
        return os.path.normcase(os.path.realpath(label))
    except OSError:
        return label


def compare_manifests(
    recorded: Sequence[Mapping[str, Any]],
    collected: Sequence[Mapping[str, Any]],
) -> SourceManifestComparison:
    """Compare a recorded manifest with the entries collected by ``verify``."""

    collected_by_key: dict[str, Mapping[str, Any]] = {}
    for entry in collected:
        collected_by_key.setdefault(_comparison_key(str(entry.get("label", ""))), entry)
    changed: list[str] = []
    missing: list[str] = []
    matched = 0
    recorded_keys: set[str] = set()
    for entry in recorded:
        label = str(entry.get("label", ""))
        key = _comparison_key(label)
        recorded_keys.add(key)
        found = collected_by_key.get(key)
        if found is None:
            missing.append(label)
            continue
        digest = entry.get("sha256")
        if digest is not None and found.get("sha256") != digest:
            changed.append(label)
        else:
            matched += 1
    extra = [
        str(entry.get("label", ""))
        for key, entry in collected_by_key.items()
        if key not in recorded_keys
    ]
    return SourceManifestComparison(matched, tuple(changed), tuple(missing), tuple(extra))
