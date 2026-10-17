"""Soak a large synthetic migration and assert the streaming story at scale.

Generates a JSONL source with unique IDs, runs the real CLI, then checks the
stored row count, the run summary counts, and the peak memory of the migration
process against an optional ceiling. Wall time and peak RSS are printed and
written to an evidence file.

Peak RSS is exact when one run is measured on Linux or macOS, where the
platform reports the dead child's high-water mark. Other combinations sample
the live process while it runs, so the number is a close approximation, and a
platform that cannot report memory leaves it unmeasured instead of failing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

_MIB = 1024 * 1024
_POLL_SECONDS = 0.05


def _bounded_integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}"
            )
        return number

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a large synthetic migration and assert its row counts, "
            "summary counts, wall time, and peak memory."
        )
    )
    parser.add_argument(
        "--rows",
        type=_bounded_integer(1, 100_000_000),
        default=1_000_000,
        help="number of generated records (default: 1000000)",
    )
    parser.add_argument(
        "--runs",
        type=_bounded_integer(1, 10),
        default=1,
        help="number of independent CLI runs (default: 1)",
    )
    parser.add_argument(
        "--batch-size",
        type=_bounded_integer(1, 10_000),
        default=500,
        help="SQLite write batch size (default: 500)",
    )
    parser.add_argument(
        "--max-peak-rss-mib",
        type=_bounded_integer(1, 1_000_000),
        default=None,
        help="fail when a measured run peaks above this many MiB",
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="directory for the generated source and run outputs; defaults to a fresh temp directory",
    )
    return parser


def _write_jsonl(path: Path, row_count: int) -> None:
    types = ("DEBIT", "CREDIT")
    types_length = len(types)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(row_count):
            row = {
                "transaction_id": f"00000000-0000-4000-8000-{index:012d}",
                "account_id": f"ACC-{index % 1000:04d}",
                "posted_at": "2026-01-01T00:00:00Z",
                "amount": round(10 + (index % 100_000) / 100, 2),
                "currency": "USD",
                "transaction_type": types[index % types_length],
                "status": "POSTED",
                "counterparty": f"Soak Counterparty {index % 100}",
                "description": f"Soak row {index}",
            }
            stream.write(json.dumps(row, separators=(",", ":")))
            stream.write("\n")


def _windows_peak_working_set(process_id: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information, False, process_id
    )
    if not handle:
        return None
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PeakWorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def _linux_peak_hwm(process_id: int) -> int | None:
    try:
        with open(f"/proc/{process_id}/status", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _sample_peak(process_id: int) -> int | None:
    if sys.platform == "win32":
        return _windows_peak_working_set(process_id)
    return _linux_peak_hwm(process_id)


def _child_peak_bytes() -> int | None:
    """Peak RSS of the single reaped child, when the platform reports one."""

    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    if not usage.ru_maxrss:
        return None
    if sys.platform == "darwin":
        return int(usage.ru_maxrss)
    return int(usage.ru_maxrss) * 1024


def _run_measured(
    command: list[str], cwd: Path, log_path: Path, *, exact: bool
) -> tuple[int, float, int | None, str]:
    started = time.perf_counter()
    if exact:
        # One reaped child per script run (runs == 1), so the platform's
        # high-water mark belongs to the migration.
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=False
            )
        elapsed = time.perf_counter() - started
        peak = _child_peak_bytes()
        return result.returncode, elapsed, peak, ("getrusage" if peak else "unavailable")
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT
        )
        peak = 0
        while process.poll() is None:
            sample = _sample_peak(process.pid)
            if sample:
                peak = max(peak, sample)
            time.sleep(_POLL_SECONDS)
        process.wait()
    elapsed = time.perf_counter() - started
    if peak:
        return process.returncode, elapsed, peak, "poll"
    return process.returncode, elapsed, None, "unavailable"


def _stored_row_count(database: Path) -> int:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        targets = [name for name in tables if name == "transactions"]
        if len(targets) != 1:
            raise RuntimeError(f"expected one transactions table, found {len(targets)}")
        return int(connection.execute('SELECT COUNT(*) FROM "transactions"').fetchone()[0])
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    project_root = Path(__file__).resolve().parents[1]
    entry_point = project_root / "main.py"
    if not entry_point.is_file():
        print(f"soak: CLI entry point not found: {entry_point}", file=sys.stderr)
        return 2

    if args.work_dir is not None:
        work = args.work_dir.resolve()
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="airlock-soak-"))

    failures: list[str] = []
    runs_evidence: list[dict[str, object]] = []
    exact_measurement = sys.platform != "win32" and args.runs == 1

    source = work / "soak.jsonl"
    _write_jsonl(source, args.rows)
    print(f"Generated {args.rows:,} JSONL records ({source.stat().st_size:,} bytes).")

    exit_code = 0
    for run_number in range(1, args.runs + 1):
        output = work / f"output-{run_number}"
        log_path = work / f"run-{run_number}.log"
        command = [
            sys.executable,
            str(entry_point),
            "--input",
            str(source),
            "--output-dir",
            str(output),
            "--batch-size",
            str(args.batch_size),
        ]
        code, elapsed, peak, method = _run_measured(
            command, project_root, log_path, exact=exact_measurement
        )
        rate = args.rows / elapsed if elapsed else 0.0
        peak_mib = round(peak / _MIB, 1) if peak else None
        run_details: dict[str, object] = {
            "run": run_number,
            "rows": args.rows,
            "exit_code": code,
            "seconds": round(elapsed, 3),
            "records_per_second": round(rate),
            "peak_rss_mib": peak_mib,
            "peak_rss_method": method,
        }
        runs_evidence.append(run_details)
        peak_text = f"{peak_mib} MiB ({method})" if peak_mib else f"unavailable ({method})"
        print(
            f"run {run_number}/{args.runs}: {elapsed:.3f}s, {rate:,.0f} records/s, "
            f"peak RSS {peak_text}"
        )

        if code != 0:
            details = log_path.read_text(encoding="utf-8", errors="replace")
            failures.append(f"run {run_number}: CLI returned {code}\n{details[-800:]}")
            exit_code = 1
            continue

        try:
            migrated = _stored_row_count(output / "target_migration.db")
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            failures.append(f"run {run_number}: could not count stored rows: {exc}")
            exit_code = 1
            continue
        run_details["stored_rows"] = migrated
        if migrated != args.rows:
            failures.append(
                f"run {run_number}: stored {migrated:,} rows; expected {args.rows:,}"
            )
            exit_code = 1

        summary = json.loads((output / "airlock_summary.json").read_text(encoding="utf-8"))
        records = summary.get("records", {})
        storage = summary.get("storage", {})
        expected_counts = {
            "checked": args.rows,
            "valid": args.rows,
            "invalid": 0,
            "file_errors": 0,
        }
        for key, expected in expected_counts.items():
            if records.get(key) != expected:
                failures.append(
                    f"run {run_number}: summary records.{key} is "
                    f"{records.get(key)!r}; expected {expected}"
                )
                exit_code = 1
        if storage.get("status") != "committed":
            failures.append(
                f"run {run_number}: storage status is {storage.get('status')!r}"
            )
            exit_code = 1
        if storage.get("inserted") != args.rows:
            failures.append(
                f"run {run_number}: storage inserted {storage.get('inserted')!r} "
                f"rows; expected {args.rows}"
            )
            exit_code = 1

        if args.max_peak_rss_mib is not None:
            if peak is None:
                print(
                    "note: peak RSS is unavailable on this platform; "
                    "the memory ceiling was not checked",
                    file=sys.stderr,
                )
            elif peak_mib is not None and peak_mib > args.max_peak_rss_mib:
                failures.append(
                    f"run {run_number}: peak RSS {peak_mib} MiB exceeds the "
                    f"{args.max_peak_rss_mib} MiB ceiling"
                )
                exit_code = 1

    evidence_path = work / "soak_verification.json"
    evidence = {
        "status": "passed" if not failures else "failed",
        "rows": args.rows,
        "runs": runs_evidence,
        "max_peak_rss_mib": args.max_peak_rss_mib,
        "failures": failures,
    }
    evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    if failures:
        for message in failures:
            print(f"FAIL {message}", file=sys.stderr)
        print(f"soak_failed failures={len(failures)} evidence={evidence_path}", file=sys.stderr)
        return 1
    total_seconds = sum(float(run["seconds"]) for run in runs_evidence)
    peaks = [run["peak_rss_mib"] for run in runs_evidence if run["peak_rss_mib"]]
    peak_summary = f"{max(peaks)}" if peaks else "unavailable"
    print(
        f"soak_ok rows={args.rows} runs={args.runs} seconds={total_seconds:.3f} "
        f"peak_rss_mib={peak_summary} evidence={evidence_path}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
