"""End-to-end check for the source manifest that `verify` reconciles.

Migrates fixtures, requires the recorded manifest to match an independent
SHA-256 of every source file, and requires an untouched copy and an old
summary (one without a manifest) to keep verifying. A same-size swapped
source must fail `verify` with the file named even though every row still
matches, and deleted and added files must land in the manifest report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _record(index: int, *, counterparty: str, amount: str = "125.50") -> dict:
    return {
        "transaction_id": f"{index:08d}-1111-4111-8111-111111111111",
        "account_id": f"ACC-10{index:02d}",
        "posted_at": "2026-01-15T09:30:00Z",
        "amount": amount,
        "currency": "USD",
        "transaction_type": "DEBIT",
        "status": "POSTED",
        "counterparty": counterparty,
        "description": f"Record {index}",
    }


RECORDS = [
    _record(1, counterparty="Alpha One"),
    _record(2, counterparty="Beta Two"),
    _record(3, counterparty="Gamma Three"),
]


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _independent(path: Path) -> dict:
    data = path.read_bytes()
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, result.stdout + result.stderr


def _verify(output_dir: Path, source: Path, *extra: str) -> tuple[int, str]:
    return _run(
        ["verify", "--input", str(source), "--output-dir", str(output_dir), *extra]
    )


def _report(output_dir: Path) -> dict:
    return json.loads((output_dir / "airlock_verify.json").read_text(encoding="utf-8"))


def _manifest(output_dir: Path) -> list:
    summary = json.loads(
        (output_dir / "airlock_summary.json").read_text(encoding="utf-8")
    )
    return summary.get("source_manifest")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that source manifests pin the exact bytes a run read."
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
        else Path(tempfile.mkdtemp(prefix="airlock-source-manifest-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    def _migrate(source: Path, output: Path, *extra: str) -> tuple[int, str]:
        return _run(
            ["--input", str(source), "--output-dir", str(output), *extra]
        )

    def _copy(name: str, source_dir: Path) -> Path:
        destination = work / name
        shutil.copytree(source_dir, destination)
        return destination

    records = work / "records.jsonl"
    _write_jsonl(records, RECORDS)

    # 1. The recorded manifest matches an independent hash of the source.
    file_out = work / "out-file"
    code, text = _migrate(records, file_out)
    check("file migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    entries = _manifest(file_out)
    independent = _independent(records)
    check("file manifest count", entries is not None and len(entries) == 1, str(entries))
    if entries:
        entry = entries[0]
        check("file manifest label", entry.get("label") == str(records), str(entry))
        check(
            "file manifest size",
            entry.get("size") == independent["size"],
            f"{entry.get('size')} != {independent['size']}",
        )
        check(
            "file manifest sha256",
            entry.get("sha256") == independent["sha256"],
            f"{entry.get('sha256')} != {independent['sha256']}",
        )
    code, text = _verify(file_out, records)
    check("file verify exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("file verify message", "Verification: consistent" in text, text[-400:])
    check(
        "file verify manifest line",
        "Source manifest: 1 file(s) matched" in text,
        text[-400:],
    )
    block = _report(file_out)["source_manifest"]
    check(
        "file manifest block",
        block == {
            "status": "matched",
            "checked": 1,
            "matched": 1,
            "changed": [],
            "missing": [],
            "extra": [],
        },
        str(block),
    )
    scenarios["file"] = {
        "size": independent["size"],
        "sha256": independent["sha256"][:16],
        "status": block.get("status"),
    }

    # 2. An untouched copy of the handoff verifies.
    copy_out = _copy("out-copy", file_out)
    code, text = _verify(copy_out, records)
    check("copy verify exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "copy manifest block",
        _report(copy_out)["source_manifest"]["status"] == "matched",
        str(_report(copy_out)["source_manifest"]),
    )
    scenarios["copy"] = {"exit": code}

    # 3. A directory scan records one entry per stored file.
    scan_dir = work / "scan"
    scan_dir.mkdir()
    first = scan_dir / "a.jsonl"
    second = scan_dir / "b.jsonl"
    _write_jsonl(first, RECORDS[:1])
    _write_jsonl(second, RECORDS[1:])
    dir_out = work / "out-dir"
    code, text = _migrate(scan_dir, dir_out)
    check("directory migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    entries = _manifest(dir_out)
    check("directory entries", entries is not None and len(entries) == 2, str(entries))
    for path in (first, second):
        entry = next(
            (
                item
                for item in (entries or [])
                if isinstance(item, dict) and item.get("label") == str(path)
            ),
            None,
        )
        independent = _independent(path)
        check(
            f"directory {path.name} digest",
            entry is not None
            and entry.get("size") == independent["size"]
            and entry.get("sha256") == independent["sha256"],
            str(entry),
        )
    code, text = _verify(dir_out, scan_dir)
    check("directory verify exit", code == 0, f"exit={code}\n{text[-400:]}")
    block = _report(dir_out)["source_manifest"]
    check(
        "directory manifest block",
        block.get("checked") == 2 and block.get("matched") == 2,
        str(block),
    )
    scenarios["directory"] = {"entries": len(entries or [])}

    # 4. A same-size source swap fails the manifest while every row matches.
    rows = [
        json.loads(line)
        for line in records.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    original_line = json.dumps(rows[0], separators=(",", ":"))
    swapped = {name: rows[0][name] for name in reversed(list(rows[0]))}
    swapped_line = json.dumps(swapped, separators=(",", ":"))
    check(
        "swap keeps the byte count",
        len(swapped_line) == len(original_line),
        f"{len(swapped_line)} != {len(original_line)}",
    )
    size_before = records.stat().st_size
    records.write_text(
        records.read_text(encoding="utf-8").replace(original_line, swapped_line),
        encoding="utf-8",
    )
    check(
        "swap keeps the file size",
        records.stat().st_size == size_before,
        f"{records.stat().st_size} != {size_before}",
    )
    code, text = _verify(file_out, records)
    check("swap verify exit", code == 1, f"exit={code}\n{text[-500:]}")
    check("swap names the file", f"changed: {records}" in text, text[-500:])
    check(
        "swap rows still matched",
        "Matched: 3; changed: 0; missing: 0; unexpected: 0" in text,
        text[-400:],
    )
    report = _report(file_out)
    check("swap report status", report["status"] == "mismatch", str(report["status"]))
    check(
        "swap rows report unaffected",
        report["records"]["matched"] == 3 and report["records"]["changed"] == 0,
        str(report["records"]),
    )
    block = report["source_manifest"]
    check(
        "swap manifest block",
        block.get("status") == "mismatch"
        and block.get("changed") == [str(records)]
        and block.get("missing") == []
        and block.get("extra") == [],
        str(block),
    )
    scenarios["swap"] = {"exit": code, "records_matched": report["records"]["matched"]}

    # 5. A summary without a manifest keeps verifying, with a notice.
    old_out = _copy("out-old-summary", file_out)
    summary_path = old_out / "airlock_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.pop("source_manifest", None)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    code, text = _verify(old_out, records)
    check("old summary exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "old summary notice",
        "Source manifest: skipped" in text and "no source manifest" in text,
        text[-400:],
    )
    check(
        "old summary message",
        "Verification: consistent" in text,
        text[-400:],
    )
    block = _report(old_out)["source_manifest"]
    check(
        "old summary block",
        block
        == {
            "status": "skipped",
            "notice": "the run summary has no source manifest",
        },
        str(block),
    )
    scenarios["old_summary"] = {"exit": code}

    # 6. An anonymized run keeps labels only, and verify skips the check.
    anon_out = work / "out-anon"
    code, text = _migrate(records, anon_out, "--anonymize")
    check("anonymized migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    entries = _manifest(anon_out)
    check("anonymized entries", entries is not None and len(entries) == 1, str(entries))
    keys = sorted(entries[0].keys()) if entries else []
    check("anonymized keys", keys == ["label"], str(entries))
    check(
        "anonymized label is a token",
        entries is not None and entries and entries[0].get("label") != str(records),
        str(entries),
    )
    code, text = _verify(anon_out, records, "--anonymized")
    check("anonymized verify exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "anonymized skip notice",
        "Source manifest: skipped" in text and "anonymized" in text,
        text[-400:],
    )
    block = _report(anon_out)["source_manifest"]
    check(
        "anonymized block",
        block.get("status") == "skipped"
        and "labels only" in str(block.get("notice")),
        str(block),
    )
    scenarios["anonymized"] = {"keys": keys}

    # 7. A deleted source lands in the manifest report.
    records.unlink()
    code, text = _verify(file_out, records)
    check("deleted verify exit", code == 1, f"exit={code}\n{text[-500:]}")
    check("deleted names the file", f"missing: {records}" in text, text[-500:])
    block = _report(file_out)["source_manifest"]
    check(
        "deleted manifest block",
        block.get("missing") == [str(records)] and block.get("changed") == [],
        str(block),
    )
    scenarios["deleted"] = {"exit": code, "missing": len(block.get("missing") or [])}

    # 8. An added file lands in the manifest report even when rows still match.
    added = scan_dir / "c.jsonl"
    _write_jsonl(added, RECORDS[:1])
    code, text = _verify(dir_out, scan_dir)
    check("added verify exit", code == 1, f"exit={code}\n{text[-500:]}")
    check("added names the file", f"extra: {added}" in text, text[-500:])
    check(
        "added rows still matched",
        "Matched: 3; changed: 0; missing: 0; unexpected: 0" in text,
        text[-400:],
    )
    check(
        "added manifest verdict",
        "Verification: mismatch (source manifest differs)" in text,
        text[-400:],
    )
    report = _report(dir_out)
    check(
        "added manifest block",
        report["source_manifest"].get("extra") == [str(added)],
        str(report["source_manifest"]),
    )
    check(
        "added records report",
        report["records"]["matched"] == 3
        and report["records"]["missing"] == 0
        and report["records"]["unexpected"] == 0,
        str(report["records"]),
    )
    scenarios["added"] = {"exit": code, "extra": len(report["source_manifest"]["extra"])}

    # 9. A JSONL-only run records and checks the manifest too.
    jsonl_source = work / "jsonl-source.jsonl"
    _write_jsonl(jsonl_source, RECORDS)
    jsonl_out = work / "out-jsonl"
    code, text = _run(
        [
            "--input",
            str(jsonl_source),
            "--output-format",
            "jsonl",
            "--output-dir",
            str(jsonl_out),
        ]
    )
    check("jsonl migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    entries = _manifest(jsonl_out)
    independent = _independent(jsonl_source)
    check(
        "jsonl manifest digest",
        entries is not None
        and len(entries) == 1
        and entries[0].get("sha256") == independent["sha256"],
        str(entries),
    )
    code, text = _verify(jsonl_out, jsonl_source)
    check("jsonl verify exit", code == 0, f"exit={code}\n{text[-400:]}")
    block = _report(jsonl_out)["source_manifest"]
    check(
        "jsonl manifest block",
        block.get("status") == "matched" and block.get("matched") == 1,
        str(block),
    )
    scenarios["jsonl_format"] = {"matched": block.get("matched")}

    evidence = work / "source_manifest_verification.json"
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
            f"source_manifest_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"source_manifest_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
