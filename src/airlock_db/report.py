"""Compile migration metrics and bounded error samples into standalone HTML."""

from __future__ import annotations

import html
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pipeline import ValidationMetrics


_STYLE = """
:root {
  color-scheme: light;
  --ink: #102a43;
  --paper: #eef2f4;
  --white: #ffffff;
  --slate: #526777;
  --line: #c6d1d9;
  --teal: #176b61;
  --amber: #b7791f;
  --red: #ad453f;
  --soft-red: #f7e9e7;
  --soft-green: #e5f1ed;
  --focus: #155eef;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
a { color: var(--teal); }
a:focus-visible, summary:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
.page { width: min(1160px, calc(100% - 40px)); margin: 0 auto; padding: 42px 0 64px; }
.masthead { display: flex; justify-content: space-between; align-items: end; gap: 24px; border-bottom: 3px solid var(--ink); padding-bottom: 22px; }
h1 { max-width: 720px; margin: 0; font-size: clamp(2rem, 5vw, 3.5rem); line-height: 1.05; letter-spacing: -0.045em; }
.lede { max-width: 62ch; margin: 14px 0 0; color: var(--slate); }
.run-state { min-width: 175px; border-left: 4px solid var(--teal); padding: 8px 0 8px 14px; }
.run-state.review { border-color: var(--amber); }
.run-state.failed { border-color: var(--red); }
.run-state strong { display: block; font-size: 1.05rem; }
.run-state small, .quiet { color: var(--slate); }
.section { margin-top: 36px; }
h2 { margin: 0 0 14px; font-size: 1.3rem; letter-spacing: -0.02em; }
h3 { margin: 0 0 8px; font-size: 1rem; }
.summary-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr); gap: 36px; align-items: start; }
.chart-frame { padding: 10px 0 0; }
.chart-frame svg { display: block; width: 100%; height: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 10px 22px; margin-top: 8px; color: var(--slate); font-size: .92rem; }
.legend span { display: inline-flex; align-items: center; gap: 8px; }
.swatch { width: 11px; height: 11px; border-radius: 2px; background: var(--teal); }
.swatch.invalid { background: var(--red); }
.metric-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-top: 1px solid var(--line); }
.metric { padding: 14px 12px 14px 0; border-bottom: 1px solid var(--line); }
.metric dt { color: var(--slate); font-size: .88rem; }
.metric dd { margin: 2px 0 0; font-size: 1.65rem; font-variant-numeric: tabular-nums; font-weight: 680; }
.drift-table, .error-table { width: 100%; border-collapse: collapse; text-align: left; }
.drift-table th, .drift-table td, .error-table th, .error-table td { padding: 11px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
.drift-table th, .error-table th { color: var(--slate); font-size: .85rem; font-weight: 650; }
.drift-table td:last-child, .drift-table th:last-child { text-align: right; font-variant-numeric: tabular-nums; }
.empty-row { color: var(--slate); padding: 14px 0; }
.storage { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); padding: 16px 0; }
.storage p { margin: 0; }
.storage-counts { text-align: right; font-variant-numeric: tabular-nums; }
.mono { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .9em; }
.table-wrap { overflow-x: auto; border-top: 1px solid var(--line); }
.error-table { min-width: 860px; }
.error-table tr.problem { background: var(--soft-red); }
.error-table code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .88em; }
.issue-message { min-width: 220px; max-width: 400px; }
details { margin-top: 7px; }
summary { width: fit-content; color: var(--teal); cursor: pointer; font-size: .88rem; }
pre { max-width: 420px; max-height: 250px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 10px; background: var(--white); border: 1px solid var(--line); color: var(--ink); font-size: .8rem; }
.footnote { margin-top: 18px; color: var(--slate); font-size: .88rem; }
@media (max-width: 720px) {
  .page { width: min(100% - 28px, 1160px); padding-top: 26px; }
  .masthead { align-items: start; flex-direction: column; }
  .run-state { min-width: 0; }
  .summary-grid { grid-template-columns: 1fr; gap: 24px; }
  .storage { grid-template-columns: 1fr; }
  .storage-counts { text-align: left; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; }
}
"""

