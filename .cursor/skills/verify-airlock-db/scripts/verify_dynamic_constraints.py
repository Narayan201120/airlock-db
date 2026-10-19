"""End-to-end check for `field_rules` in constraints files with `--config`.

Builds a dynamic schema fixture, then requires a field_rules-only constraints
file to reject one row per rule kind (plus a row that breaks two kinds at
once) with the exact codes and messages, to let absent, null, and
second-contact rows pass, to refuse malformed rule files with exit 2, and to
combine with --config while the built-in examples file still cannot. The
repair fingerprint must accept the original rules file, reject a changed one,
and verify must reconcile only when the same rules file is supplied.
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
EXAMPLE_CONSTRAINTS = REPO_ROOT / "examples" / "constraints.json"

SCHEMA = {
    "version": 1,
    "model_name": "OrderRow",
    "id_field": "order_id",
    "strict": False,
    "strip_whitespace": True,
    "fields": {
        "order_id": {"type": "string", "required": True, "min_length": 1, "max_length": 40},
        "status": {"type": "string", "required": False, "max_length": 40},
        "quantity": {"type": "integer", "required": False},
        "sku": {"type": "string", "required": False, "max_length": 40},
        "email": {"type": "string", "required": False, "max_length": 120},
        "phone": {"type": "string", "required": False, "max_length": 40},
    },
}

RULES = {
    "field_rules": [
        {"field": "status", "enum": ["open", "closed"]},
        {"field": "quantity", "min": 1, "max": 500},
        {"field": "sku", "pattern": "^SKU-[0-9]{4}$"},
        {"required_any_of": ["email", "phone"]},
    ]
}

# A different rules file: same shape, one changed bound, so the repair
# fingerprint must refuse it.
RULES_VARIANT = {
    "field_rules": [
        {"field": "status", "enum": ["open", "closed"]},
        {"field": "quantity", "min": 1, "max": 400},
        {"field": "sku", "pattern": "^SKU-[0-9]{4}$"},
        {"required_any_of": ["email", "phone"]},
    ]
}

# One valid row, one violation per rule kind, then a row that breaks two
# kinds at once, in source order.
MESSY_ROWS = [
    {"order_id": "ORD-1", "status": "open", "quantity": 10, "sku": "SKU-0001",
     "email": "a@example.com"},
    {"order_id": "ORD-2", "status": "hold", "quantity": 10, "sku": "SKU-0002",
     "email": "b@example.com"},
    {"order_id": "ORD-3", "status": "open", "quantity": 0, "sku": "SKU-0003",
     "email": "c@example.com"},
    {"order_id": "ORD-4", "status": "open", "quantity": 10, "sku": "BAD",
     "email": "d@example.com"},
    {"order_id": "ORD-5", "status": "open", "quantity": 10, "sku": "SKU-0005"},
    {"order_id": "ORD-6", "status": "hold", "quantity": 600, "sku": "SKU-0006",
     "phone": "+15551234567"},
]

CLEAN_ROWS = [
    {"order_id": "ORD-1", "status": "open", "quantity": 1, "sku": "SKU-0001",
     "email": "a@example.com"},
    {"order_id": "ORD-2", "status": "closed", "quantity": 500, "sku": "SKU-0002",
     "phone": "+15551234567"},
    {"order_id": "ORD-3", "status": "open", "quantity": 250, "sku": "SKU-0003",
     "email": "c@example.com"},
]

# The first two rows pass the field rules (absent or null status; the second
# contact field alone satisfies required_any_of). The third fails: its only
# listed field is an empty string.
EDGE_ROWS = [
    {"order_id": "ORD-10", "phone": "+15550000001"},
    {"order_id": "ORD-11", "status": None, "email": "x@example.com"},
    {"order_id": "ORD-12", "status": "open", "quantity": 5, "sku": "SKU-0012",
     "email": ""},
]

BAD_RULES = {
    "unknown_entry_key": (
        {"field_rules": [{"field": "status", "enum": ["open"], "bogus": 1}]},
        "unknown key",
    ),
    "min_above_max": (
        {"field_rules": [{"field": "quantity", "min": 10, "max": 5}]},
        "min must not exceed max",
    ),
    "invalid_regex": (
        {"field_rules": [{"field": "sku", "pattern": "["}]},
        "regular expression",
    ),
    "duplicate_field": (
        {"field_rules": [
            {"field": "status", "enum": ["open"]},
            {"field": "status", "enum": ["closed"]},
        ]},
        "duplicates the rules for field 'status'",
    ),
    "duplicate_enum": (
        {"field_rules": [{"field": "quantity", "enum": [1, "1.0"]}]},
        "duplicate value",
    ),
    "rules_not_a_list": (
        {"field_rules": {"field": "status", "enum": ["open"]}},
        "field_rules must be a JSON array",
    ),
}


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


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _summary(output_dir: Path) -> dict:
    return json.loads((output_dir / "airlock_summary.json").read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify field_rules constraints with dynamic schemas."
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
        else Path(tempfile.mkdtemp(prefix="airlock-dynamic-constraints-"))
    )
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    schema = _write_json(work / "schema.json", SCHEMA)
    rules = _write_json(work / "rules.json", RULES)
    rules_variant = _write_json(work / "rules-variant.json", RULES_VARIANT)
    messy = _write_jsonl(work / "messy.jsonl", MESSY_ROWS)
    clean = _write_jsonl(work / "clean.jsonl", CLEAN_ROWS)
    edge = _write_jsonl(work / "edge.jsonl", EDGE_ROWS)

    # 1. Dynamic migration: one violation per kind plus a row that breaks two
    #    kinds, with the exact codes and messages.
    out_messy = work / "out-messy"
    code, text = _run(
        ["--input", str(messy), "--config", str(schema), "--constraints", str(rules),
         "--error-log", "--output-dir", str(out_messy)]
    )
    check("messy exit", code == 1, f"exit={code}\n{text[-400:]}")
    summary = _summary(out_messy)
    by_code = summary["issues"]["by_code"]
    check(
        "messy by_code",
        by_code
        == {
            "constraint_enum": 2,
            "constraint_range": 2,
            "constraint_pattern": 1,
            "constraint_required_any_of": 1,
        },
        f"by_code={by_code}",
    )
    check(
        "messy counts",
        (summary["records"]["valid"], summary["records"]["invalid"]) == (1, 5),
        f"records={summary['records']}",
    )
    expected_issues = [
        (2, "constraint_enum", ["status"], "status is not one of the configured values"),
        (3, "constraint_range", ["quantity"],
         "quantity is below the configured minimum of 1"),
        (4, "constraint_pattern", ["sku"], "sku does not match the configured pattern"),
        (5, "constraint_required_any_of", [], "at least one of email, phone is required"),
        (6, "constraint_enum", ["status"], "status is not one of the configured values"),
        (6, "constraint_range", ["quantity"],
         "quantity is above the configured maximum of 500"),
    ]
    issue_rows = [
        (row["record_number"], row["code"], row["location"], row["message"])
        for row in summary["issue_rows"]
    ]
    check(
        "messy issue rows",
        issue_rows == expected_issues,
        f"rows={issue_rows}",
    )
    scenarios["messy"] = {"by_code": by_code, "rows": len(issue_rows)}

    # 2. All-valid rows migrate clean with the same rules.
    out_clean = work / "out-clean"
    code, text = _run(
        ["--input", str(clean), "--config", str(schema), "--constraints", str(rules),
         "--output-dir", str(out_clean)]
    )
    check("clean exit", code == 0, f"exit={code}\n{text[-400:]}")
    clean_codes = _summary(out_clean)["issues"]["by_code"]
    check(
        "clean no constraint codes",
        not [name for name in clean_codes if name.startswith("constraint_")],
        f"by_code={clean_codes}",
    )
    check(
        "clean counts",
        _summary(out_clean)["records"]["valid"] == 3,
        f"records={_summary(out_clean)['records']}",
    )
    scenarios["clean"] = {"valid": 3}

    # 3. Absent and null fields pass; required_any_of accepts the second
    #    field alone and rejects an empty string.
    out_edge = work / "out-edge"
    code, text = _run(
        ["--input", str(edge), "--config", str(schema), "--constraints", str(rules),
         "--output-dir", str(out_edge)]
    )
    check("edge exit", code == 1, f"exit={code}\n{text[-400:]}")
    edge_summary = _summary(out_edge)
    check(
        "edge counts",
        (edge_summary["records"]["valid"], edge_summary["records"]["invalid"]) == (2, 1),
        f"records={edge_summary['records']}",
    )
    edge_rows = [(row["code"], row["location"]) for row in edge_summary["issue_rows"]]
    check(
        "edge empty-string row fails required_any_of",
        edge_rows == [("constraint_required_any_of", [])],
        f"rows={edge_rows}",
    )
    scenarios["edge"] = {"valid": 2, "invalid": 1}

    # 4. Malformed rule files exit 2 with a message naming the problem.
    for label, (payload, fragment) in BAD_RULES.items():
        bad = _write_json(work / f"bad-{label}.json", payload)
        out = work / f"out-bad-{label}"
        code, text = _run(
            ["--input", str(clean), "--config", str(schema), "--constraints", str(bad),
             "--output-dir", str(out)]
        )
        check(f"bad {label} exit", code == 2, f"exit={code}\n{text[-300:]}")
        check(f"bad {label} message", fragment in text, text[-300:])
        check(
            f"bad {label} no run",
            not (out / "airlock_summary.json").exists(),
            "summary was written",
        )
    scenarios["bad_rules"] = len(BAD_RULES)

    # 5. The combination rule: the built-in examples file still cannot meet
    #    --config, while the field_rules-only file can.
    code, text = _run(
        ["--input", str(clean), "--config", str(schema),
         "--constraints", str(EXAMPLE_CONSTRAINTS), "--output-dir", str(work / "out-refused")]
    )
    check("builtin rules exit", code == 2, f"exit={code}\n{text[-300:]}")
    check("builtin rules message", "cannot be combined" in text, text[-300:])
    code, text = _run(
        ["repair", "--error-log", str(out_messy / "airlock_errors.jsonl"),
         "--config", str(schema), "--constraints", str(EXAMPLE_CONSTRAINTS),
         "--emit-decisions", str(work / "refused-decisions.jsonl")]
    )
    check("repair refused exit", code == 2, f"exit={code}\n{text[-300:]}")
    check("repair refused message", "cannot be combined" in text, text[-300:])
    code, text = _run(
        ["verify", "--input", str(clean), "--config", str(schema),
         "--constraints", str(EXAMPLE_CONSTRAINTS), "--output-dir", str(out_clean)]
    )
    check("verify refused exit", code == 2, f"exit={code}\n{text[-300:]}")
    check("verify refused message", "cannot be combined" in text, text[-300:])
    code, text = _run(
        ["--input", str(clean), "--config", str(schema), "--constraints", str(rules),
         "--output-dir", str(work / "out-allowed")]
    )
    check("field_rules file allowed", code == 0, f"exit={code}\n{text[-300:]}")
    scenarios["combination"] = {"refused": 3, "allowed": 1}

    # 6. The repair fingerprint accepts the original rules file and rejects
    #    a changed one.
    template = work / "decisions.jsonl"
    code, text = _run(
        ["repair", "--error-log", str(out_messy / "airlock_errors.jsonl"),
         "--config", str(schema), "--constraints", str(rules),
         "--emit-decisions", str(template)]
    )
    check("repair same rules exit", code == 0, f"exit={code}\n{text[-400:]}")
    check(
        "repair same rules passes the fingerprint",
        "do not match" not in text,
        text[-400:],
    )
    check("repair same rules template", template.is_file(), "template missing")
    code, text = _run(
        ["repair", "--error-log", str(out_messy / "airlock_errors.jsonl"),
         "--config", str(schema), "--constraints", str(rules_variant),
         "--emit-decisions", str(work / "variant-decisions.jsonl")]
    )
    check("repair variant rules exit", code == 2, f"exit={code}\n{text[-400:]}")
    check(
        "repair variant rules mismatch",
        "business constraints do not match" in text,
        text[-400:],
    )
    scenarios["fingerprint"] = {"same": 0, "variant": 2}

    # 7. verify reconciles the dynamic target only when the same field_rules
    #    file is supplied: without it, the rows the rules rejected look
    #    missing from the target.
    code, text = _run(
        ["verify", "--input", str(messy), "--config", str(schema),
         "--constraints", str(rules), "--output-dir", str(out_messy)]
    )
    check("verify exit", code == 0, f"exit={code}\n{text[-400:]}")
    check("verify message", "Verification: consistent" in text, text[-400:])
    code, text = _run(
        ["verify", "--input", str(messy), "--config", str(schema),
         "--output-dir", str(out_messy)]
    )
    check(
        "verify without rules is not consistent",
        code != 0 or "Verification: consistent" not in text,
        f"exit={code}\n{text[-400:]}",
    )
    scenarios["verify"] = {"with_rules": 0, "without_rules": code}

    evidence = work / "dynamic_constraints_verification.json"
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
            f"dynamic_constraints_failed failures={len(failures)} work_dir={work}",
            file=sys.stderr,
        )
        return 1
    print(f"dynamic_constraints_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
