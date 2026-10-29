# PERF_REPORT

## Environment

- Machine: Windows 11, CPython 3.12.10, pydantic 2.12.5, PyYAML 6.0.2
- Repo: AirLock DB, branch `perf/optimize`, base `b630a06`
- Date: 2026-10-29
- Method: `scripts/benchmark_migration.py`, `benchmarks/benchmark_*`, cProfile
  in-process runs (steady state, second run after warmup), subprocess runs
  (cold, real CLI). Workloads are deterministic with fixed seeds and IDs.
- Note on noise: this box varies about 30 percent run to run on identical
  code (observed spreads of 0.26 to 0.44 s on a fixed 10k workload). Every
  claim below rests on structure (removed call counts in profiles), on
  byte-identical output checks, or on repeated A/B pairs that agree in
  direction. Single timing numbers are reported with their spreads, not as
  exact gains.

## Project classification

CLI tool and data pipeline. No frontend, no network, no server. Local-first
migration: read JSON/JSONL/XML/CSV/SQLite, validate with Pydantic, write
SQLite and JSONL, write an HTML report and a JSON summary.

Critical flows: migration (built-in and dynamic schema, with and without
anonymize), dry run, verify, repair (interactive, decisions, summary),
report rebuild, infer, check.

Primary metrics: migration wall time and throughput in records per second,
peak RSS during soak, cold start time, artifact size (wheel bytes and source
bytes) and dependency count, SQLite write cost, report build cost. No
LCP/INP/CLS. No network.

## Before table (baseline, commit b630a06)

| Workload | Median wall | Throughput | Notes |
| --- | --- | --- | --- |
| Dynamic valid 20k | 1.019 s subprocess (runs 1.710, 0.906, 1.019) | 19,627 records/s | `scripts/benchmark_migration.py --rows 20000 --runs 3`. Run 1 is cold. |
| Dynamic 20k, 10 percent invalid | 2.281 s median of 5 (1.508, 2.281, 2.625, 2.452, 1.194) | 8,770 records/s | Invalid path costs 2.2x the valid path. Report plus repair log writes dominate. |
| Field-rules 10k valid, in-process steady | 0.334 s median (0.334, 0.317, 0.380, 0.356, 0.298) | 29,940 records/s | 10-field enum rule. |
| Invalid 10k (1k invalid), in-process steady | 0.967 s | 10,340 records/s | cProfile: `_encode_line` 0.096 s tottime, 0.321 s cumtime, 1M of 2.41M calls. |
| Built-in anonymized 20k | about 1.3 s per 10k in-process | about 6,000 records/s | HMAC per field per row is the extra cost over validation. |
| Verify 10k, in-process steady | 0.58 to 0.92 s across runs | n/a | 30,008 SQLite round trips per 10k rows. |
| Startup `--help` | 473 ms | n/a | Pydantic deferred already. |
| Startup `--version` | 171 ms | n/a | Cheapest path. |
| Repair summary 24-entry log | about 550 ms | n/a | Dominated by interpreter plus import. |
| Source size | 26 files, 437,324 bytes | n/a | Largest: ingestion 74,752, cli 53,395, repair 48,520. |
| Wheel | airlock_db-0.4.0-py3-none-any.whl, 112,271 bytes | n/a | |
| Dependencies | 2 | n/a | pydantic (hot path), PyYAML (lazy only). Both used. None unused. |

Profile hotspots, steady state:

- Dynamic valid 5k in 0.290 s: `validate_python` 0.031 s, sqlite
  `executemany` 0.021 s, `database_value` 0.021 s over 30k calls.
- Anonymized 5k in 0.827 s: `validate_python` 0.068 s, `executemany`
  0.047 s, `anonymize.transaction` 0.037 s, `_clean_text` 0.031 s over
  45k calls, HMAC `_digest` plus `hmac_new` plus `hexdigest` about 0.08 s.
- Invalid 10k in 0.967 s: `_encode_line` 0.096 s tottime, 0.321 s with
  callees, 138k `BytesIO.write` calls, 139k `str.encode` calls.
- Verify 10k in 1.325 s: 30,008 `sqlite3 execute` calls at 0.094 s,
  `_row_digest` 20k calls at 0.029 s, import scan 0.094 s cold.

## Changes and measured impact

### 1. Streamline repair log line encoding (`96e1b03`)

`repair_log._encode_line` built a fresh UUID4, a fresh `JSONEncoder`, and
pumped every line through a per-chunk Python loop with `BytesIO` writes and
two `ascii` passes. The rewrite uses one `json.dumps` call, generates the
Decimal marker prefix from a process counter only when a Decimal is present,
and skips the marker replacement pass when there are none. Output bytes are
unchanged: a 804,102-byte repair log from a 10k workload with 1k invalid rows
is byte-identical before and after (sha256 `94aea583...`).

Impact: `_encode_line` drops out of the profile top 12. Function calls per
10k invalid rows fall from 2.41M to 1.34M. Subprocess A/B on the invalid
workload favors the new code (about 1,085 ms vs 1,125 to 1,485 ms), though
the box noise overlaps. The structural win (1M fewer calls, no per-row
CSPRNG) stands regardless of timing noise.

Proven by: `verify_repair_summary.py` (8 scenarios), `verify_decisions_dry_run.py`
(5 scenarios), `verify_dry_run.py` (8 scenarios), plus byte-identical log,
report counts, and summary counts on the parity workload.

### 2. Batch verify ledger inserts (`d1677b2`)

