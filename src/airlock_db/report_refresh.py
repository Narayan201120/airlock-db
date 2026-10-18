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
from .summary import sample_notice

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


def _source_counts(source_counts: Mapping[str, Any], label: str) -> tuple[int, int, int]:
    counts = source_counts.get(label) or {}
    return (
        int(counts.get("records", 0)),
        int(counts.get("valid", 0)),
        int(counts.get("invalid", 0)),
    )


def _comparison_from_summaries(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Diff two run summaries; the baseline is ``before``, the now run is ``after``."""

    before_records = before.get("records") or {}
    after_records = after.get("records") or {}
    record_rows = [
        {
            "field": field,
            "before": int(before_records.get(field, 0)),
            "after": int(after_records.get(field, 0)),
        }
        for field in ("checked", "valid", "invalid", "file_errors")
    ]
    records_changed = sum(
        1 for row in record_rows if row["before"] != row["after"]
    )

    before_codes = dict((before.get("issues") or {}).get("by_code") or {})
    after_codes = dict((after.get("issues") or {}).get("by_code") or {})
    issue_new = sorted(set(after_codes) - set(before_codes))
    issue_resolved = sorted(set(before_codes) - set(after_codes))
    issues_total_before = int((before.get("issues") or {}).get("total", 0))
    issues_total_after = int((after.get("issues") or {}).get("total", 0))

    before_sources = before.get("source_counts") or {}
    after_sources = after.get("source_counts") or {}
    sources_new = sorted(set(after_sources) - set(before_sources))
    sources_removed = sorted(set(before_sources) - set(after_sources))
    sources_changed = sorted(
        label
        for label in set(before_sources) & set(after_sources)
        if _source_counts(before_sources, label) != _source_counts(after_sources, label)
    )

    return {
        "baseline": "",
        "records": record_rows,
        "records_changed": records_changed,
        "issue_new": issue_new,
        "issue_resolved": issue_resolved,
        "issues_total_before": issues_total_before,
        "issues_total_after": issues_total_after,
        "sources_new": sources_new,
        "sources_removed": sources_removed,
        "sources_changed": sources_changed,
        "sources_changed_count": (
            len(sources_new) + len(sources_removed) + len(sources_changed)
        ),
    }


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
    parser.add_argument("--compare", metavar="OTHER_DIR", default=None)
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    try:
        summary = _load_summary(output_dir / "airlock_summary.json")
        metrics = _metrics_from_summary(summary)
        comparison: Mapping[str, Any] | None = None
        if args.compare:
            baseline_summary = _load_summary(Path(args.compare) / "airlock_summary.json")
            comparison = _comparison_from_summaries(baseline_summary, summary)
            comparison["baseline"] = str(Path(args.compare))
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

        notices: list[str] = []
        records = summary.get("records") or {}
        notice = sample_notice(
            int(records["sample_limit"]) if "sample_limit" in records else None,
            bool(records.get("sample_reached")),
        )
        if notice is not None:
            notices.append(notice)

        report_path = write_html_report(
            output_dir / "airlock_report.html",
            metrics,
            error_rows=error_rows,
            storage=storage,
            sources=sources,
            repair_outcomes=repair_outcomes,
            notices=notices,
            comparison=comparison,
        )
    except (OSError, ValueError, RepairLogError, sqlite3.Error) as exc:
        return _fail(str(exc))

    print(f"Report refreshed: {report_path}")
    if comparison is not None:
        print(
            f"Comparison: {comparison['records_changed']} record fields changed, "
            f"{len(comparison['issue_new'])} issue categories new, "
            f"{len(comparison['issue_resolved'])} resolved, "
            f"{comparison['sources_changed_count']} sources changed"
        )
    if repair_outcomes is not None:
        print(
            f"Repair outcomes: {repair_outcomes['corrected']} corrected, "
            f"{repair_outcomes['dropped']} dropped, "
            f"{repair_outcomes['pending']} pending"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(refresh_main())
