"""End-to-end check for `report --output-dir DIR --compare OTHER_DIR`.

Builds a baseline migration with two JSONL sources and four rejected rows,
then mutates the sources (fix one bad row, add a row missing a required
field, change the counts in the second file, and add a third source file)
so the second run differs in records, issue categories, and sources. The
expected delta is computed from the two run summaries, not hardcoded, and
the new and resolved issue sets must both be non-empty before the
assertions run. The report runs cover the exact stdout Comparison line,
the rendered comparison section, determinism, behavior without --compare,
a missing baseline summary (exit 2, report untouched), self-comparison
with every delta zero, and the standalone/first-match integrity rules
copied from verify_migration_output.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

COMPARISON_MARKER = 'aria-labelledby="comparison-heading"'
NO_COMPARE_LINE_STEM = "Comparison: 0 record fields changed, 0 issue categories new, 0 resolved, 0 sources changed"


def _valid_row(index: int) -> dict:
    return {
        "transaction_id": f"{index:08d}-1111-4111-8111-111111111111",
        "account_id": f"ACC-10{index:02d}",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": "125.50",
        "currency": "USD",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": f"Party {index}",
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, result.stdout, result.stderr


def _report(work: Path, *extra: str) -> tuple[int, str, str]:
    return _run(["report", "--output-dir", str(work / "after"), *extra])


def _extract_section(report: str) -> str | None:
    match = re.search(
        r'<section class="section comparison".*?</section>', report, re.DOTALL
    )
    return match.group(0) if match else None


def _delta_from_summaries(before: dict, after: dict) -> dict[str, object]:
    """Expected delta, computed from the two run summaries only."""

    record_fields = ("checked", "valid", "invalid", "file_errors")
    before_records = before.get("records") or {}
    after_records = after.get("records") or {}
    before_codes = set((before.get("issues") or {}).get("by_code") or {})
    after_codes = set((after.get("issues") or {}).get("by_code") or {})
    before_sources = before.get("source_counts") or {}
    after_sources = after.get("source_counts") or {}

    def _triple(summary: dict, label: str) -> tuple:
        counts = summary.get(label) or {}
        return (
            counts.get("records", 0),
            counts.get("valid", 0),
            counts.get("invalid", 0),
        )

    return {
        "records": [
            (field, before_records.get(field, 0), after_records.get(field, 0))
            for field in record_fields
        ],
        "records_changed": sum(
            1
            for field, before_value, after_value in [
                (field, before_records.get(field, 0), after_records.get(field, 0))
                for field in record_fields
            ]
            if before_value != after_value
        ),
        "issue_new": sorted(after_codes - before_codes),
        "issue_resolved": sorted(before_codes - after_codes),
        "sources_new": sorted(set(after_sources) - set(before_sources)),
        "sources_removed": sorted(set(before_sources) - set(after_sources)),
        "sources_changed": sorted(
            label
            for label in set(before_sources) & set(after_sources)
            if _triple(before_sources, label) != _triple(after_sources, label)
        ),
    }


def _stdout_line(delta: dict[str, object]) -> str:
    sources_changed = (
        len(delta["sources_new"])
        + len(delta["sources_removed"])
        + len(delta["sources_changed"])
    )
    return (
        f"Comparison: {delta['records_changed']} record fields changed, "
        f"{len(delta['issue_new'])} issue categories new, "
        f"{len(delta['issue_resolved'])} resolved, "
        f"{sources_changed} sources changed"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that report --compare diffs two run summaries."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = (
        args.work_dir.resolve()
        if args.work_dir
        else Path(tempfile.mkdtemp(prefix="airlock-run-compare-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    # Fixture: baseline run over two sources, then the mutated second run.
    source_a = work / "source_a.jsonl"
    source_b = work / "source_b.jsonl"
    source_c = work / "source_c.jsonl"
    rows_a = [
        _valid_row(1),
        {**_valid_row(2), "amount": "-5.00"},  # fixed in the second run
        {**_valid_row(3), "currency": "EUROS"},
        {**_valid_row(4), "posted_at": "not-a-date"},
        {**_valid_row(5), "account_id": "x"},
    ]
    rows_b = [_valid_row(index) for index in range(6, 9)]
    _write_jsonl(source_a, rows_a)
    _write_jsonl(source_b, rows_b)

    for run_name in ("before", "after"):
        if run_name == "after":
            rows_a[1] = _valid_row(2)  # fix the bad amount
            _write_jsonl(source_a, rows_a)
            broken = {
                key: value
                for key, value in _valid_row(90).items()
                if key != "counterparty"  # a fresh "missing" issue code
            }
            rows_b = rows_b + [broken]
            _write_jsonl(source_b, rows_b)
            _write_jsonl(source_c, [_valid_row(10), _valid_row(11)])
        inputs = (
            [source_a, source_b, source_c]
            if run_name == "after"
            else [source_a, source_b]
        )
        call = []
        for path in inputs:
            call.extend(["--input", str(path)])
        code, output, error = _run(
            [
                *call,
                "--output-dir",
                str(work / run_name),
                "--error-log",
                str(work / run_name / "airlock_errors.jsonl"),
            ]
        )
        check(
            f"{run_name} migration exit",
            code == 1,
            f"exit={code}\n{(output + error)[-400:]}",
        )

    before_summary = json.loads(
        (work / "before" / "airlock_summary.json").read_text(encoding="utf-8")
    )
    after_summary = json.loads(
        (work / "after" / "airlock_summary.json").read_text(encoding="utf-8")
    )
    delta = _delta_from_summaries(before_summary, after_summary)
    check(
        "fixture new issue categories non-empty",
        bool(delta["issue_new"]),
        f"delta={json.dumps(delta)}; adjust the fixtures",
    )
    check(
        "fixture resolved issue categories non-empty",
        bool(delta["issue_resolved"]),
        f"delta={json.dumps(delta)}; adjust the fixtures",
    )

    after_report = work / "after" / "airlock_report.html"
    expected_line = _stdout_line(delta)

    # 1. Without --compare: no Comparison stdout line, no section in the HTML.
    code, output, error = _report(work)
    text = output + error
    check("plain report exit", code == 0, f"exit={code}\n{text[-400:]}")
    check("plain report refreshed line", "Report refreshed:" in text, text[-400:])
    check("plain repair outcomes line", "Repair outcomes:" in text, text[-400:])
    check("plain no comparison in stdout", "Comparison:" not in text, text[-400:])
    if after_report.is_file():
        report_text = after_report.read_text(encoding="utf-8")
        check(
            "plain no comparison in HTML",
            COMPARISON_MARKER not in report_text,
            "comparison marker present without --compare",
        )
    else:
        check("plain report exists", False, f"missing {after_report}")
    scenarios["without_compare"] = {"exit": code}

    # 2. The compare run prints the exact computed counts.
    code, output, error = _report(work, "--compare", str(work / "before"))
    check(
        "compare report exit",
        code == 0,
        f"exit={code}\n{(output + error)[-400:]}",
    )
    check(
        "compare stdout line",
        expected_line in output,
        f"stdout does not contain {expected_line!r}",
    )
    scenarios["compare_stdout"] = {"exit": code, "line": expected_line}
    report_text = after_report.read_text(encoding="utf-8")
    section_first = _extract_section(report_text)
    check("comparison section rendered", section_first is not None, "section missing")

    # 3. The section carries the computed rows and labels.
    if section_first is not None:
        section_lower = section_first.casefold()
        for field, before_value, after_value in delta["records"]:
            check(
                f"record row {field}",
                f"<td>{field}</td><td>{before_value}</td><td>{after_value}</td>"
                in section_first,
                f"expected {field} {before_value} -> {after_value} row",
            )
        baseline_path = str(work / "before")
        check(
            "baseline directory shown",
            f'<span class="mono">{baseline_path}</span>' in section_first,
            "baseline path missing",
        )

        def _category_text(prefix: str) -> str:
            match = re.search(rf"{re.escape(prefix)}\s*(.*?)</p>", section_first)
            return match.group(1) if match else ""

        new_paragraph = _category_text("New issue categories:")
        resolved_paragraph = _category_text("Resolved issue categories:")
        for label in delta["issue_new"]:
            check(
                f"new code {label} labeled new",
                f"<code>{label}</code>" in new_paragraph,
                f"{label} not in {new_paragraph!r}",
            )
        for label in delta["issue_resolved"]:
            check(
                f"resolved code {label} labeled resolved",
                f"<code>{label}</code>" in resolved_paragraph,
                f"{label} not in {resolved_paragraph!r}",
            )
        check(
            "new category count",
            new_paragraph.count("<code>") == len(delta["issue_new"]),
            new_paragraph,
        )
        check(
            "resolved category count",
            resolved_paragraph.count("<code>") == len(delta["issue_resolved"]),
            resolved_paragraph,
        )
        status_counts = {
            status: len(
                re.findall(
                    rf'<td class="mono">([^<]+)</td><td>{status}</td>', section_first
                )
            )
            for status in ("new", "changed", "removed")
        }
        for status in ("new", "changed"):
            check(
                f"sources {status} count",
                status_counts[status] == len(delta[f"sources_{status}"]),
                f"{status_counts[status]} != {len(delta[f'sources_{status}'])}",
            )
        for label in delta["sources_new"]:
            check(
                f"source marked new: {label}",
                f'<td class="mono">{label}</td><td>new</td>' in section_first,
                "row missing",
            )
        for label in delta["sources_changed"]:
            check(
                f"source marked changed: {label}",
                f'<td class="mono">{label}</td><td>changed</td>' in section_first,
                "row missing",
            )
        check(
            "no scripts in section",
            "<script" not in section_lower,
            "section contains a script",
        )
    scenarios["comparison_section"] = {
        "statuses": {
            "new": len(delta["sources_new"]),
            "changed": len(delta["sources_changed"]),
            "removed": len(delta["sources_removed"]),
        },
        "issue_new": delta["issue_new"],
        "issue_resolved": delta["issue_resolved"],
    }

    # 4. Determinism: the same command twice yields the same section.
    code2, output2, _ = _report(work, "--compare", str(work / "before"))
    section_second = _extract_section(
        after_report.read_text(encoding="utf-8")
    )
    check("determinism exit", code2 == 0, f"exit={code2}")
    check(
        "determinism stdout line",
        expected_line in output2,
        "second run stdout differs",
    )
    check(
        "determinism section identical",
        section_first is not None and section_first == section_second,
        "the extracted comparison section changed between runs",
    )
    scenarios["determinism"] = {"identical": section_first == section_second}

    # 5. A missing baseline summary exits 2 and leaves the report untouched.
    report_hash = _sha256(after_report)
    code, output, error = _report(work, "--compare", str(work / "empty"))
    check("missing baseline exit", code == 2, f"exit={code}\n{error[-400:]}")
    check(
        "missing baseline message",
        "run summary is missing" in error,
        error[-400:],
    )
    check(
        "missing baseline report untouched",
        _sha256(after_report) == report_hash,
        "airlock_report.html was rewritten on failure",
    )
    scenarios["missing_baseline"] = {"exit": code}

    # 6. Comparing the run with itself reports every delta as zero.
    code, output, error = _report(work, "--compare", str(work / "after"))
    text = output + error
    check("self compare exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "self compare line",
        NO_COMPARE_LINE_STEM in output,
        f"stdout does not contain {NO_COMPARE_LINE_STEM!r}",
    )
    scenarios["self_compare"] = {"exit": code, "line": NO_COMPARE_LINE_STEM}

    # 7. The compared HTML is standalone and its first chart/count stay original.
    report_text = after_report.read_text(encoding="utf-8")
    report_lower = report_text.casefold()
    external_asset = re.search(
        r"(?:src|href)\s*=\s*['\"]\s*(?:https?:)?//"
        r"|url\(\s*['\"]?\s*(?:https?:)?//"
        r"|@import\s+(?:url\(\s*)?['\"]?\s*(?:https?:)?//",
        report_lower,
    )
    chart = re.search(
        r'<svg\b[^>]*aria-label="(?:([\d,]+) valid records and ([\d,]+) invalid records'
        r'|No records were validated)"',
        report_text,
        re.IGNORECASE,
    )
    checked = re.search(
        r"<dt>\s*Records checked\s*</dt>\s*<dd>([\d,]+)</dd>",
        report_text,
        re.IGNORECASE,
    )
    expected_valid = int(
        (after_summary.get("records") or {}).get("valid", 0)
    )
    expected_invalid = int(
        (after_summary.get("records") or {}).get("invalid", 0)
    )
    check(
        "integrity no script",
        "<script" not in report_lower,
        "the report gained a script",
    )
    check(
        "integrity no external asset",
        external_asset is None,
        "the report gained an external reference",
    )
    check("integrity chart present", chart is not None, "chart missing")
    check("integrity checked present", checked is not None, "Records checked missing")
    if chart is not None and chart.group(1) is not None:
        check(
            "integrity first chart stays original",
            (
                int(chart.group(1).replace(",", "")) == expected_valid
                and int(chart.group(2).replace(",", "")) == expected_invalid
            ),
            f"chart labels {chart.group(1)} valid / {chart.group(2)} invalid "
            f"but summary says {expected_valid} / {expected_invalid}",
        )
    if checked is not None:
        expected_checked = int(
            (after_summary.get("records") or {}).get("checked", 0)
        )
        check(
            "integrity first count stays original",
            int(checked.group(1).replace(",", "")) == expected_checked,
            f"Records checked is {checked.group(1)} but summary says {expected_checked}",
        )
    scenarios["integrity"] = {"standalone": external_asset is None}

    evidence = work / "run_compare_verification.json"
    evidence.write_text(
        json.dumps(
            {
                "status": "passed" if not failures else "failed",
                "scenarios": scenarios,
                "failures": failures,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if failures:
        for message in failures:
            print(f"FAIL {message}", file=sys.stderr)
        print(
            f"run_compare_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"run_compare_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
