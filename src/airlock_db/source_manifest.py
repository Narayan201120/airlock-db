"""Streaming source manifest: one label, size, and SHA-256 per source file.

The migration records an entry when it starts reading each file, and
``verify`` re-collects the entries and compares them before trusting the row
digests, so a swapped file of the same shape cannot pass silently. Each
digest is one streaming read in bounded chunks, and anonymized runs keep
labels only.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

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
