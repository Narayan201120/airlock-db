"""End-to-end check for per-field transforms in dynamic schema configs.

Migrates a dirty fixture through a layout that trims, folds case, parses
European dates and decimals, and maps enum aliases, then requires the exact
stored values, the transform_failed rejections, a working verify, fail-fast
config errors, and repair sessions that normalize the same way.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

CONFIG_A = """\
version: 1
model_name: TicketRow
id_field: ticket_id
strip_whitespace: false
fields:
  ticket_id:
    type: string
    pattern: '^TKT-[0-9]{6}$'
    transforms:
      - trim: true
  customer_name:
    type: string
    transforms:
      - trim: true
      - lowercase: true
  priority:
    type: enum
    values: [LOW, NORMAL, HIGH]
    transforms:
      - uppercase: true
  estimated_cost:
    type: decimal
    max_digits: 12
    decimal_places: 2
    transforms:
      - decimal:
          dot: false
          places: 2
  opened_at:
    type: datetime
    transforms:
      - datetime: '%d/%m/%Y'
  closed_on:
    type: date
    required: false
    transforms:
      - datetime: '%d/%m/%Y'
  region:
    type: enum
    values: [EMEA, APAC]
    required: false
    transforms:
      - alias:
          n/a: APAC
          europe: EMEA
