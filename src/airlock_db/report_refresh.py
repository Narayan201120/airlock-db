"""Rebuild the standalone HTML report from a stored run summary."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .pipeline import ValidationMetrics
from .repair_log import RepairLogError, RepairLogReader
from .repair_progress import RepairProgressReader
from .report import write_html_report

_SUMMARY_FORMAT = "airlock-db-run-summary"
_REPAIR_PROGRESS_TABLE = "airlock_repair_progress"


def _fail(message: str) -> int:
    print(f"airlock-db report: {message}", file=sys.stderr)
    return 2


def _load_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"run summary is missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read run summary {path}: {exc}") from exc
    try:
        summary = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"run summary is not valid JSON: {exc}") from exc
    if not isinstance(summary, dict):
        raise ValueError("run summary is not a JSON object")
    if summary.get("format") != _SUMMARY_FORMAT:
        raise ValueError("run summary has an unexpected format field")
    return summary


def _metrics_from_summary(summary: Mapping[str, Any]) -> ValidationMetrics:
    records = summary.get("records") or {}
    issues = summary.get("issues") or {}
    metrics = ValidationMetrics(
        records_seen=int(records.get("checked", 0)),
        valid_records=int(records.get("valid", 0)),
        invalid_records=int(records.get("invalid", 0)),
        file_errors=int(records.get("file_errors", 0)),
        incomplete_files=int(records.get("incomplete_files", 0)),
        drift_counts=Counter(summary.get("drift") or {}),
        error_counts=Counter(issues.get("by_code") or {}),
    )
    source_counts = summary.get("source_counts") or {}
    metrics.source_counts = {
        str(source): dict(counts) for source, counts in source_counts.items()
    }
    return metrics


def _managed_table_row_count(database: Path) -> int:
    """Count rows in the single managed table of an existing target database."""

    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        targets = [
            name
            for name in tables
            if name == "transactions" or name.startswith("dynamic_records_")
        ]
        if len(targets) != 1:
            raise ValueError(f"expected one managed output table, found {len(targets)}")
        quoted = '"' + targets[0].replace('"', '""') + '"'
        return int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    finally:
        connection.close()


def _repair_outcomes(output_dir: Path, *, log_entries: int) -> dict[str, int]:
    """Total repair decisions across every namespace stored in the target database."""

    database = output_dir / "target_migration.db"
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        namespaces: list[tuple[str, int]] = []
        try:
            namespaces = [
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    f"SELECT log_fingerprint, MAX(entry_number) FROM {_REPAIR_PROGRESS_TABLE} "
                    "GROUP BY log_fingerprint"
                )
            ]
        except sqlite3.Error:
            return {"log_entries": log_entries, "corrected": 0, "dropped": 0, "pending": log_entries}
    finally:
        connection.close()

    corrected = 0
    dropped = 0
    for namespace, namespace_max in namespaces:
        entry_count = max(log_entries, namespace_max)
        with RepairProgressReader(database, namespace, entry_count=entry_count) as reader:
            while True:
                entry = reader.next()
                if entry is None:
                    break
                _, outcome = entry
                if outcome == "corrected":
                    corrected += 1
                elif outcome == "dropped":
                    dropped += 1
    return {
        "log_entries": log_entries,
        "corrected": corrected,
        "dropped": dropped,
        "pending": max(0, log_entries - corrected - dropped),
    }


def refresh_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="airlock-db report",
        description="Rebuild airlock_report.html from airlock_summary.json and repair progress.",
    )
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    try:
        summary = _load_summary(output_dir / "airlock_summary.json")
        metrics = _metrics_from_summary(summary)
        error_rows = summary.get("issue_rows") or []
        sources = summary.get("sources") or []
        storage = dict(summary.get("storage") or {})

        database = output_dir / "target_migration.db"
        if database.is_file():
            storage["database_rows"] = _managed_table_row_count(database)

        repair_outcomes: Mapping[str, int] | None = None
        log_path = output_dir / "airlock_errors.jsonl"
        if log_path.is_file():
            with RepairLogReader(log_path) as reader:
                log_entries = sum(1 for _ in reader)
            repair_outcomes = _repair_outcomes(output_dir, log_entries=log_entries)

        report_path = write_html_report(
            output_dir / "airlock_report.html",
            metrics,
            error_rows=error_rows,
            storage=storage,
            sources=sources,
            repair_outcomes=repair_outcomes,
        )
    except (OSError, ValueError, RepairLogError, sqlite3.Error) as exc:
        return _fail(str(exc))

    print(f"Report refreshed: {report_path}")
    if repair_outcomes is not None:
        print(
            f"Repair outcomes: {repair_outcomes['corrected']} corrected, "
            f"{repair_outcomes['dropped']} dropped, "
            f"{repair_outcomes['pending']} pending"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(refresh_main())
