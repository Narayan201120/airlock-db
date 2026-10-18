"""End-to-end check for fixed-width positional input.

Builds a fixed-width twin of a delimited fixture, migrates both, and requires
identical stored rows and counts; then requires archives, directory scans, a
non-UTF-8 encoding, a short line, bad span specs, and `infer` to behave as
designed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

BANNER = "ACCOUNT EXPORT v1 - CONFIDENTIAL"

FIELDS = (
    ("transaction_id", 1, 36),
    ("account_id", 37, 8),
    ("posted_at", 45, 20),
    ("amount", 65, 10),
    ("currency", 75, 3),
    ("transaction_type", 78, 6),
    ("status", 84, 8),
    ("counterparty", 92, 24),
    ("description", 116, 30),
)
RECORD_LENGTH = 145
RIGHT_ALIGNED = {"amount"}

RECORDS = [
    {
        "transaction_id": "11111111-1111-4111-8111-111111111111",
        "account_id": "ACC-1001",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": "125.50",
        "currency": "USD",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": "Northwind Trading Co",
        "description": "Office supplies invoice 4471",
    },
    {
        "transaction_id": "22222222-2222-4222-8222-222222222222",
        "account_id": "ACC-2002",
        "posted_at": "2026-01-16T14:00:00Z",
        "amount": "80.00",
        "currency": "EUR",
        "transaction_type": "CREDIT",
        "status": "PENDING",
        "counterparty": "Rheinland Logistik GmbH",
        "description": "",
    },
    {
        "transaction_id": "33333333-3333-4333-8333-333333333333",
        "account_id": "ACC-3003",
        "posted_at": "2026-01-17T23:59:59Z",
        "amount": "9.99",
        "currency": "GBP",
        "transaction_type": "REFUND",
        "status": "REVERSED",
        "counterparty": "Albion Books Ltd",
        "description": "Refund for order 88",
    },
]


def _layout_spec(encoding: str = "utf-8", skip_lines: int = 1) -> dict:
    return {
        "encoding": encoding,
        "skip_lines": skip_lines,
        "fields": [
            {"name": name, "start": start, "length": length}
            for name, start, length in FIELDS
        ],
    }


def _cell(record: dict, name: str, length: int) -> str:
    value = str(record.get(name) or "")
    if len(value) > length:
        raise ValueError(f"fixture value for {name} exceeds its {length}-column span")
    return value.rjust(length) if name in RIGHT_ALIGNED else value.ljust(length)


def _fixed_line(record: dict) -> str:
    return "".join(_cell(record, name, length) for name, _start, length in FIELDS)


def _fixed_text(records: list[dict]) -> str:
    lines = [BANNER, *(_fixed_line(record) for record in records)]
    return "\n".join(lines) + "\n"


def _write_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[name for name, _, _ in FIELDS])
        writer.writeheader()
        for record in RECORDS:
            writer.writerow(record)


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


def _summary(output_dir: Path) -> dict:
    return json.loads((output_dir / "airlock_summary.json").read_text(encoding="utf-8"))


def _summary_counts(output_dir: Path) -> tuple[int, int, int]:
    records = _summary(output_dir)["records"]
    return records["checked"], records["valid"], records["invalid"]


def _stored_rows(output_dir: Path) -> list[tuple]:
    connection = sqlite3.connect(output_dir / "target_migration.db")
    try:
        return connection.execute(
            "SELECT transaction_id, account_id, posted_at, amount, currency, "
            "transaction_type, status, counterparty, description "
            "FROM transactions ORDER BY transaction_id"
        ).fetchall()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that --fixed-width reads delimited-equivalent records."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="airlock-fixed-width-"))
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    layout_path = work / "layout.json"
    layout_path.write_text(json.dumps(_layout_spec()), encoding="utf-8")

    # 1. The delimited twin migrates cleanly.
    twin_csv = work / "twin.csv"
    _write_csv(twin_csv)
    code, text = _run(["--input", str(twin_csv), "--output-dir", str(work / "out-csv")])
    check("csv twin exit", code == 0, f"exit={code}\n{text[-500:]}")
    csv_counts = _summary_counts(work / "out-csv")
    check("csv twin counts", csv_counts == (3, 3, 0), f"counts={csv_counts}")
    csv_rows = _stored_rows(work / "out-csv")

    # 2. The fixed-width twin matches the delimited twin exactly.
    twin_dat = work / "twin.dat"
    twin_dat.write_text(_fixed_text(RECORDS), encoding="utf-8")
    code, text = _run(
        [
            "--input", str(twin_dat),
            "--fixed-width", str(layout_path),
            "--output-dir", str(work / "out-fw"),
        ]
    )
    check("fixed-width twin exit", code == 0, f"exit={code}\n{text[-500:]}")
    fw_counts = _summary_counts(work / "out-fw")
    check("fixed-width twin counts", fw_counts == csv_counts, f"counts={fw_counts} csv={csv_counts}")
    fw_rows = _stored_rows(work / "out-fw")
    check("fixed-width twin rows", fw_rows == csv_rows, f"fw={fw_rows}\ncsv={csv_rows}")
    scenarios["twin"] = {"counts": list(fw_counts), "rows": len(fw_rows)}

    # 3. Verify replays the fixed-width source, and the flag is required.
    code, text = _run(
        [
            "verify",
            "--input", str(twin_dat),
            "--fixed-width", str(layout_path),
            "--output-dir", str(work / "out-fw"),
        ]
    )
    check("fixed-width verify exit", code == 0, f"exit={code}\n{text[-500:]}")
    code, text = _run(["verify", "--input", str(twin_dat), "--output-dir", str(work / "out-fw")])
    check("verify without flag fails", code == 1, f"exit={code}\n{text[-300:]}")
    scenarios["verify"] = {"with_flag": 0, "without_flag": code}

    # 4. A directory scan reads every member file with the layout.
    parts = work / "parts"
    parts.mkdir()
    (parts / "part1.dat").write_text(_fixed_text(RECORDS[:2]), encoding="utf-8")
    (parts / "part2.dat").write_text(_fixed_text(RECORDS[2:]), encoding="utf-8")
    code, text = _run(
        [
            "--input", str(parts),
            "--fixed-width", str(layout_path),
            "--output-dir", str(work / "out-parts"),
        ]
    )
    check("directory scan exit", code == 0, f"exit={code}\n{text[-500:]}")
    parts_counts = _summary_counts(work / "out-parts")
    check("directory scan counts", parts_counts == csv_counts, f"counts={parts_counts}")
    check(
        "directory scan rows",
        _stored_rows(work / "out-parts") == csv_rows,
        "scanned rows differ from the twin",
    )
    scenarios["directory"] = {"counts": list(parts_counts)}

    # 5. A gzip layer around the same records.
    gz_path = work / "twin.dat.gz"
    with gzip.open(gz_path, "wb") as stream:
        stream.write(_fixed_text(RECORDS).encode("utf-8"))
    code, text = _run(
        [
            "--input", str(gz_path),
            "--fixed-width", str(layout_path),
            "--output-dir", str(work / "out-gz"),
        ]
    )
    check("gzip exit", code == 0, f"exit={code}\n{text[-500:]}")
    gz_counts = _summary_counts(work / "out-gz")
    check("gzip counts", gz_counts == csv_counts, f"counts={gz_counts}")
    scenarios["gzip"] = {"counts": list(gz_counts)}

    # 6. A zip member with an unsupported suffix still reads as fixed-width.
    zip_path = work / "records.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("records.dat", _fixed_text(RECORDS))
    code, text = _run(
        [
            "--input", str(zip_path),
            "--fixed-width", str(layout_path),
            "--output-dir", str(work / "out-zip"),
        ]
    )
    check("zip member exit", code == 0, f"exit={code}\n{text[-500:]}")
    zip_counts = _summary_counts(work / "out-zip")
    check("zip member counts", zip_counts == csv_counts, f"counts={zip_counts}")
    check("zip member rows", _stored_rows(work / "out-zip") == csv_rows, "zip rows differ from the twin")
    scenarios["zip"] = {"counts": list(zip_counts)}

    # 7. A short line is one rejected row, not a crash.
    short_path = work / "short.dat"
    lines = _fixed_text(RECORDS).splitlines()
    lines[2] = lines[2][:60]
    short_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, text = _run(
        [
            "--input", str(short_path),
            "--fixed-width", str(layout_path),
            "--output-dir", str(work / "out-short"),
        ]
    )
    check("short line exit", code == 1, f"exit={code}\n{text[-500:]}")
    short_counts = _summary_counts(work / "out-short")
    check("short line counts", short_counts == (3, 2, 1), f"counts={short_counts}")
    short_summary = _summary(work / "out-short")
    check(
        "short line code",
        short_summary["issues"]["by_code"].get("fixed_width_short_line") == 1,
        f"by_code={short_summary['issues']['by_code']}",
    )
    check(
        "short line rows",
        len(_stored_rows(work / "out-short")) == 2,
        "rejected row reached the target",
    )
    scenarios["short_line"] = {"counts": list(short_counts)}

    # 8. A cp1252 layout preserves non-ASCII text; padding becomes null.
    accented = dict(RECORDS[0])
    accented["transaction_id"] = "44444444-4444-4444-8444-444444444444"
    accented["counterparty"] = "Müller & Söhne GmbH"
    accented["description"] = ""
    latin_path = work / "latin.dat"
    latin_path.write_bytes(_fixed_text([accented]).encode("cp1252"))
    latin_layout = work / "layout-cp1252.json"
    latin_layout.write_text(json.dumps(_layout_spec(encoding="cp1252")), encoding="utf-8")
    code, text = _run(
        [
            "--input", str(latin_path),
            "--fixed-width", str(latin_layout),
            "--output-dir", str(work / "out-latin"),
        ]
    )
    check("cp1252 exit", code == 0, f"exit={code}\n{text[-500:]}")
    latin_counts = _summary_counts(work / "out-latin")
    check("cp1252 counts", latin_counts == (1, 1, 0), f"counts={latin_counts}")
    latin_stored = _stored_rows(work / "out-latin")
    check(
        "cp1252 values",
        len(latin_stored) == 1
        and latin_stored[0][7] == "Müller & Söhne GmbH"
        and latin_stored[0][8] is None,
        f"rows={latin_stored}",
    )
    scenarios["cp1252"] = {"counts": list(latin_counts)}

    # 9. Bad layout specs fail before the run starts.
    overlap = _layout_spec()
    overlap["fields"][1]["start"] = 30
    duplicate = _layout_spec()
    duplicate["fields"][2]["name"] = "amount"
    unknown = _layout_spec()
    unknown["separator"] = ","
    for label, spec, fragment in (
        ("overlap", overlap, "overlaps or precedes"),
        ("duplicate-name", duplicate, "repeats the field name"),
        ("unknown-key", unknown, "unknown fixed-width layout key"),
    ):
        bad_layout = work / f"bad-{label}.json"
        bad_layout.write_text(json.dumps(spec), encoding="utf-8")
        bad_output = work / f"out-bad-{label}"
        code, text = _run(
            [
                "--input", str(twin_dat),
                "--fixed-width", str(bad_layout),
                "--output-dir", str(bad_output),
            ]
        )
        check(f"{label} exit", code == 2, f"exit={code}\n{text[-300:]}")
        check(f"{label} message", fragment in text, text[-300:])
        check(
            f"{label} no output",
            not (bad_output / "airlock_summary.json").exists(),
            "a config error still wrote a summary",
        )
    scenarios["bad_layouts"] = {"checked": 3}

    # 10. Draft inference reads the same layout.
    draft = work / "draft.yaml"
    code, text = _run(
        [
            "infer",
            "--input", str(twin_dat),
            "--fixed-width", str(layout_path),
            "--output", str(draft),
        ]
    )
    infer_exit = code
    check("infer exit", code == 0, f"exit={code}\n{text[-500:]}")
    draft_text = draft.read_text(encoding="utf-8") if draft.exists() else ""
    check(
        "infer fields",
        "transaction_id" in draft_text and "amount" in draft_text,
        f"draft missing fields\n{draft_text[:300]}",
    )
    code, text = _run(
        [
            "--input", str(twin_dat),
            "--fixed-width", str(layout_path),
            "--config", str(draft),
            "--output-dir", str(work / "out-infer"),
        ]
    )
    check("infer draft migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    infer_counts = _summary_counts(work / "out-infer")
    check("infer draft migrate counts", infer_counts == (3, 3, 0), f"counts={infer_counts}")
    scenarios["infer"] = {"exit": infer_exit, "draft_counts": list(infer_counts)}

    evidence = work / "fixed_width_verification.json"
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
        print(f"fixed_width_failed failures={len(failures)} work_dir={work}", file=sys.stderr)
        return 1
    print(f"fixed_width_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
