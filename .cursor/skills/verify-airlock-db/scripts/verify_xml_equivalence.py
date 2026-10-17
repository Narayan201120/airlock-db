"""Check that XML ingestion matches JSON ingestion for the same records.

Runs the CLI twice in temporary directories. The JSON run establishes the
accepted, stored rows. Those stored values are re-serialized as XML records
and migrated again. Every stored column except ``migrated_at`` must match.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _run_cli(arguments: list[str], cwd: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(
            f"unexpected CLI exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _managed_table(database: Path) -> str:
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
    candidates = [
        name
        for name in names
        if name == "transactions" or name.startswith("dynamic_records_")
    ]
    if len(candidates) != 1:
        raise SystemExit(f"expected one managed table in {database}, found {len(candidates)}")
    return candidates[0]


def _table_rows(database: Path, table: str) -> tuple[list[str], list[tuple[object, ...]]]:
    quoted = '"' + table.replace('"', '""') + '"'
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
        columns = [
            row[1]
            for row in connection.execute(f"PRAGMA table_info({quoted})")
            if row[1] != "migrated_at"
        ]
        selection = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
        ordering = ", ".join(str(index) for index in range(1, len(columns) + 1))
        query = f"SELECT {selection} FROM {quoted} ORDER BY {ordering}"
        return columns, [tuple(row) for row in connection.execute(query)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that XML ingestion matches JSON ingestion for the same records."
    )
    parser.add_argument("input", type=Path, help="JSON array or JSONL file to migrate first")
    parser.add_argument("--constraints", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.constraints and args.config:
        parser.error("pass either --constraints or --config, not both")

    mode_flags: list[str] = []
    if args.constraints:
        mode_flags += ["--constraints", str(args.constraints.resolve())]
    if args.config:
        mode_flags += ["--config", str(args.config.resolve())]

    with tempfile.TemporaryDirectory(
        prefix="airlock-xml-", ignore_cleanup_errors=True
    ) as scratch:
        scratch_path = Path(scratch)
        json_output = scratch_path / "json-run"
        xml_output = scratch_path / "xml-run"

        _run_cli(
            ["--input", str(args.input.resolve()), "--output-dir", str(json_output), *mode_flags],
            cwd=REPO_ROOT,
        )
        json_database = json_output / "target_migration.db"
        table = _managed_table(json_database)
        columns, json_rows = _table_rows(json_database, table)
        if not json_rows:
            raise SystemExit("the JSON run committed no rows; equivalence cannot be checked")

        xml_path = scratch_path / "equivalent.xml"
        root = ET.Element("transactions")
        for row in json_rows:
            element = ET.SubElement(root, "transaction")
            for column, value in zip(columns, row):
                if value is None:
                    continue
                child = ET.SubElement(element, column)
                child.text = str(value)
        ET.indent(root)
        ET.ElementTree(root).write(xml_path, encoding="utf-8", xml_declaration=True)

        _run_cli(
            ["--input", str(xml_path), "--output-dir", str(xml_output), *mode_flags],
            cwd=REPO_ROOT,
        )
        xml_database = xml_output / "target_migration.db"
        xml_table = _managed_table(xml_database)
        xml_columns, xml_rows = _table_rows(xml_database, xml_table)

        if xml_table != table or xml_columns != columns or xml_rows != json_rows:
            print(f"json_rows={len(json_rows)} xml_rows={len(xml_rows)}")
            raise SystemExit("XML migration output does not match the JSON migration output")
        print(f"records={len(json_rows)}")
        print(f"table={table}")
        print("xml_equals_json=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
