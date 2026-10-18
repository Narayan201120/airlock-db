"""Machine-readable run summary written beside the HTML report."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._version import __version__
from .pipeline import ValidationMetrics

SUMMARY_FORMAT = "airlock-db-run-summary"


def sample_notice(limit: int | None, reached: bool) -> str | None:
    """Return the preview-run notice for a sampled run, or None."""

    if not reached:
        return None
    return (
        f"Preview run: stopped after {limit} accepted records; "
        "the target holds only the scanned prefix of the sources."
    )

_SUMMARY_STORAGE_KEYS = (
    "status",
    "database",
    "table",
    "inserted",
    "updated",
    "duplicate_transaction_ids",
    "conflicting_transaction_ids",
    "jsonl",
    "jsonl_rows",
    "error",
)


def build_run_summary(
    metrics: ValidationMetrics,
    *,
    storage: Mapping[str, Any],
    sources: Sequence[str],
    source_manifest: Sequence[Mapping[str, Any]] | None = None,
    schema_name: str,
    schema_fingerprint: str,
    output_format: str,
    conflict_policy: str,
    anonymized: bool,
    exit_code: int,
    issue_rows: Sequence[Mapping[str, Any]] | None = None,
    resumed_records: int = 0,
    sample_limit: int | None = None,
    sample_reached: bool = False,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the JSON-safe run summary for one migration."""

    data = metrics.to_dict()
    timestamp = generated_at or datetime.now(timezone.utc)
    timestamp_text = (
        timestamp.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    storage_snapshot = {
        key: storage.get(key)
        for key in _SUMMARY_STORAGE_KEYS
        if key in storage
    }
    records: dict[str, Any] = {
        "checked": data["records_seen"],
        "valid": data["valid_records"],
        "invalid": data["invalid_records"],
        "file_errors": data["file_errors"],
        "incomplete_files": data["incomplete_files"],
        "normalized_fields": data["normalized_fields"],
        "resumed_records": resumed_records,
    }
    if sample_limit is not None:
        records["sample_limit"] = sample_limit
        records["sample_reached"] = bool(sample_reached)
    return {
        "format": SUMMARY_FORMAT,
        "version": 1,
        "airlock_version": __version__,
        "generated_at": timestamp_text,
        "anonymized": anonymized,
        "schema": {"name": schema_name, "fingerprint": schema_fingerprint},
        "output": {"format": output_format, "conflict_policy": conflict_policy},
        "records": records,
        "issues": {
            "total": sum(data["error_counts"].values()),
            "by_code": data["error_counts"],
        },
        "drift": {category: count for category, count in data["drift_counts"].items()},
        "source_counts": data.get("source_counts", {}),
        "sources": list(sources),
        "source_manifest": (
            [dict(entry) for entry in source_manifest]
            if source_manifest is not None
            else None
        ),
        "issue_rows": [dict(row) for row in (issue_rows or ())],
        "storage": storage_snapshot,
        "exit_code": exit_code,
    }


def write_run_summary(path: str | Path, summary: Mapping[str, Any]) -> Path:
    """Write the run summary atomically and return its path."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = (
        json.dumps(
            summary,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            allow_nan=False,
            default=str,
        )
        + "\n"
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(document)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return destination
