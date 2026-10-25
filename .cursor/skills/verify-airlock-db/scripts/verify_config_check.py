"""End-to-end check for `airlock-db check`.

Requires a valid schema, constraints file, and fixed-width layout to load
together and print their counts and fingerprints. The dynamic schema
fingerprint must match the one a real migration writes to its summary. Bad
files, the mixed built-in plus dynamic rule, missing flags, and an absent file
must each exit 2 without printing a partial schema line.
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
SCHEMA = REPO_ROOT / "examples" / "schema_config.json"
BUILTIN_CONSTRAINTS = REPO_ROOT / "examples" / "constraints.json"


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


def _token(text: str) -> dict[str, str]:
    for line in text.splitlines():
        if line.startswith("check_ok "):
            return dict(part.split("=", 1) for part in line.split()[1:])
    return {}


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that airlock-db check validates configuration without writing."
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
        else Path(tempfile.mkdtemp(prefix="airlock-check-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    field_rules = _write(
        work / "field_rules.json",
        json.dumps(
            {
                "field_rules": [
                    {"field": "priority", "enum": ["LOW", "NORMAL", "HIGH"]},
                    {"field": "estimated_cost", "min": 0.01, "max": 1000.0},
                    {"field": "customer_name", "pattern": "^[A-Z][A-Za-z ]*$"},
                    {"required_any_of": ["estimated_cost", "notes"]},
                ]
            }
        ),
    )
    layout = _write(
        work / "layout.yaml",
        "encoding: utf-8-sig\n"
        "skip_lines: 1\n"
        "fields:\n"
        "  - name: ticket_id\n"
        "    start: 1\n"
        "    length: 10\n"
        "  - name: customer_name\n"
        "    start: 11\n"
        "    length: 20\n"
        "  - name: priority\n"
        "    start: 31\n"
        "    length: 8\n",
    )
    transforms = _write(
        work / "transforms.yaml",
        "version: 1\n"
        "model_name: CheckRecord\n"
        "id_field: record_id\n"
        "strip_whitespace: false\n"
        "fields:\n"
        "  record_id:\n"
        "    type: string\n"
        "    required: true\n"
        "  name:\n"
        "    type: string\n"
        "    required: true\n"
        "    transforms:\n"
        "      - trim: true\n"
        "      - lowercase: true\n",
    )

    # 1. Every file kind loads together and reports its counts.
    code, text = _run(
        [
            "check",
            "--config",
            str(SCHEMA),
            "--constraints",
            str(field_rules),
            "--fixed-width",
            str(layout),
        ]
    )
    check("all valid exit", code == 0, f"exit={code}\n{text[-500:]}")
    token = _token(text)
    check("all valid schema", token.get("schema") == "dynamic", text[-500:])
    check("all valid fields", token.get("schema_fields") == "6", text[-500:])
    check("all valid transforms", token.get("transforms") == "0", text[-500:])
    check("all valid field rules", token.get("field_rules") == "4", text[-500:])
    check("all valid layout", token.get("layout_fields") == "3", text[-500:])
    check("all valid name", "Schema: SupportTicket" in text, text[-500:])
    check("all valid layout detail", "record_length 38" in text, text[-500:])
    check(
        "all valid notice",
        "Check: configuration is valid. No files were written." in text,
        text[-500:],
    )
    scenarios["all_valid"] = token

    # 2. A config with transforms counts every rule.
    code, text = _run(["check", "--config", str(transforms)])
    check("transforms exit", code == 0, f"exit={code}\n{text[-500:]}")
    token = _token(text)
    check("transforms fields", token.get("schema_fields") == "2", text[-500:])
    check("transforms count", token.get("transforms") == "2", text[-500:])
    scenarios["transforms"] = token

    # 3. Built-in constraints validate against the built-in schema.
    code, text = _run(["check", "--constraints", str(BUILTIN_CONSTRAINTS)])
    check("builtin exit", code == 0, f"exit={code}\n{text[-500:]}")
    token = _token(text)
    check("builtin schema", token.get("schema") == "builtin", text[-500:])
    check("builtin fields", token.get("schema_fields") == "9", text[-500:])
    check("builtin rules", token.get("builtin_rules") == "5", text[-500:])
    check(
        "builtin fingerprint line",
        "Constraints: 5 built-in rules, 0 field rules (fingerprint " in text,
        text[-500:],
    )
    scenarios["builtin_constraints"] = token

    # 4. The dynamic fingerprint matches a real migration summary.
    tickets = _write(
        work / "tickets.json",
        json.dumps(
            [
                {
                    "ticket_id": "TKT-000001",
                    "customer_name": "Alpha",
                    "priority": "LOW",
                    "estimated_cost": "12.50",
                    "opened_at": "2026-01-15T09:30:00Z",
                    "notes": "first",
                }
            ]
        ),
    )
    parity_out = work / "parity"
    code, text = _run(
        [
            "--input",
            str(tickets),
            "--config",
            str(SCHEMA),
            "--constraints",
            str(field_rules),
            "--output-dir",
            str(parity_out),
        ]
    )
    check("parity migration exit", code == 0, f"exit={code}\n{text[-500:]}")
    summary = json.loads((parity_out / "airlock_summary.json").read_text(encoding="utf-8"))
    fingerprint = summary["schema"]["fingerprint"]
    code, text = _run(
        [
            "check",
            "--config",
            str(SCHEMA),
            "--constraints",
            str(field_rules),
        ]
    )
    check("parity check exit", code == 0, f"exit={code}\n{text[-500:]}")
    check(
        "parity fingerprint",
        f"fingerprint {fingerprint}" in text,
        f"missing {fingerprint}\n{text[-500:]}",
    )
    check(
        "parity field rules",
        _token(text).get("field_rules") == "4",
        text[-500:],
    )
    scenarios["fingerprint_parity"] = {"fingerprint": fingerprint}

    # 5. Bad files exit 2 without a partial schema line.
    bad_schema = _write(work / "bad_schema.yaml", "fields: [unclosed\n")
    bad_constraints = _write(work / "bad_constraints.json", '{"allowed_currencies": "USD"}')
    bad_layout = _write(
        work / "bad_layout.yaml",
        "fields:\n"
        "  - name: left\n"
        "    start: 1\n"
        "    length: 5\n"
        "  - name: right\n"
        "    start: 3\n"
        "    length: 5\n",
    )
    for label, flag, path in (
        ("schema", "--config", bad_schema),
        ("constraints", "--constraints", bad_constraints),
        ("layout", "--fixed-width", bad_layout),
    ):
        code, text = _run(["check", flag, str(path)])
        check(f"bad {label} exit", code == 2, f"exit={code}\n{text[-400:]}")
        check(f"bad {label} message", "airlock-db:" in text, text[-400:])
        check(
            f"bad {label} no partial output",
            "Schema:" not in text,
            text[-400:],
        )
    scenarios["bad_files"] = ["schema", "constraints", "layout"]

    # 6. Built-in constraints with a dynamic schema stay refused.
    code, text = _run(
        [
            "check",
            "--config",
            str(SCHEMA),
            "--constraints",
            str(BUILTIN_CONSTRAINTS),
        ]
    )
    check("mixed exit", code == 2, f"exit={code}\n{text[-400:]}")
    check(
        "mixed message",
        "--constraints cannot be combined with --config" in text,
        text[-400:],
    )
    check("mixed no partial output", "Schema:" not in text, text[-400:])
    scenarios["mixed_rules"] = {"exit": code}

    # 7. No flags and a missing file exit 2.
    code, text = _run(["check"])
    check("no flags exit", code == 2, f"exit={code}\n{text[-400:]}")
    check(
        "no flags message",
        "at least one of --config, --constraints, or --fixed-width is required" in text,
        text[-400:],
    )
    code, text = _run(["check", "--config", str(work / "absent.yaml")])
    check("missing file exit", code == 2, f"exit={code}\n{text[-400:]}")
    check("missing file no output", "Schema:" not in text, text[-400:])
    scenarios["refusals"] = {"exit": 2}

    evidence = work / "config_check_verification.json"
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
            f"config_check_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"config_check_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
