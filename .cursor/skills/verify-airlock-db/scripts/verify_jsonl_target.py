"""End-to-end check for `airlock-db verify` against JSONL targets.

Migrates fixtures to a JSONL-only target, then requires an untouched file to
verify and altered, deleted, extra, and corrupt rows to fail with exact
counts. Duplicate ids are compared under both conflict policies, the report
names the checked file, a two-target run verifies its database, and
anonymized fields stay excluded from the digests.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TARGET_NAME = "target_migration.jsonl"


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


UNIQUE = [
    _record(1, counterparty="Alpha One"),
    _record(2, counterparty="Beta Two"),
    _record(3, counterparty="Gamma Three"),
    _record(4, counterparty="Delta Four"),
]

DUPLICATE = [
    _record(1, counterparty="Alpha One"),
    _record(1, counterparty="Alpha Two"),
    _record(2, counterparty="Beta Two"),
]


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _rewrite_field(path: Path, name: str, value: str) -> None:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        row[name] = value
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that JSONL-only targets verify, and that tampering fails."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="airlock-jsonl-target-"))
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    unique = work / "unique.jsonl"
    _write_jsonl(unique, UNIQUE)
    duplicate = work / "duplicate.jsonl"
    _write_jsonl(duplicate, DUPLICATE)

    def _migrate(source: Path, output: Path, *extra: str) -> tuple[int, str]:
        return _run(
            [
                "--input", str(source),
                "--output-format", "jsonl",
                "--output-dir", str(output),
                *extra,
            ]
        )

    def _copy(name: str, source_dir: Path) -> Path:
        destination = work / name
        shutil.copytree(source_dir, destination)
        return destination

    # 1. A JSONL-only run writes the file and no database.
    clean_out = work / "out-unique"
    code, text = _migrate(unique, clean_out)
    check("jsonl migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    check(
        "no database written",
        not (clean_out / "target_migration.db").exists(),
        "a database appeared in a JSONL-only run",
    )
    target = clean_out / TARGET_NAME
    check(
        "jsonl rows",
        len(target.read_text(encoding="utf-8").splitlines()) == 4,
        "unexpected JSONL row count",
    )

    # 2. The untouched file verifies.
    code, text = _verify(clean_out, unique)
    check("clean verify exit", code == 0, f"exit={code}\n{text[-500:]}")
    report = _report(clean_out)
    records = report["records"]
    check(
        "clean target block",
        report["target"] == {"jsonl": str(target)},
        str(report["target"]),
    )
    check("clean status", report["status"] == "consistent", str(report["status"]))
    check(
        "clean counts",
        records["accepted"] == 4
        and records["distinct_ids"] == 4
        and records["stored"] == 4
        and records["stored_ids"] == 4
        and records["collapsed"] == 0
        and records["collapsed_stored"] == 0
        and records["matched"] == 4
        and records["changed"] == 0
        and records["missing"] == 0
        and records["unexpected"] == 0,
        str(records),
    )
    scenarios["clean"] = {
        "matched": records["matched"],
        "changed": records["changed"],
        "missing": records["missing"],
        "unexpected": records["unexpected"],
    }

    # 3. An altered row is a changed row.
    changed_out = _copy("out-changed", clean_out)
    changed_target = changed_out / TARGET_NAME
    changed_target.write_text(
        changed_target.read_text(encoding="utf-8").replace("Beta Two", "Beta Two X", 1),
        encoding="utf-8",
    )
    code, text = _verify(changed_out, unique)
    check("changed exit", code == 1, f"exit={code}\n{text[-300:]}")
    report = _report(changed_out)
    records = report["records"]
    check(
        "changed counts",
        records["changed"] == 1
        and records["matched"] == 3
        and records["missing"] == 0
        and records["unexpected"] == 0,
        str(records),
    )
    check(
        "changed sample",
        any(
            sample["id"] == "00000002-1111-4111-8111-111111111111"
            for sample in report["samples"]["changed"]
        ),
        str(report["samples"]["changed"]),
    )
    scenarios["changed"] = {"changed": records["changed"]}

    # 4. A deleted row is a missing row.
    deleted_out = _copy("out-deleted", clean_out)
    deleted_target = deleted_out / TARGET_NAME
    deleted_target.write_text(
        "".join(
            line + "\n"
            for line in deleted_target.read_text(encoding="utf-8").splitlines()
            if "Gamma Three" not in line
        ),
        encoding="utf-8",
    )
    code, text = _verify(deleted_out, unique)
    check("deleted exit", code == 1, f"exit={code}\n{text[-300:]}")
    report = _report(deleted_out)
    records = report["records"]
    check(
        "deleted counts",
        records["missing"] == 1
        and records["matched"] == 3
        and records["changed"] == 0
        and records["unexpected"] == 0
        and records["stored"] == 3,
        str(records),
    )
    check(
        "deleted sample",
        any(
            sample["id"] == "00000003-1111-4111-8111-111111111111"
            for sample in report["samples"]["missing"]
        ),
        str(report["samples"]["missing"]),
    )
    scenarios["deleted"] = {"missing": records["missing"]}

    # 5. An appended row is an unexpected row.
    extra_out = _copy("out-extra", clean_out)
    extra_target = extra_out / TARGET_NAME
    with extra_target.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(_record(9, counterparty="Extra Nine"), separators=(",", ":"))
            + "\n"
        )
    code, text = _verify(extra_out, unique)
    check("extra exit", code == 1, f"exit={code}\n{text[-300:]}")
    report = _report(extra_out)
    records = report["records"]
    check(
        "extra counts",
        records["unexpected"] == 1
        and records["matched"] == 4
        and records["changed"] == 0
        and records["missing"] == 0
        and records["stored"] == 5,
        str(records),
    )
    check(
        "extra sample",
        any(
            sample["id"] == "00000009-1111-4111-8111-111111111111"
            for sample in report["samples"]["unexpected"]
        ),
        str(report["samples"]["unexpected"]),
    )
    scenarios["extra"] = {"unexpected": records["unexpected"]}

    # 6. A corrupt line fails with the line number.
    corrupt_out = _copy("out-corrupt", clean_out)
    corrupt_target = corrupt_out / TARGET_NAME
    corrupt_lines = corrupt_target.read_text(encoding="utf-8").splitlines()
    corrupt_lines[1] = "{not json"
    corrupt_target.write_text("\n".join(corrupt_lines) + "\n", encoding="utf-8")
    code, text = _verify(corrupt_out, unique)
    check("corrupt exit", code == 1, f"exit={code}\n{text[-300:]}")
    check(
        "corrupt message",
        "cannot parse" in text and "line 2" in text,
        text[-300:],
    )
    scenarios["corrupt"] = {"exit": code}

    # 7. Blank lines are ignored.
    blank_out = _copy("out-blank", clean_out)
    blank_target = blank_out / TARGET_NAME
    blank_target.write_text(
        blank_target.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    code, text = _verify(blank_out, unique)
    check("blank line exit", code == 0, f"exit={code}\n{text[-300:]}")
    scenarios["blank_line"] = {"exit": code}

    # 8. A two-target run verifies its database, not the file.
    both_out = work / "out-both"
    code, text = _run(
        [
            "--input", str(unique),
            "--output-format", "both",
            "--output-dir", str(both_out),
        ]
    )
    check("both migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    code, text = _verify(both_out, unique)
    check("both verify exit", code == 0, f"exit={code}\n{text[-500:]}")
    both_target = _report(both_out)["target"]
    check("both target block", "database" in both_target, str(both_target))
    scenarios["both"] = {"target": sorted(both_target)}

    # 9. Anonymized runs keep the excluded fields out of the digests.
    anon_out = work / "out-anon"
    code, text = _run(
        [
            "--input", str(unique),
            "--anonymize",
            "--output-format", "jsonl",
            "--output-dir", str(anon_out),
        ]
    )
    check("anon migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    code, text = _verify(anon_out, unique)
    check("anon verify exit", code == 0, f"exit={code}\n{text[-500:]}")
    report = _report(anon_out)
    check(
        "anon fields",
        report["anonymized"] is True and report["excluded_fields"] != [],
        str(report["excluded_fields"]),
    )
    anon_changed = _copy("out-anon-changed", anon_out)
    anon_target = anon_changed / TARGET_NAME
    anon_target.write_text(
        anon_target.read_text(encoding="utf-8").replace(
            '"amount":"125.50"', '"amount":"999.00"', 1
        ),
        encoding="utf-8",
    )
    code, text = _verify(anon_changed, unique)
    check("anon changed exit", code == 1, f"exit={code}\n{text[-300:]}")
    anon_changed_records = _report(anon_changed)["records"]
    check(
        "anon changed counts",
        anon_changed_records["changed"] == 1,
        str(anon_changed_records),
    )
    anon_token = _copy("out-anon-token", anon_out)
    _rewrite_field(anon_token / TARGET_NAME, "counterparty", "CP-REWRITTEN-TOKEN")
    token_exit, text = _verify(anon_token, unique)
    check("anon token exit", token_exit == 0, f"exit={token_exit}\n{text[-300:]}")
    scenarios["anonymized"] = {
        "excluded": report["excluded_fields"],
        "business_field_change": anon_changed_records["changed"],
        "token_exit": token_exit,
    }

    # 10. Duplicate ids follow the recorded conflict policy.
    dup_out = work / "out-dup"
    code, text = _migrate(duplicate, dup_out)
    check("duplicate migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    dup_target = dup_out / TARGET_NAME
    check(
        "duplicate rows",
        len(dup_target.read_text(encoding="utf-8").splitlines()) == 3,
        "duplicate rows were filtered by the writer",
    )
    code, text = _verify(dup_out, duplicate)
    check("duplicate skip exit", code == 0, f"exit={code}\n{text[-500:]}")
    records = _report(dup_out)["records"]
    check(
        "duplicate skip counts",
        records["accepted"] == 3
        and records["distinct_ids"] == 2
        and records["collapsed"] == 1
        and records["stored"] == 3
        and records["stored_ids"] == 2
        and records["collapsed_stored"] == 1
        and records["matched"] == 2,
        str(records),
    )
    code, text = _verify(dup_out, duplicate, "--conflict-policy", "replace")
    check("duplicate replace exit", code == 0, f"exit={code}\n{text[-500:]}")
    check(
        "replace warning",
        "recorded conflict policy 'skip'" in text,
        text[-300:],
    )

    dup_first = _copy("out-dup-first", dup_out)
    first_target = dup_first / TARGET_NAME
    first_target.write_text(
        first_target.read_text(encoding="utf-8").replace("Alpha One", "Alpha One X", 1),
        encoding="utf-8",
    )
    code, text = _verify(dup_first, duplicate)
    check("first duplicate altered skip exit", code == 1, f"exit={code}\n{text[-300:]}")
    check(
        "first duplicate altered skip counts",
        _report(dup_first)["records"]["changed"] == 1
        and _report(dup_first)["records"]["matched"] == 1,
        str(_report(dup_first)["records"]),
    )
    code, text = _verify(dup_first, duplicate, "--conflict-policy", "replace")
    check(
        "first duplicate altered replace exit",
        code == 0,
        f"exit={code}\n{text[-300:]}",
    )

    dup_last = _copy("out-dup-last", dup_out)
    last_target = dup_last / TARGET_NAME
    last_target.write_text(
        last_target.read_text(encoding="utf-8").replace("Alpha Two", "Alpha Two X", 1),
        encoding="utf-8",
    )
    code, text = _verify(dup_last, duplicate)
    check("last duplicate altered skip exit", code == 0, f"exit={code}\n{text[-300:]}")
    code, text = _verify(dup_last, duplicate, "--conflict-policy", "replace")
    check(
        "last duplicate altered replace exit",
        code == 1,
        f"exit={code}\n{text[-300:]}",
    )
    check(
        "last duplicate altered replace counts",
        _report(dup_last)["records"]["changed"] == 1,
        str(_report(dup_last)["records"]),
    )

    dup_deleted = _copy("out-dup-deleted", dup_out)
    deleted_duplicate_target = dup_deleted / TARGET_NAME
    deleted_duplicate_target.write_text(
        "".join(
            line + "\n"
            for line in deleted_duplicate_target.read_text(
                encoding="utf-8"
            ).splitlines()
            if "Alpha Two" not in line
        ),
        encoding="utf-8",
    )
    code, text = _verify(dup_deleted, duplicate)
    check(
        "deleted duplicate skip exit", code == 0, f"exit={code}\n{text[-300:]}"
    )
    code, text = _verify(dup_deleted, duplicate, "--conflict-policy", "replace")
    check(
        "deleted duplicate replace exit",
        code == 1,
        f"exit={code}\n{text[-300:]}",
    )
    records = _report(dup_deleted)["records"]
    check(
        "deleted duplicate replace counts",
        records["changed"] == 1 and records["missing"] == 0,
        str(records),
    )
    scenarios["duplicates"] = {"skip": "first", "replace": "last"}

    # 11. Neither target exists.
    empty_out = work / "out-empty"
    empty_out.mkdir()
    code, text = _verify(empty_out, unique)
    check("no target exit", code == 1, f"exit={code}\n{text[-300:]}")
    check("no target message", "nothing to verify" in text, text[-300:])
    scenarios["missing_target"] = {"exit": code}

    evidence = work / "jsonl_target_verification.json"
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
            f"jsonl_target_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"jsonl_target_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