"""

CONFIG_B = {
    "version": 1,
    "model_name": "TicketRow",
    "id_field": "ticket_id",
    "strip_whitespace": False,
    "fields": {
        "ticket_id": {
            "type": "string",
            "pattern": "^TKT-[0-9]{6}$",
            "transforms": [{"trim": True}],
        },
        "customer_name": {
            "type": "string",
            "transforms": [{"trim": True}, {"lowercase": True}],
        },
        "priority": {
            "type": "enum",
            "values": ["LOW", "NORMAL", "HIGH"],
            "transforms": [{"uppercase": True}],
        },
        "estimated_cost": {
            "type": "decimal",
            "max_digits": 12,
            "decimal_places": 2,
            "transforms": [{"decimal": {"dot": False, "places": 2}}],
        },
        "opened_at": {
            "type": "datetime",
            "transforms": [{"datetime": "%d/%m/%Y"}],
        },
        "closed_on": {
            "type": "date",
            "required": False,
            "transforms": [{"datetime": "%d/%m/%Y"}],
        },
        "region": {
            "type": "enum",
            "values": ["EMEA", "APAC"],
            "required": False,
            "transforms": [{"alias": {"n/a": "APAC", "europe": "APAC"}}],
        },
    },
}

RECORDS = [
    {
        "ticket_id": " TKT-000001 ",
        "customer_name": " ALICE ",
        "priority": "high",
        "estimated_cost": "1.234,50",
        "opened_at": "31/12/2025",
        "closed_on": "15/03/2026",
        "region": "n/a",
    },
    {
        "ticket_id": "TKT-000002",
        "customer_name": "Bob",
        "priority": "normal",
        "estimated_cost": "1234,565",
        "opened_at": "05/01/2026",
        "closed_on": "16/04/2026",
        "region": "EMEA",
    },
    {
        "ticket_id": "TKT-000003",
        "customer_name": "  Carol  ",
        "priority": "HIGH",
        "estimated_cost": "99,90",
        "opened_at": "01/02/2026",
        "region": "n/a",
    },
    {
        "ticket_id": "TKT-000004",
        "customer_name": "Dan",
        "priority": "low",
        "estimated_cost": "10,00",
        "opened_at": "2025/12/31",
        "region": "EMEA",
    },
    {
        "ticket_id": "TKT-000005",
        "customer_name": "Eve",
        "priority": "normal",
        "estimated_cost": "1.23.4.5",
        "opened_at": "03/03/2026",
        "region": "EMEA",
    },
    {
        "ticket_id": "TKT-000006",
        "customer_name": "Fay",
        "priority": "urgent",
        "estimated_cost": "5,00",
        "opened_at": "04/04/2026",
        "region": "EMEA",
    },
]

EXPECTED_ROWS = [
    ("TKT-000001", "alice", "HIGH", "1234.50", "2025-12-31T00:00:00", "2026-03-15", "APAC"),
    ("TKT-000002", "bob", "NORMAL", "1234.57", "2026-01-05T00:00:00", "2026-04-16", "EMEA"),
    ("TKT-000003", "carol", "HIGH", "99.90", "2026-02-01T00:00:00", None, "APAC"),
]


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


def _summary_counts(output_dir: Path) -> tuple[int, int, int]:
    summary = _summary(output_dir)
    records = summary["records"]
    return records["checked"], records["valid"], records["invalid"]


def _summary(output_dir: Path) -> dict:
    return json.loads((output_dir / "airlock_summary.json").read_text(encoding="utf-8"))


def _stored_rows(output_dir: Path) -> list[tuple]:
    connection = sqlite3.connect(output_dir / "target_migration.db")
    try:
        (table,) = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'dynamic_records_%'"
        ).fetchone()
        return connection.execute(
            f"SELECT ticket_id, customer_name, priority, estimated_cost, "
            f"opened_at, closed_on, region FROM {table} ORDER BY ticket_id"
        ).fetchall()
    finally:
        connection.close()


def _write_json_config(path: Path, spec: dict) -> Path:
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify per-field transforms in dynamic schema configs."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="airlock-transforms-"))
    _reset_dir(work)

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    fixture = work / "tickets.jsonl"
    fixture.write_text(
        "".join(json.dumps(record) + "\n" for record in RECORDS),
        encoding="utf-8",
    )
    config_a = work / "layout.yaml"
    config_a.write_text(CONFIG_A, encoding="utf-8")
    config_b = _write_json_config(work / "layout-b.json", CONFIG_B)

    # 1. Main run: transforms normalize stored values; bad values fail with
    #    transform_failed, and an unmapped enum value stays an enum issue.
    out_a = work / "out-a"
    code, text = _run(
        [
            "--input", str(fixture),
            "--config", str(config_a),
            "--error-log",
            "--output-dir", str(out_a),
        ]
    )
    check("main exit", code == 1, f"exit={code}\n{text[-500:]}")
    counts = _summary_counts(out_a)
    check("main counts", counts == (6, 3, 3), f"counts={counts}")
    summary_a = _summary(out_a)
    check(
        "main normalized_fields",
        summary_a["records"]["normalized_fields"] == 16,
        f"normalized={summary_a['records']['normalized_fields']}",
    )
    check(
        "main transform_failed count",
        summary_a["issues"]["by_code"].get("transform_failed") == 2,
        f"by_code={summary_a['issues']['by_code']}",
    )
    check(
        "main enum count",
        summary_a["issues"]["by_code"].get("enum") == 1,
        f"by_code={summary_a['issues']['by_code']}",
    )
    scenarios["main"] = {"counts": list(counts), "normalized": 16}

    # 2. Stored values are the exact normalized results.
    rows = _stored_rows(out_a)
    check(
        "stored rows",
        [tuple(row) for row in rows] == EXPECTED_ROWS,
        f"rows={rows}",
    )
    scenarios["stored_rows"] = len(rows)

    # 3. The repair log names the field, the rule, and the original input.
    log_lines = [
        json.loads(line)
        for line in (out_a / "airlock_errors.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    header = next(line for line in log_lines if line.get("kind") == "header")
    entries = [line for line in log_lines if line.get("kind") == "record"]
    check("error log header", header.get("kind") == "header", str(header))
    check("error log entries", len(entries) == 3, f"entries={len(entries)}")
    issues = [
        (issue["code"], tuple(issue["location"]), issue["message"], issue["input"])
        for entry in entries
        for issue in entry.get("issues", [])
    ]
    check(
        "error log codes",
        [issue[0] for issue in issues] == ["transform_failed", "transform_failed", "enum"],
        f"codes={[issue[0] for issue in issues]}",
    )
    check(
        "error log locations",
        [issue[1] for issue in issues]
        == [("opened_at",), ("estimated_cost",), ("priority",)],
        f"locations={[issue[1] for issue in issues]}",
    )
    check(
        "error log datetime rule",
        "opened_at" in issues[0][2]
        and "datetime(%d/%m/%Y)" in issues[0][2]
        and issues[0][3] == "2025/12/31",
        f"issue={issues[0]}",
    )
    check(
        "error log decimal rule",
        "estimated_cost" in issues[1][2]
        and "decimal" in issues[1][2]
        and issues[1][3] == "1.23.4.5",
        f"issue={issues[1]}",
    )
    scenarios["error_log"] = {"transform_failed": 2, "enum": 1}

    # 4. An untouched target verifies; a tampered stored value does not.
    code, text = _run(
        [
            "verify",
            "--input", str(fixture),
            "--config", str(config_a),
            "--output-dir", str(out_a),
        ]
    )
    check("verify exit", code == 0, f"exit={code}\n{text[-500:]}")
    connection = sqlite3.connect(out_a / "target_migration.db")
    (table,) = connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'dynamic_records_%'"
    ).fetchone()
    connection.execute(f"UPDATE {table} SET priority = 'LOW' WHERE ticket_id = 'TKT-000001'")
    connection.commit()
    connection.close()
    code, text = _run(
        [
            "verify",
            "--input", str(fixture),
            "--config", str(config_a),
            "--output-dir", str(out_a),
        ]
    )
    check("tampered verify exit", code == 1, f"exit={code}\n{text[-500:]}")
    check("tampered verify id", "TKT-000001" in text, text[-500:])
    scenarios["verify"] = {"untouched": 0, "tampered": 1}

    # 5. Transforms feed the schema fingerprint: an alias change moves both
    #    the fingerprint and the target table.
    out_b = work / "out-b"
    code, text = _run(
        [
            "--input", str(fixture),
            "--config", str(config_b),
            "--error-log",
            "--output-dir", str(out_b),
        ]
    )
    check("variant exit", code == 1, f"exit={code}\n{text[-500:]}")
    summary_b = _summary(out_b)
    check(
        "fingerprint differs",
        summary_a["schema"]["fingerprint"] != summary_b["schema"]["fingerprint"],
        f"a={summary_a['schema']['fingerprint']} b={summary_b['schema']['fingerprint']}",
    )
    check(
        "variant counts",
        _summary_counts(out_b) == (6, 3, 3),
        f"counts={_summary_counts(out_b)}",
    )
    scenarios["fingerprint"] = {
        "a": summary_a["schema"]["fingerprint"],
        "b": summary_b["schema"]["fingerprint"],
    }

    # 6. Invalid transforms blocks stop the run before input is processed.
    base_fields = {
        "ticket_id": {"type": "string"},
        "priority": {"type": "enum", "values": ["LOW", "HIGH"]},
        "amount": {"type": "decimal"},
    }
    variants = {
        "not_a_list": {
            "ticket_id": {"type": "string", "transforms": "trim"},
        },
        "wrong_field_type": {
            "ticket_id": {
                "type": "string",
                "transforms": [{"datetime": "%d/%m/%Y"}],
            },
        },
        "unknown_target": {
            "ticket_id": {"type": "string"},
            "priority": {
                "type": "enum",
                "values": ["LOW", "HIGH"],
                "transforms": [{"alias": {"urgent": "HIGH", "odd": "NOPE"}}],
            },
        },
    }
    fragments = {
        "not_a_list": "must be a non-empty list",
        "wrong_field_type": "only valid for date or datetime fields",
        "unknown_target": "is not a declared value",
    }
    for label, override in variants.items():
        fields = dict(base_fields)
        fields.update(override)
        config = _write_json_config(
            work / f"bad-{label}.json",
            {"version": 1, "model_name": "BadRow", "id_field": "ticket_id", "fields": fields},
        )
        out = work / f"out-bad-{label}"
        code, text = _run(
            ["--input", str(fixture), "--config", str(config), "--output-dir", str(out)]
        )
        check(f"bad {label} exit", code == 2, f"exit={code}\n{text[-300:]}")
        check(f"bad {label} message", fragments[label] in text, text[-300:])
        check(f"bad {label} no run", not (out / "airlock_summary.json").exists(), "summary was written")
    scenarios["config_errors"] = len(variants)

    # 7. Repair applies the same transforms: corrected values pass only
    #    because the rules run again on the repair side, and they are stored
    #    in normalized form.
    decisions = work / "decisions.jsonl"
    decisions.write_text(
        '{"entry": 1, "action": "set", "field": "opened_at", "value": "31/12/2025"}\n'
        '{"entry": 2, "action": "set", "field": "estimated_cost", "value": "1.234,50"}\n'
        '{"entry": 3, "action": "drop"}\n',
        encoding="utf-8",
    )
    code, text = _run(
        [
            "repair",
            "--error-log", str(out_b / "airlock_errors.jsonl"),
            "--config", str(config_b),
            "--decisions", str(decisions),
        ]
    )
    check("repair exit", code == 0, f"exit={code}\n{text[-500:]}")
    check(
        "repair outcome line",
        "2 corrected, 1 dropped" in text,
        text[-500:],
    )
    connection = sqlite3.connect(out_b / "target_migration.db")
    (table,) = connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'dynamic_records_%'"
    ).fetchone()
    repaired = connection.execute(
        f"SELECT ticket_id, opened_at, estimated_cost FROM {table} "
        f"WHERE ticket_id IN ('TKT-000004', 'TKT-000005') ORDER BY ticket_id"
    ).fetchall()
    row_count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    connection.close()
    check(
        "repair normalized values",
        repaired
        == [
            ("TKT-000004", "2025-12-31T00:00:00", "10.00"),
            ("TKT-000005", "2026-03-03T00:00:00", "1234.50"),
        ],
        f"repaired={repaired}",
    )
    check("repair row count", row_count == 5, f"rows={row_count}")
    scenarios["repair"] = {"corrected": 2, "dropped": 1}

    evidence = work / "transforms_verification.json"
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
        print(f"transforms_failed failures={len(failures)} work_dir={work}", file=sys.stderr)
        return 1
    print(f"transforms_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