_MAX_ERROR_ROWS = 10_000
_MAX_INPUT_PREVIEW_CHARS = 512


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _format_input(value: Any) -> str:
    if isinstance(value, str):
        return value[:_MAX_INPUT_PREVIEW_CHARS] + (
            "... [truncated]" if len(value) > _MAX_INPUT_PREVIEW_CHARS else ""
        )
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    if len(rendered) > _MAX_INPUT_PREVIEW_CHARS:
        return rendered[:_MAX_INPUT_PREVIEW_CHARS] + "... [truncated]"
    return rendered


def _success_failure_svg(valid: int, invalid: int) -> str:
    total = valid + invalid
    if total == 0:
        return (
            '<svg viewBox="0 0 720 42" role="img" aria-label="No records were validated">'
            '<rect x="0" y="10" width="720" height="20" rx="3" fill="#c6d1d9" />'
            '<text x="360" y="25" text-anchor="middle" fill="#102a43" '
            'font-family="system-ui, sans-serif" font-size="13">No records to chart</text></svg>'
        )
    valid_width = 720 * valid / total
    invalid_width = 720 - valid_width
    return (
        f'<svg viewBox="0 0 720 42" role="img" '
        f'aria-label="{valid} valid records and {invalid} invalid records">'
        f'<rect x="0" y="10" width="{valid_width:.3f}" height="20" rx="3" fill="#176b61" />'
        f'<rect x="{valid_width:.3f}" y="10" width="{invalid_width:.3f}" height="20" fill="#ad453f" />'
        '</svg>'
    )


def _render_error_row(row: Mapping[str, Any]) -> str:
    record_number = row.get("record_number")
    record_text = "Source" if record_number is None else str(record_number)
    location = row.get("location") or ()
    field_text = ".".join(str(part) for part in location) if location else "—"
    input_value = row.get("input")
    input_text = (
        str(input_value)
        if row.get("input_is_serialized")
        else _format_input(input_value)
    )
    input_details = (
        '<details><summary>View input</summary><pre>'
        f"{_escape(input_text)}</pre></details>"
        if input_value is not None
        else ""
    )
    return (
        '<tr class="problem">'
        f'<td class="mono">{_escape(row.get("source", ""))}<br>'
        f'<span class="quiet">{_escape(record_text)}</span></td>'
        f'<td><code>{_escape(row.get("code", "error"))}</code><br>'
        f'<span class="quiet">{_escape(row.get("category", ""))}</span></td>'
        f'<td class="mono">{_escape(field_text)}</td>'
        f'<td class="issue-message">{_escape(row.get("message", ""))}{input_details}</td>'
        '</tr>'
    )