`verify_target` ran one `ledger.execute` per expected row (10k) and one per
stored row line (10k), plus per-row SELECT plus DELETE on the stored pass.
Both insert loops now accumulate 5,000-row chunks and use one `executemany`
per chunk. `INSERT OR IGNORE` and `INSERT OR REPLACE` keep first-wins and
last-wins collapse accounting identical. The SELECT plus DELETE comparison
loop is untouched.

Impact: interleaved A/B on a 10k verify, both orders, new code wins every
pair (0.717 vs 0.867 ms warm, 0.611 vs 0.920 fresh, about 17 to 34 percent).
`airlock_verify.json` is identical modulo timestamp and output path.
Duplicate collapse verified for both conflict policies (collapsed 4,
matched 6 on a 10-row fixture with a 5x duplicate).

Proven by: `verify_handoff.py` (10 scenarios), `verify_jsonl_target.py`
(10 scenarios), both covering the batched JSONL and SQLite paths.

### 3. Drop unused io import in repair log (`df52fed`)

Leftover from change 1. No behavior change. `compileall` green.

## After table (branch perf/optimize)

| Workload | Result | Change vs before |
| --- | --- | --- |
| Dynamic valid 20k | unchanged (migration path untouched) | none, confirmed no-regression intent |
| Invalid 10k repair log | byte-identical log, 1M fewer calls | structural win, about 8 percent wall in A/B |
| Verify 10k | 0.61 to 0.72 s vs 0.87 to 0.92 s paired | 17 to 34 percent in paired runs |
| Budget gate `scripts/perf_budget.py` | `perf_budget_ok workloads=3` | new gate, see below |
| Full E2E | CI `verify.yml` on push | pending at push time |

## Removed dependencies and size breakdown

None removed. Both runtime dependencies are used: pydantic validates every
record on the hot path, PyYAML loads YAML configs and layouts lazily (plain
JSON runs never import it). Dropping either breaks supported inputs, so the
dependency count floor is 2.

- Source: 26 files, 437,324 bytes (unchanged within a few lines).
- Wheel: 112,271 bytes.
- No new heavy dependencies added. Only the standard library (`itertools`
  counter) was used.

## Budgets and CI

`scripts/perf_budget.py` runs three small deterministic workloads through the
real CLI (2k valid rows, 2k rows with 10 percent invalid plus repair log,
verify over the messy output) and fails on wrong exit codes, wrong stored
row counts, a missing `consistent` verdict, or wall time over generous
ceilings (30 s migration, 60 s invalid migration, 60 s verify, floor
500 records/s; observed local times are about 1 s and 2,200 records/s).
Wired into `.github/workflows/verify.yml` as the `Performance budget` step.
Ceilings are set 10x or more above observed times so shared runners do not
flake; the gate catches order-of-magnitude regressions, not noise.

## What did not work (reverted, not shipped)

- Bounded per-field HMAC digest cache in `RunAnonymizer`: in-process A/B
  showed the cached variant slower (0.680 s vs 0.631 s median on 10k
  anonymized rows). HMAC is fast enough that dict bookkeeping costs more
  than it saves at these cardinalities. Reverted.
- `model_construct` instead of `model_copy` in `anonymize.transaction`:
  microbenchmark showed `model_construct` slower (0.007 s vs 0.004 s per
  2,000 copies). Reverted.
- Enum candidate precomputation plus `__dict__` instead of `model_dump` in
  `pipeline` field-rules path: steady-state A/B slower (0.390 s vs
  0.334 s median). Reverted.
- Exact-type-first reorder in `dynamic_store.database_value`: round 1
  favored it, round 2 flipped it. Within machine noise. Reverted per the
  no-speculative-changes rule.

## Floor justification (why 50 percent is out of reach here)

The remaining per-record costs are intrinsic to the specified behavior:

- Pydantic validation (`validate_python`) is the largest single cost on
  every path (0.03 to 0.07 s per 5k rows). Cutting it means skipping
  validation, which breaks the tool's contract.
- SQLite `executemany` plus `commit` plus `fsync` are durability costs.
  Weakening them (`synchronous=OFF` on the real target, larger unbounded
  batches) risks the committed-target guarantee verify checks.
- HMAC anonymization is required output. Caching was measured and lost.
- Report HTML and the machine-readable summary lines are specified byte
  formats; pretty-printing and escaping cannot change without breaking
  golden outputs.
- Cold start is interpreter plus pydantic import (0.163 s of 0.47 s for
  `--help`). The codebase already defers every heavy import out of the
  `--help` and `--version` paths.

Prior optimization rounds already shipped (see `benchmarks/`: JSON array
parser, schema text ASCII fast path at minus 35 percent on its workload,
duplicate-skip, bounded caps), so the easy wins are gone. The two shipped
changes take the waste that profiling proved (per-row UUID4 plus encoder
construction plus chunk loop; per-row ledger round trips). Further gains
without behavior change are in the low single digits.

## Risks, skipped items, manual QA

- Risks: the `_encode_line` rewrite changes transient memory shape (one
  `dumps` string instead of incremental chunks). Inputs are bounded by
  reader ceilings (512 KiB per value, 100 issues per entry), so the
  transient string stays far under the 8 MiB cap. The 0-byte-log regression
  caught during development (misindented write block) is covered by the
  parity check described above.
- Skipped: report streaming, `model_dump` avoidance in `jsonl_target`,
  single-pass source hashing, startup import surgery. Each needs output or
  architecture changes that break the zero-regression constraint.
- Manual QA: push the branch and watch the `verify` workflow on Linux and
  Windows; confirm the new `Performance budget` step passes on both. Run
  `scripts/soak_migration.py --rows 100000` on Linux if peak RSS matters
  for the release.
