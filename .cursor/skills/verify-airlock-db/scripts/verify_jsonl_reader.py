"""Verify JSONL and CSV reader edge cases against crafted inputs.

Covers the paths the standard fixtures miss: LF/CRLF/bare-CR line endings,
blank lines, records split across chunk boundaries, oversized records,
invalid UTF-8, duplicate JSON keys, and CSV rows with empty, short, or extra
values. Run from any directory; pass a scratch directory for generated files.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

from airlock_db.ingestion import (  # noqa: E402
    _iter_bounded_jsonl_lines,
    iter_source_records,
)

CAP = 512 * 1024
CHUNK = 64 * 1024


def _fake(char: bytes, total: int) -> bytes:
    return char * total


SPLITTER_CASES: dict[str, dict[str, object]] = {
    "lf": {"sizes": [1, 1, 1], "content": [b"a", b"b", b"c"]},
    "crlf": {"sizes": [1, 1, 1], "content": [b"a", b"b", b"c"]},
    "bare_cr": {"sizes": [1, 1, 1], "content": [b"a", b"b", b"c"]},
    "mixed": {
        "sizes": [1, 0, 1, 0, 1],
        "content": [b"a", b"", b"b", b"", b"c"],
    },
    "empty_file": {"sizes": [], "content": []},
    "only_lf": {"sizes": [0], "content": [b""]},
    "only_cr": {"sizes": [0], "content": [b""]},
    "only_crlf": {"sizes": [0], "content": [b""]},
    "no_trailing_newline": {"sizes": [1], "content": [b"a"]},
    "trailing_blank": {"sizes": [1, 0], "content": [b"a", b""]},
    "crlf_across_chunk": {
        "sizes": [CHUNK - 1, 4],
        "content": [_fake(b"a", CHUNK - 1), b"tail"],
    },
    "bare_cr_at_chunk_end": {
        "sizes": [CHUNK - 1, 4],
        "content": [_fake(b"a", CHUNK - 1), b"tail"],
    },
    "exact_cap": {"sizes": [CAP], "content": [_fake(b"a", CAP)]},
    "cap_plus_one": {"sizes": [CAP + 1], "oversized": [True], "content": [None]},
    "giant_three_chunks": {
        "sizes": [3 * CHUNK + 1000, 1],
        "content": [_fake(b"a", 3 * CHUNK + 1000), b"z"],
    },
    "oversized_then_ok": {
        "sizes": [600_000, 2],
        "oversized": [True, False],
        "content": [None, b"ok"],
    },
    "oversized_at_eof": {
        "sizes": [2, 600_000],
        "oversized": [False, True],
        "content": [b"ok", None],
    },
    "cr_crlf_sequence": {
        "sizes": [1, 0, 0, 1],
        "content": [b"a", b"", b"", b"b"],
    },
    "crlf_split_cr_end": {"sizes": [3, 4], "content": [b"pad", b"next"]},
}

SPLITTER_PAYLOADS: dict[str, bytes] = {
    "lf": b"a\nb\nc",
    "crlf": b"a\r\nb\r\nc\r\n",
    "bare_cr": b"a\rb\rc\r",
    "mixed": b"a\r\n\nb\r\n\r\nc",
    "empty_file": b"",
    "only_lf": b"\n",
    "only_cr": b"\r",
    "only_crlf": b"\r\n",
    "no_trailing_newline": b"a",
    "trailing_blank": b"a\n\n",
    "crlf_across_chunk": _fake(b"a", CHUNK - 1) + b"\r\n" + b"tail\n",
    "bare_cr_at_chunk_end": _fake(b"a", CHUNK - 1) + b"\r" + b"tail\n",
    "exact_cap": _fake(b"a", CAP) + b"\n",
    "cap_plus_one": _fake(b"a", CAP + 1) + b"\n",
    "giant_three_chunks": _fake(b"a", 3 * CHUNK + 1000) + b"\n" + b"z\n",
    "oversized_then_ok": _fake(b"a", 600_000) + b"\n" + b"ok\n",
    "oversized_at_eof": b"ok\n" + _fake(b"a", 600_000),
    "cr_crlf_sequence": b"a\r\r\r\nb",
    "crlf_split_cr_end": b"pad\r\nnext\n",
}

MESSY_PARTS = [
    b'{"a": 1}\n',
    b"\n",
    b'{"b": 2}\r\n',
    b'{"c": 3}\r{"d": 4}\r',
    b'{"x": "\xff"}\n',
    b"{oops\n",
    b'{"k": 1, "k": 2}\n',
    b'{"big": "' + b"a" * 600_000 + b'"}\n',
    b'{"e": 5}',
]

EXPECTED_MESSY = [
    {"number": 1, "keys": ["a"]},
    {"number": 3, "keys": ["b"]},
    {"number": 4, "keys": ["c"]},
    {"number": 5, "keys": ["d"]},
    {"number": 6, "issue": "encoding_error", "input_len": 10},
    {"number": 7, "issue": "json_decode_error", "input_len": 5},
    {"number": 8, "issue": "duplicate_json_key", "input_len": 16},
    {"number": 9, "issue": "record_too_large", "input_len": 315},
    {"number": 10, "keys": ["e"]},
]

EXPECTED_CLI = {
    "exit_code": 1,
    "checked": 9,
    "valid": 0,
    "invalid": 9,
    "by_code": {
        "duplicate_json_key": 1,
        "encoding_error": 1,
        "extra_forbidden": 5,
        "json_decode_error": 1,
        "missing": 40,
        "record_too_large": 1,
    },
}

EXPECTED_CSV_ROWS = [
    (1, {"a": "1", "b": "2", "c": "3", "d": "4"}),
    (2, {"a": "2", "b": None, "c": "4", "d": "5"}),
    (3, {"a": "only", "b": "two"}),
    (4, "csv_extra_values"),
]


def _fail(label: str, expected: object, actual: object) -> None:
    raise SystemExit(
        f"{label} mismatch\nexpected: {expected!r}\nactual:   {actual!r}"
    )


def check_splitter() -> int:
    checks = 0
    for name, payload in SPLITTER_PAYLOADS.items():
        expected = SPLITTER_CASES[name]
        results = list(_iter_bounded_jsonl_lines(io.BytesIO(payload)))
        sizes = [line_size for _raw, line_size, _preview, _over in results]
        oversized = [over for _raw, _size, _preview, over in results]
        if sizes != expected["sizes"]:
            _fail(f"splitter[{name}] sizes", expected["sizes"], sizes)
        expected_oversized = expected.get(
            "oversized", [False] * len(expected["sizes"])
        )
        if oversized != expected_oversized:
            _fail(f"splitter[{name}] oversized", expected_oversized, oversized)
        if "content" in expected:
            content = [raw for raw, _size, _preview, _over in results]
            if content != expected["content"]:
                _fail(f"splitter[{name}] content", expected["content"], content)
        checks += len(results) + 1
    return checks


def check_messy_file(work: Path) -> int:
    messy = work / "messy_jsonl.jsonl"
    messy.write_bytes(b"".join(MESSY_PARTS))
    records = []
    for record in iter_source_records(messy):
        if record.issue is not None:
            records.append(
                {
                    "number": record.record_number,
                    "issue": record.issue.code,
                    "input_len": len(record.issue.input_value or ""),
                }
            )
        else:
            records.append(
                {"number": record.record_number, "keys": sorted(record.value)}
            )
    if records != EXPECTED_MESSY:
        _fail("messy records", EXPECTED_MESSY, records)

    cli_out = work / "cli_out"
    completed = subprocess.run(
        [
            sys.executable,
            "main.py",
            "--input",
            str(messy),
            "--output-dir",
            str(cli_out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    summary = json.loads((cli_out / "airlock_summary.json").read_text(encoding="utf-8"))
    observed = {
        "exit_code": completed.returncode,
        "checked": summary["records"]["checked"],
        "valid": summary["records"]["valid"],
        "invalid": summary["records"]["invalid"],
        "by_code": summary["issues"]["by_code"],
    }
    if observed != EXPECTED_CLI:
        _fail("messy CLI summary", EXPECTED_CLI, observed)
    return len(records) + 1


def check_csv_edges(work: Path) -> int:
    csv_path = work / "csv_edge.csv"
    csv_path.write_text(
        "a,b,c,d\n1,2,3,4\n\n2,,4,5\nonly,two\n1,2,3,4,5\n",
        encoding="utf-8",
    )
    rows = [
        (
            record.record_number,
            record.issue.code if record.issue is not None else record.value,
        )
        for record in iter_source_records(csv_path)
    ]
    if rows != EXPECTED_CSV_ROWS:
        _fail("csv edge rows", EXPECTED_CSV_ROWS, rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work_dir", type=Path, help="scratch directory for outputs")
    args = parser.parse_args(argv)

    logging.disable(logging.CRITICAL)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    checks = check_splitter()
    checks += check_messy_file(args.work_dir)
    checks += check_csv_edges(args.work_dir)
    print(f"jsonl_csv_reader_ok checks={checks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