def render_html_report(
    metrics: ValidationMetrics,
    *,
    error_rows: Sequence[Mapping[str, Any]],
    storage: Mapping[str, Any],
    sources: Sequence[str] = (),
    generated_at: datetime | None = None,
    repair_outcomes: Mapping[str, int] | None = None,
    notices: Sequence[str] = (),
) -> str:
    """Return a single-file report with inline styling, SVG, and error details."""

    data = metrics.to_dict()
    valid = int(data["valid_records"])
    invalid = int(data["invalid_records"])
    issue_count = sum(int(count) for count in data["error_counts"].values())
    reported_issue_count = max(issue_count, len(error_rows))
    visible_error_rows = error_rows[:_MAX_ERROR_ROWS]
    storage_status = str(storage.get("status", "unknown"))
    conflict_policy = str(storage.get("conflict_policy", "skip"))
    conflicting_ids = int(storage.get("conflicting_transaction_ids", 0) or 0)
    unresolved_conflicts = conflict_policy == "skip" and conflicting_ids > 0
    has_issues = (
        invalid > 0
        or data["file_errors"] > 0
        or data["incomplete_files"] > 0
        or unresolved_conflicts
    )
    if storage_status == "failed":
        state_label, state_class = "Storage failed", "failed"
    elif has_issues:
        state_label, state_class = "Review required", "review"
    else:
        state_label, state_class = "Migration complete", ""

    timestamp = generated_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp_text = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    source_items = "".join(f"<li class=\"mono\">{_escape(source)}</li>" for source in sources)
    if not source_items:
        source_items = '<li class="quiet">No input paths were recorded.</li>'
    notice_block = "".join(
        f'    <p class="quiet notice" role="note">{_escape(notice)}</p>\n'
        for notice in notices
    )

    drift_counts = data["drift_counts"]
    if drift_counts:
        drift_rows = "".join(
            f"<tr><td>{_escape(category)}</td><td>{int(count)}</td></tr>"
            for category, count in sorted(drift_counts.items())
        )
    else:
        drift_rows = '<tr><td colspan="2" class="empty-row">No schema drift detected.</td></tr>'

    source_counts = data["source_counts"]
    if source_counts:
        source_rows = "".join(
            f"<tr><td>{_escape(source)}</td><td>{int(counts.get('records', 0))}</td>"
            f"<td>{int(counts.get('valid', 0))}</td>"
            f"<td>{int(counts.get('invalid', 0))}</td></tr>"
            for source, counts in sorted(source_counts.items())
        )
        records_by_source_section = f'''
    <section class="section" aria-labelledby="records-by-source-heading">
      <h2 id="records-by-source-heading">Records by source</h2>
      <table class="drift-table">
        <thead><tr><th scope="col">Source</th><th scope="col">Checked</th><th scope="col">Valid</th><th scope="col">Invalid</th></tr></thead>
        <tbody>{source_rows}</tbody>
      </table>
    </section>
'''
    else:
        records_by_source_section = ""

    if repair_outcomes is not None:
        log_entries = int(repair_outcomes.get("log_entries", 0))
        corrected = int(repair_outcomes.get("corrected", 0))
        dropped = int(repair_outcomes.get("dropped", 0))
        pending = int(repair_outcomes.get("pending", 0))
        repair_section = f'''
    <section class="section" aria-labelledby="repair-outcomes-heading">
      <h2 id="repair-outcomes-heading">Repair outcomes</h2>
      <dl class="metric-list">
        <div class="metric"><dt>Log entries</dt><dd>{log_entries}</dd></div>
        <div class="metric"><dt>Corrected</dt><dd>{corrected}</dd></div>
        <div class="metric"><dt>Dropped</dt><dd>{dropped}</dd></div>
        <div class="metric"><dt>Pending</dt><dd>{pending}</dd></div>
      </dl>
    </section>
'''
    else:
        repair_section = ""

    if visible_error_rows:
        rows_by_category: dict[str, list[str]] = {}
        for row in visible_error_rows:
            category = str(row.get("category", "")) or "uncategorized"
            rows_by_category.setdefault(category, []).append(_render_error_row(row))
        category_blocks: list[str] = []
        for category, rendered_rows in sorted(rows_by_category.items()):
            issue_word = "issue" if len(rendered_rows) == 1 else "issues"
            category_blocks.append(
                "<details><summary>"
                f"{_escape(category)} — {len(rendered_rows)} {issue_word}</summary>"
                '<div class="table-wrap">'
                '<table class="error-table">'
                "<thead><tr><th scope=\"col\">Source and row</th>"
                "<th scope=\"col\">Issue</th><th scope=\"col\">Field</th>"
                "<th scope=\"col\">Details</th></tr></thead>"
                f"<tbody>{''.join(rendered_rows)}</tbody>"
                "</table></div></details>"
            )
        error_body = "".join(category_blocks)
    else:
        if reported_issue_count:
            empty_row = (
                '<tr><td colspan="4" class="empty-row">'
                "Issue details were omitted by the report limit."
                "</td></tr>"
            )
        else:
            empty_row = '<tr><td colspan="4" class="empty-row">No validation errors were recorded.</td></tr>'
        error_body = (
            '<div class="table-wrap">'
            '<table class="error-table">'
            "<thead><tr><th scope=\"col\">Source and row</th>"
            "<th scope=\"col\">Issue</th><th scope=\"col\">Field</th>"
            "<th scope=\"col\">Details</th></tr></thead>"
            f"<tbody>{empty_row}</tbody>"
            "</table></div>"
        )

    omitted_count = max(0, reported_issue_count - len(visible_error_rows))
    if omitted_count:
        error_note = (
            f"Showing {len(visible_error_rows)} of {reported_issue_count} issue entries. "
            f"{omitted_count} additional entries were omitted to keep the report bounded."
        )
    else:
        error_note = f"{reported_issue_count} issue entr{'y' if reported_issue_count == 1 else 'ies'} recorded."

    inserted = storage.get("inserted", "—")
    duplicates = storage.get("duplicate_transaction_ids", "—")
    updated = storage.get("updated", "—")
    conflicts = storage.get("conflicting_transaction_ids", "—")
    storage_error = storage.get("error")
    identity_label = str(storage.get("identity_label", "transaction ID"))
    schema_name = str(storage.get("schema_name", "financial transaction"))
    storage_message = (
        f"SQLite output: <span class=\"mono\">{_escape(storage.get('database', ''))}</span> "
        f"(table <span class=\"mono\">{_escape(storage.get('table', 'transactions'))}</span>). "
        f"Status: {_escape(storage_status)}."
    )
    if storage_error:
        storage_message += f" <span class=\"quiet\">{_escape(storage_error)}</span>"

    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>AirLock DB migration report</title>
  <style>{_STYLE}</style>
