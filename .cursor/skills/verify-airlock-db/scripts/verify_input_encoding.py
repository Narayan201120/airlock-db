"""End-to-end check for `--input-encoding`.

Requires the migration, verify, and infer CLIs to decode cp1252 fixtures
(JSONL, JSON array, TSV, CSV, and gzip) with the flag, fail without it,
keep plain UTF-8 inputs working, and refuse unknown encoding names.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
MARKER = "M\u00fcller & S\u00f6hne GmbH"


def _record(index: int) -> dict:
    return {
        "transaction_id": f"{index:08d}-1111-4111-8111-111111111111",
        "account_id": f"ACC-10{index:02d}",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": "125.50",
        "currency": "USD",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": MARKER,
    }


def _write_cp1252_jsonl(path: Path) -> bytes:
    text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in (_record(1), _record(2), _record(3))
    )
    data = text.encode("cp1252")
    path.write_bytes(data)
    return data


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, result.stdout + result.stderr


def _summary_valid(output_dir: Path) -> int:
    summary = json.loads(
        (output_dir / "airlock_summary.json").read_text(encoding="utf-8")
    )
    return summary["records"]["valid"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that --input-encoding decodes cp1252 fixtures end to end."
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
        else Path(tempfile.mkdtemp(prefix="airlock-input-encoding-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    def _migrate(source: Path, output: Path, *extra: str) -> tuple[int, str]:
        return _run(["--input", str(source), "--output-dir", str(output), *extra])

    # 1. JSONL: cp1252 fails by default and works with the flag.
    jsonl = work / "records.jsonl"
    cp1252_bytes = _write_cp1252_jsonl(jsonl)
    out_plain = work / "out-jsonl-plain"
    code, text = _migrate(jsonl, out_plain)
    check("jsonl plain exit", code == 1, f"exit={code}\n{text[-500:]}")
    summary = out_plain / "airlock_summary.json"
    if summary.exists():
        junk = _summary_valid(out_plain)
        check("jsonl plain valid", junk == 0, f"valid={junk}")
    else:
        check("jsonl plain encoding_error", "encoding_error" in text, text[-300:])
    out_flag = work / "out-jsonl-flag"
    code, text = _migrate(jsonl, out_flag, "--input-encoding", "cp1252")
    check("jsonl flag exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("jsonl flag valid", _summary_valid(out_flag) == 3, f"valid={_summary_valid(out_flag)}")
    connection = sqlite3.connect(out_flag / "target_migration.db")
    try:
        matched = connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE counterparty = ?", (MARKER,)
        ).fetchone()[0]
    finally:
        connection.close()
    check("jsonl decoded rows", matched == 3, f"matched={matched}")
    scenarios["jsonl"] = {"flag_exit": code, "decoded": matched}

    # 2. JSON array.
    json_array = work / "records.json"
    json_array.write_bytes(
        json.dumps(
            [_record(1), _record(2), _record(3)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("cp1252")
    )
    code, text = _migrate(json_array, work / "out-json-plain")
    check("json plain exit", code == 1, f"exit={code}\n{text[-300:]}")
    out_flag = work / "out-json-flag"
    code, text = _migrate(json_array, out_flag, "--input-encoding", "cp1252")
    check("json flag exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("json flag valid", _summary_valid(out_flag) == 3, f"valid={_summary_valid(out_flag)}")
    scenarios["json"] = {"flag_exit": code, "valid": _summary_valid(out_flag)}

    # 3. TSV.
    field_names = list(_record(1))
    header = "\t".join(field_names)
    tsv_rows = [header]
    for index in (1, 2, 3):
        tsv_rows.append("\t".join(_record(index).values()))
    tsv = work / "records.tsv"
    tsv.write_bytes(("\n".join(tsv_rows) + "\n").encode("cp1252"))
    code, text = _migrate(tsv, work / "out-tsv-plain")
    check("tsv plain exit", code == 1, f"exit={code}\n{text[-300:]}")
    out_flag = work / "out-tsv-flag"
    code, text = _migrate(tsv, out_flag, "--input-encoding", "cp1252")
    check("tsv flag exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("tsv flag valid", _summary_valid(out_flag) == 3, f"valid={_summary_valid(out_flag)}")
    scenarios["tsv"] = {"flag_exit": code, "valid": _summary_valid(out_flag)}

    # 4. CSV: --csv-encoding wins, --input-encoding is the fallback.
    csv_rows = [",".join(header.split("\t"))]
    for index in (1, 2, 3):
        csv_rows.append(",".join(_record(index).values()))
    csv_1252 = work / "records.csv"
    csv_1252.write_bytes(("\n".join(csv_rows) + "\n").encode("cp1252"))
    code, text = _migrate(csv_1252, work / "out-csv-csvflag", "--csv-encoding", "cp1252")
    check("csv csv-encoding exit", code == 0, f"exit={code}\n{text[-500:]}")
    check(
        "csv csv-encoding valid",
        _summary_valid(work / "out-csv-csvflag") == 3,
        f"valid={_summary_valid(work / 'out-csv-csvflag')}",
    )
    out_flag = work / "out-csv-inputflag"
    code, text = _migrate(csv_1252, out_flag, "--input-encoding", "cp1252")
    check("csv input-encoding exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("csv input-encoding valid", _summary_valid(out_flag) == 3, f"valid={_summary_valid(out_flag)}")
    code, text = _migrate(csv_1252, work / "out-csv-plain")
    check("csv plain exit", code == 1, f"exit={code}\n{text[-300:]}")
    scenarios["csv"] = {"csv_encoding_exit": 0, "input_encoding_exit": 0, "plain_exit": code}

    # 5. Gzip wrapper around the cp1252 JSONL.
    gz = work / "records.jsonl.gz"
    gz.write_bytes(gzip.compress(cp1252_bytes))
    code, text = _migrate(gz, work / "out-gz-plain")
    check("gz plain exit", code == 1, f"exit={code}\n{text[-300:]}")
    out_flag = work / "out-gz-flag"
    code, text = _migrate(gz, out_flag, "--input-encoding", "cp1252")
    check("gz flag exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("gz flag valid", _summary_valid(out_flag) == 3, f"valid={_summary_valid(out_flag)}")
    scenarios["gz"] = {"flag_exit": code, "valid": _summary_valid(out_flag)}

    # 6. UTF-8 BOM regression: no flags needed.
    utf8 = work / "records_utf8.jsonl"
    utf8.write_bytes(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in (_record(1), _record(2), _record(3))
        ).encode("utf-8-sig")
    )
    out_utf8 = work / "out-utf8"
    code, text = _migrate(utf8, out_utf8)
    check("utf8 plain exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("utf8 plain valid", _summary_valid(out_utf8) == 3, f"valid={_summary_valid(out_utf8)}")
    scenarios["utf8"] = {"exit": code, "valid": 3}

    # 7. verify honors the flag using scenario 1's output directory.
    out_verify = work / "out-jsonl-flag"
    code, text = _run(
        [
            "verify",
            "--input", str(jsonl),
            "--output-dir", str(out_verify),
            "--input-encoding", "cp1252",
        ]
    )
    check("verify flag exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("verify consistent", "Verification: consistent" in text, text[-300:])
    code, text = _run(["verify", "--input", str(jsonl), "--output-dir", str(out_verify)])
    check("verify plain exit", code == 1, f"exit={code}\n{text[-300:]}")
    scenarios["verify"] = {"flag_exit": 0, "plain_exit": code}

    # 8. infer honors the flag.
    inferred = work / "inferred.json"
    code, text = _run(
        ["infer", "--input", str(jsonl), "--output", str(inferred), "--input-encoding", "cp1252"]
    )
    check("infer flag exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("infer output exists", inferred.exists(), "inferred layout file missing")
    code, text = _run(["infer", "--input", str(jsonl), "--output", str(work / "inferred2.json")])
    check("infer plain exit", code == 1, f"exit={code}\n{text[-300:]}")
    check("infer plain encoding_error", "encoding_error" in text, text[-300:])
    scenarios["infer"] = {"flag_exit": 0, "plain_exit": code}

    # 9. Unknown encoding name.
    code, text = _migrate(jsonl, work / "out-bad", "--input-encoding", "nonsense")
    check("migrate bad name exit", code == 2, f"exit={code}\n{text[-300:]}")
    check("migrate bad name message", "unknown text encoding" in text, text[-300:])
    code, text = _run(
        [
            "verify",
            "--input", str(jsonl),
            "--output-dir", str(out_verify),
            "--input-encoding", "nonsense",
        ]
    )
    check("verify bad name exit", code == 2, f"exit={code}\n{text[-300:]}")
    check("verify bad name message", "unknown text encoding" in text, text[-300:])
    code, text = _run(
        ["infer", "--input", str(jsonl), "--output", str(work / "bad.yaml"), "--input-encoding", "nonsense"]
    )
    check("infer bad name exit", code == 1, f"exit={code}\n{text[-300:]}")
    check("infer bad name message", "unknown text encoding" in text, text[-300:])
    scenarios["bad_name"] = {"migrate_exit": 2, "verify_exit": 2, "infer_exit": code}

    evidence = work / "input_encoding_verification.json"
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
            f"input_encoding_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"input_encoding_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
