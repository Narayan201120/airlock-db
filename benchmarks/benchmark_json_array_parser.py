"""A/B benchmark for JSON array parsing through the migration CLI.

The legacy boundary scanner is kept here so the current parser can be compared
without modifying production code between runs. Inputs and SQLite outputs are
temporary; valid rows and report counts are checked on every pair.

Example:
    python benchmarks/benchmark_json_array_parser.py --rows 100000 --repeats 6
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airlock_db import cli, ingestion  # noqa: E402


_ROW_COLUMNS = (
    "transaction_id, account_id, posted_at, amount, currency, "
    "transaction_type, status, counterparty, description"
)


def _legacy_parse_value(self: ingestion._JSONStream) -> Any:
    """Original Python boundary scanner retained as the A/B baseline."""

    self._skip_whitespace()
    value_start = self._position
    first = self._buffer[value_start] if value_start < len(self._buffer) else ""
    segment = self._buffer
    segment_start = value_start
    cursor = value_start
    record_parts: list[str] = []
    record_length = 0
    if first and first in "[{":
        mode = "container"
        depth = 0
        in_string = False
        escaped = False
    elif first == '"':
        mode = "string"
        in_string = True
        escaped = False
        depth = 0
        cursor += 1
        record_length = 1
    else:
        mode = "primitive"
        in_string = False
        escaped = False
        depth = 0

    def read_more() -> bool:
        nonlocal segment, segment_start, cursor
        if cursor > segment_start:
            record_parts.append(segment[segment_start:cursor])
        segment = ""
        segment_start = 0
        cursor = 0
        chunk = self._stream.read(self._chunk_size)
        if not chunk:
            self._eof = True
            return False
        segment = chunk
        return True

    def finish_value(end: int) -> Any:
        value_text = "".join([*record_parts, segment[segment_start:end]])
        try:
            value, decoded_end = self._decoder.raw_decode(value_text)
        except json.JSONDecodeError as exc:
            if self._eof and (
                exc.pos >= len(value_text)
                or "unterminated" in exc.msg.casefold()
            ):
                raise ingestion.JSONTruncatedError(str(exc)) from exc
            raise
        self._buffer = value_text[decoded_end:] + segment[end:]
        self._position = 0
        return value

    while True:
        if cursor >= len(segment):
            if read_more():
                continue
            if self._eof:
                return finish_value(cursor)
            continue

        char = segment[cursor]
        if mode == "primitive":
            if char.isspace() or char in ",]":
                return finish_value(cursor)
            cursor += 1
            record_length += 1
        elif mode == "string":
            cursor += 1
            record_length += 1
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return finish_value(cursor)
        else:
            cursor += 1
            record_length += 1
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth == 0:
                    return finish_value(cursor)

        if record_length > ingestion._MAX_JSON_RECORD_CHARS:
            raise ingestion.JSONRecordTooLargeError(
                f"JSON record exceeds {ingestion._MAX_JSON_RECORD_CHARS} characters"
            )


def _write_input(path: Path, rows: int) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("[")
        for index in range(rows):
            if index:
                stream.write(",")
            row = {
                "transaction_id": (
                    f"00000000-0000-0000-0000-{index + 1:012d}"
                ),
                "account_id": f"ACC-{index % 10000:07d}",
                "posted_at": "2026-01-15T12:30:00Z",
                "amount": "12.34",
                "currency": "USD",
                "transaction_type": "DEBIT",
                "status": "POSTED",
                "counterparty": f"Counterparty {index % 1000:07d}",
                "description": f"Description {index}",
            }
            stream.write(json.dumps(row, separators=(",", ":")))
        stream.write("]")


def _run_cli(
    input_path: Path,
    output_path: Path,
    *,
    optimized: bool,
) -> tuple[int, float, tuple[str, ...]]:
    original = ingestion._JSONStream.parse_value
    if not optimized:
        ingestion._JSONStream.parse_value = _legacy_parse_value
    try:
        args = cli._build_parser().parse_args(
            [
                "--input",
                str(input_path),
                "--output-dir",
                str(output_path),
                "--batch-size",
                "1000",
                "--max-report-issues",
                "0",
            ]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            started = time.perf_counter()
            result = cli.run_migration(args)
            elapsed = time.perf_counter() - started
        stable_lines = tuple(output.getvalue().splitlines()[:2])
        return result, elapsed, stable_lines
    finally:
        ingestion._JSONStream.parse_value = original


def _assert_same_database(left_path: Path, right_path: Path, expected_rows: int) -> None:
    query = f"SELECT {_ROW_COLUMNS} FROM transactions ORDER BY transaction_id"
    left = sqlite3.connect(left_path)
    right = sqlite3.connect(right_path)
    try:
        count = 0
        for left_row, right_row in zip(
            left.execute(query), right.execute(query), strict=True
        ):
            if left_row != right_row:
                raise RuntimeError(f"database output differs at row {count + 1}")
            count += 1
        if count != expected_rows:
            raise RuntimeError(f"expected {expected_rows} migrated rows, found {count}")
    finally:
        left.close()
        right.close()


def _outcome(source: str, *, optimized: bool) -> tuple[Any, ...]:
    original = ingestion._JSONStream.parse_value
    if not optimized:
        ingestion._JSONStream.parse_value = _legacy_parse_value
    try:
        try:
            values = list(ingestion._iter_json_values(io.StringIO(source)))
            representation = repr(values)
            if len(representation) > 1024:
                return (
                    "ok",
                    len(values),
                    hashlib.sha256(representation.encode("utf-8")).hexdigest(),
                )
            return ("ok", values)
        except Exception as exc:
            return (
                "error",
                type(exc).__name__,
                getattr(exc, "msg", str(exc)),
                getattr(exc, "pos", None),
                getattr(exc, "incomplete_array", None),
            )
    finally:
        ingestion._JSONStream.parse_value = original


def _chunked_outcome(source: str, *, optimized: bool) -> tuple[Any, ...]:
    original = ingestion._JSONStream.parse_value
    if not optimized:
        ingestion._JSONStream.parse_value = _legacy_parse_value
    try:
        reader = ingestion._JSONStream(io.StringIO(source), chunk_size=2)
        try:
            value = reader.parse_value()
            reader.finish()
            return ("ok", value)
        except Exception as exc:
            return (
                "error",
                type(exc).__name__,
                getattr(exc, "msg", str(exc)),
                getattr(exc, "pos", None),
            )
    finally:
        ingestion._JSONStream.parse_value = original


def _assert_parser_parity() -> None:
    maximum = ingestion._MAX_JSON_RECORD_CHARS
    samples = {
        "empty array": "[]",
        "heterogeneous": '[1,1.25,true,false,null,"two",{"x":[1,2]}]',
        "invalid number": "[1e]",
        "truncated exponent": "[1e",
        "partial literal": "[{\"flag\":tru}]",
        "trailing scalar text": "[truex]",
        "duplicate key": '[{"x":1,"x":2}]',
        "nonstandard constant": "[NaN]",
        "truncated string": '["unfinished',
        "truncated object": '[{"x":1',
        "bad object": '[{"x":}]',
        "trailing data": "[] trailing",
        "maximum record": "[\"" + ("x" * (maximum - 2)) + "\"]",
        "oversized record": "[\"" + ("x" * (maximum - 1)) + "\"]",
    }
    for label, source in samples.items():
        legacy = _outcome(source, optimized=False)
        current = _outcome(source, optimized=True)
        if label == "oversized record":
            if current[:2] != (
                "error",
                "JSONRecordTooLargeError",
            ):
                raise RuntimeError(
                    f"oversized record was not rejected by current parser: {current!r}"
                )
            continue
        if legacy != current:
            raise RuntimeError(
                f"parser outcome differs for {label}: legacy={legacy!r}, current={current!r}"
            )

    # Force scalars and nested values across tiny reads. raw_decode may accept
    # a numeric prefix at a chunk edge, so parse_value must wait for a boundary.
    split_samples = (
        ("123456789", 123456789),
        ("-1.2345e+60", Decimal("-1.2345e+60")),
        ("true", True),
        ("null", None),
        ('{"flag":true,"none":null}', {"flag": True, "none": None}),
        ('{"n":1.2345e+60,"text":"a\\\\b"}', {"n": Decimal("1.2345e+60"), "text": "a\\b"}),
    )
    for source, expected in split_samples:
        legacy = _chunked_outcome(source, optimized=False)
        current = _chunked_outcome(source, optimized=True)
        if legacy != current or current != ("ok", expected):
            raise RuntimeError(
                f"chunk-split value differs for {source!r}: "
                f"legacy={legacy!r}, current={current!r}"
            )

    for source in ("NaN", "Infinity", "-Infinity"):
        legacy = _chunked_outcome(source, optimized=False)
        current = _chunked_outcome(source, optimized=True)
        if legacy != current or current[1] != "InvalidJSONConstantError":
            raise RuntimeError(
                f"chunk-split constant differs for {source!r}: "
                f"legacy={legacy!r}, current={current!r}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=4)
    args = parser.parse_args()
    if args.rows < 1 or args.repeats < 1:
        parser.error("rows and repeats must be positive")

    _assert_parser_parity()
    legacy_times: list[float] = []
    optimized_times: list[float] = []
    with tempfile.TemporaryDirectory(prefix="airlock-json-array-benchmark-") as temp:
        root = Path(temp)
        input_path = root / "source.json"
        _write_input(input_path, args.rows)
        for trial in range(args.repeats):
            order = (False, True) if trial % 2 == 0 else (True, False)
            outputs: dict[bool, Path] = {}
            reports: dict[bool, tuple[str, ...]] = {}
            with tempfile.TemporaryDirectory(
                prefix="json-array-pair-", dir=root
            ) as pair_temp:
                pair_root = Path(pair_temp)
                for optimized in order:
                    output_path = pair_root / ("raw_decode" if optimized else "legacy")
                    result, elapsed, stable_lines = _run_cli(
                        input_path, output_path, optimized=optimized
                    )
                    if result != 0:
                        raise RuntimeError(f"migration exited with status {result}")
                    outputs[optimized] = output_path / "target_migration.db"
                    reports[optimized] = stable_lines
                    if optimized:
                        optimized_times.append(elapsed)
                    else:
                        legacy_times.append(elapsed)
                if reports[False] != reports[True]:
                    raise RuntimeError(
                        f"migration counts differ between parser variants: {reports!r}"
                    )
                _assert_same_database(
                    outputs[False], outputs[True], args.rows
                )

    legacy_median = statistics.median(legacy_times)
    optimized_median = statistics.median(optimized_times)
    print(
        json.dumps(
            {
                "rows_checked_per_run": args.rows,
                "repeats": args.repeats,
                "parser_parity": "matches error cases and chunk-split scalar samples",
                "record_limit": "512 KiB accepted; 512 KiB + 1 rejected",
                "sqlite_row_parity": "exact on every trial",
                "migration_count_parity": "exact on every trial",
                "legacy_median_seconds": legacy_median,
                "raw_decode_median_seconds": optimized_median,
                "elapsed_change_percent": (optimized_median / legacy_median - 1) * 100,
                "elapsed_saved_seconds": legacy_median - optimized_median,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