</head>
<body>
  <main class="page">
    <header class="masthead">
      <div>
        <h1>Migration report</h1>
        <p class="lede">AirLock DB checked each source record against the configured {_escape(schema_name)} schema.</p>
      </div>
      <div class="run-state {state_class}" role="status">
        <strong>{_escape(state_label)}</strong>
        <small>Created { _escape(timestamp_text) }</small>
      </div>
    </header>
{notice_block}
    <section class="section summary-grid" aria-labelledby="record-outcome-heading">
      <div>
        <h2 id="record-outcome-heading">Record outcome</h2>
        <div class="chart-frame">{_success_failure_svg(valid, invalid)}</div>
        <div class="legend" aria-label="Chart legend">
          <span><i class="swatch" aria-hidden="true"></i>{valid} valid</span>
          <span><i class="swatch invalid" aria-hidden="true"></i>{invalid} invalid</span>
        </div>
      </div>
      <dl class="metric-list">
        <div class="metric"><dt>Records checked</dt><dd>{data['records_seen']}</dd></div>
        <div class="metric"><dt>Source errors</dt><dd>{data['file_errors']}</dd></div>
        <div class="metric"><dt>Incomplete files</dt><dd>{data['incomplete_files']}</dd></div>
        <div class="metric"><dt>Validation issues</dt><dd>{issue_count}</dd></div>
      </dl>
    </section>
{records_by_source_section}
    <section class="section" aria-labelledby="storage-heading">
      <h2 id="storage-heading">SQLite target</h2>
      <div class="storage">
        <p>{storage_message}</p>
        <p class="storage-counts">{_escape(inserted)} inserted<br>{_escape(updated)} updated<br>{_escape(duplicates)} duplicate {_escape(identity_label)} rows ({_escape(conflict_policy)} policy; includes conflicts)<br>{_escape(conflicts)} changed-{_escape(identity_label)} conflicts</p>
      </div>
    </section>

    <section class="section" aria-labelledby="drift-heading">
      <h2 id="drift-heading">Schema drift</h2>
      <table class="drift-table">
        <thead><tr><th scope="col">Drift category</th><th scope="col">Occurrences</th></tr></thead>
        <tbody>{drift_rows}</tbody>
      </table>
    </section>

    <section class="section" aria-labelledby="issues-heading">
      <h2 id="issues-heading">Validation and source issues</h2>
      <p class="quiet">{_escape(error_note)}</p>
      {error_body}
    </section>
{repair_section}
    <section class="section" aria-labelledby="sources-heading">
      <h2 id="sources-heading">Input sources</h2>
      <ul>{source_items}</ul>
      <p class="footnote">This report is self-contained. It uses inline CSS and SVG and makes no network requests.</p>
    </section>
  </main>
</body>
</html>
'''


def write_html_report(
    output_path: str | Path,
    metrics: ValidationMetrics,
    *,
    error_rows: Sequence[Mapping[str, Any]],
    storage: Mapping[str, Any],
    sources: Sequence[str] = (),
    repair_outcomes: Mapping[str, int] | None = None,
    notices: Sequence[str] = (),
) -> Path:
    """Write a UTF-8 standalone HTML file and return its path."""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = render_html_report(
        metrics,
        error_rows=error_rows,
        storage=storage,
        sources=sources,
        repair_outcomes=repair_outcomes,
        notices=notices,
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
