"""End-to-end check for the migration `--dry-run` flag.

Requires a clean source to exit 0 with no files written, and the messy fixture
to exit 1 with the same counts a real migration produces. A dry run against an
existing output directory must leave every file byte-identical and create no
target, report, summary, or repair log. Each write flag is refused, a bad
config and a missing source fail without writing, and `--output-format jsonl`
still writes no JSONL target.
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
MESSY = REPO_ROOT / "examples" / "messy_transactions.json"
CONSTRAINTS = REPO_ROOT / "examples" / "constraints.json"
CLEAN = REPO_ROOT / "examples" / "real_data" / "online_retail_sample.jsonl"
CLEAN_SCHEMA = REPO_ROOT / "examples" / "real_data" / "online_retail_schema.json"


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


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _token(text: str) -> dict[str, str]:
    for line in text.splitlines():
        if line.startswith("dry_run_result "):
            return dict(part.split("=", 1) for part in line.split()[1:])
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that migration --dry-run reports the real outcome and writes nothing."
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
        else Path(tempfile.mkdtemp(prefix="airlock-dry-run-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    # 1. A clean source exits 0 and creates no output directory.
    clean_out = work / "clean" / "out"
    code, text = _run(
        [
            "--input",
            str(CLEAN),
            "--config",
            str(CLEAN_SCHEMA),
            "--dry-run",
            "--output-dir",
            str(clean_out),
        ]
    )
    check("clean exit", code == 0, f"exit={code}\n{text[-500:]}")
    token = _token(text)
    check("clean checked", token.get("checked") == "10", text[-500:])
    check("clean valid", token.get("valid") == "10", text[-500:])
    check("clean invalid", token.get("invalid") == "0", text[-500:])
    check("clean no files", not clean_out.parent.exists(), f"{clean_out.parent} exists")
    check(
        "clean notice",
        "Dry run: no files were written." in text,
        text[-500:],
    )
    scenarios["clean"] = token

    # 2. The messy fixture exits 1 with the real migration counts (32/8/24).
    messy_out = work / "messy" / "out"
    code, text = _run(
        [
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--dry-run",
            "--output-dir",
            str(messy_out),
        ]
    )
    check("messy exit", code == 1, f"exit={code}\n{text[-500:]}")
    token = _token(text)
    check("messy checked", token.get("checked") == "32", text[-500:])
    check("messy valid", token.get("valid") == "8", text[-500:])
    check("messy invalid", token.get("invalid") == "24", text[-500:])
    check("messy issues", token.get("issue_occurrences") == "26", text[-500:])
    check("messy no files", not messy_out.parent.exists(), f"{messy_out.parent} exists")
    check("messy breakdown", "Issues: " in text and "value_error=6" in text, text[-500:])
    scenarios["messy"] = token

    # 3. A real migration reports the same counts.
    real_out = work / "real"
    code, text = _run(
        [
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--output-dir",
            str(real_out),
        ]
    )
    check("real exit", code == 1, f"exit={code}\n{text[-400:]}")
    check("real counts", "Records checked: 32" in text, text[-400:])
    check("real valid", "Valid: 8; invalid: 24" in text, text[-400:])
    check(
        "real target written",
        (real_out / "target_migration.db").is_file(),
        f"missing {real_out / 'target_migration.db'}",
    )
    scenarios["real_parity"] = {"checked": 32, "valid": 8, "invalid": 24}

    # 4. An existing output directory stays byte-identical and gains no files.
    existing = work / "existing"
    existing.mkdir(parents=True)
    (existing / "sentinel.txt").write_text("keep me\n", encoding="utf-8")
    (existing / "airlock_summary.json").write_text('{"keep": true}\n', encoding="utf-8")
    before = _tree_hashes(existing)
    code, text = _run(
        [
            "--input",
            str(MESSY),
            "--constraints",
            str(CONSTRAINTS),
            "--dry-run",
            "--output-dir",
            str(existing),
        ]
    )
    check("existing exit", code == 1, f"exit={code}\n{text[-400:]}")
    check("existing untouched", _tree_hashes(existing) == before, "a file changed")
    check(
        "existing no target",
        not (existing / "target_migration.db").exists(),
        "a target appeared",
    )
    check(
        "existing no report",
        not (existing / "airlock_report.html").exists(),
        "a report appeared",
    )
    scenarios["existing_dir"] = {"files": sorted(before)}

    # 5. Each write flag is refused before anything runs.
    refused = [
        ("--error-log", ["--error-log"]),
        ("--commit-every", ["--commit-every", "10"]),
        ("--resume", ["--resume"]),
        ("--sample", ["--sample", "5"]),
    ]
    for flag, extra in refused:
        out = work / f"refused-{flag.strip('-')}" / "out"
        code, text = _run(
            [
                "--input",
                str(MESSY),
                "--dry-run",
                *extra,
                "--output-dir",
                str(out),
            ]
        )
        check(f"{flag} exit", code == 2, f"exit={code}\n{text[-300:]}")
        check(f"{flag} message", flag in text, text[-300:])
        check(f"{flag} no files", not out.parent.exists(), f"{out.parent} exists")
    scenarios["refusals"] = [flag for flag, _ in refused]

    # 6. A bad config exits 2 without creating the output directory.
    broken = work / "broken.yaml"
    broken.write_text("fields: [unclosed\n", encoding="utf-8")
    bad_out = work / "bad-config" / "out"
    code, text = _run(
        [
            "--input",
            str(MESSY),
            "--config",
            str(broken),
            "--dry-run",
            "--output-dir",
            str(bad_out),
        ]
    )
    check("bad config exit", code == 2, f"exit={code}\n{text[-400:]}")
    check("bad config no files", not bad_out.parent.exists(), f"{bad_out.parent} exists")
    scenarios["bad_config"] = {"exit": code}

    # 7. A missing source reports a source error and still writes nothing.
    missing_out = work / "missing" / "out"
    code, text = _run(
        [
            "--input",
            str(work / "absent.jsonl"),
            "--dry-run",
            "--output-dir",
            str(missing_out),
        ]
    )
    check("missing exit", code == 1, f"exit={code}\n{text[-400:]}")
    check("missing source error", "Source errors: 1" in text, text[-400:])
    check("missing no files", not missing_out.parent.exists(), f"{missing_out.parent} exists")
    scenarios["missing_source"] = _token(text)

    # 8. A JSONL output format is ignored, so no JSONL target appears.
    jsonl_out = work / "jsonl" / "out"
    code, text = _run(
        [
            "--input",
            str(CLEAN),
            "--config",
            str(CLEAN_SCHEMA),
            "--output-format",
            "jsonl",
            "--dry-run",
            "--output-dir",
            str(jsonl_out),
        ]
    )
    check("jsonl exit", code == 0, f"exit={code}\n{text[-400:]}")
    check("jsonl no files", not jsonl_out.parent.exists(), f"{jsonl_out.parent} exists")
    scenarios["jsonl_format"] = _token(text)

    evidence = work / "dry_run_verification.json"
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
            f"dry_run_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"dry_run_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
