# AirLock DB

Local-first migration tool for air-gapped environments. It reads legacy files, validates every record against a strict schema, writes accepted records to SQLite, and produces one self-contained HTML report.

- Makes no network requests.
- Embeds CSS and SVG in the report, so it opens on a workstation with no internet access.
- Never crashes on a bad row. Rejected rows get per-field reasons and the run continues.

## Contents

- [Features](#features)
- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Common tasks](#common-tasks)
- [Exit codes](#exit-codes)
- [Documentation](#documentation)
- [License](#license)

## Features

| Area | What you get |
| --- | --- |
| Input | JSON, JSONL, NDJSON, XML, CSV, TSV, plain or compressed (`.gz`, `.zip`), plus rows from a local SQLite database |
| Memory | Streams one record at a time, so memory stays bounded as files grow |
| Validation | Built-in financial-transaction model, or any client layout from a JSON or YAML config |
| Schema drafting | `airlock-db infer` drafts a starter layout from your files and marks conflicts for review |
| Drift reporting | Missing fields, undocumented extras, type mismatches, new enum values |
| Output | SQLite, a JSONL migration file, or both, with `skip` or `replace` conflict handling for SQLite |
| Verification | `airlock-db verify` rebuilds every stored row and reports changed, missing, or unexpected records |
| Privacy | Optional pseudonymization of sensitive fields with a run-scoped key that is never stored |
| Repair | Interactive session for rejected rows, with crash-safe checkpoints |
| Large inputs | `--sample N` previews, `--commit-every N` with `--resume` recovers, `--source-query` reads custom SQL |

## Install

Requirements:

- Python 3.10 or newer
- Pydantic 2.6 or newer, below 3
- PyYAML 6.x, installed with the package and read only for YAML schema configs
- SQLite, included with Python

On a connected machine:

```powershell
python -m pip install .
```

In an air-gapped environment, stage the Pydantic and PyYAML wheels in an approved local wheelhouse, then install without an index:

```powershell
python -m pip install --no-index --find-links C:\offline\wheelhouse .
```

From a source checkout, `python main.py` works directly if Pydantic 2 is installed. Every `airlock-db` command below also runs as `python main.py`.

## Quick start

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --output-dir output
```

Output:

```text
Records checked: 32
Valid: 8; invalid: 24
SQLite: output/target_migration.db (committed)
Report: output/airlock_report.html
```

Open `output/airlock_report.html` to see the chart, drift counts, and issue table.

Use a fresh output directory for each run. Existing rows count as duplicates or conflicts.

## Commands

| Command | Purpose |
| --- | --- |
| `airlock-db` | Migrate sources into the target |
| `airlock-db infer` | Draft a schema config from source files |
| `airlock-db verify` | Check a finished target against its sources |
| `airlock-db repair` | Fix rejected rows interactively or from a decisions file |
| `airlock-db report` | Rebuild the HTML report from an output directory |

`schema-sentry` is a second name for the same entry point.

### Key options

| Option | Purpose |
| --- | --- |
| `--input PATH` | File, archive, or directory. Repeatable |
| `--source-db PATH` | Local SQLite database as a source. Repeatable |
| `--config PATH` | JSON or YAML schema layout |
| `--constraints JSON` | Extra business rules. Built-in schema only |
| `--output-dir DIR` | Output directory. Default `output` |
| `--output-format` | `sqlite` (default), `jsonl`, or `both` |
| `--conflict-policy` | `skip` (default) or `replace` existing record IDs |
| `--batch-size N` | SQLite write batch, 1 to 10,000. Default 500 |
| `--max-report-issues N` | Issues embedded in the report, 0 to 10,000. Default 1000 |
| `--verbose` | Progress and summary log messages |
| `--version` | Print the version |

Every option is in the [CLI reference](docs/cli-reference.md).

### Output files

| File | Written when |
| --- | --- |
| `airlock_report.html` | Always |
| `airlock_summary.json` | Always. Machine-readable counts, drift, storage stats, exit code |
| `target_migration.db` | SQLite target |
| `target_migration.jsonl` | JSONL target |
| `airlock_errors.jsonl` | `--error-log` is set |
| `airlock_verify.json` | `verify` finds a mismatch |

## Common tasks

### Read inputs

#### Several sources

Repeat `--input`. Each path is a supported file or a directory, scanned recursively.

```powershell
airlock-db --input C:\legacy\batch-a --input C:\legacy\batch-b.jsonl --output-dir C:\migration\run-01
```

#### CSV or TSV

Header names must match the schema. The delimiter defaults by file suffix. Override it and the encoding with `--csv-delimiter` and `--csv-encoding`.

```powershell
python main.py --input C:\legacy\accounts.csv --output-dir csv-output
```

#### Compressed files

Gzip files and zip archives stream directly and are never extracted. Every zip member with a supported suffix is validated.

```powershell
python main.py --input C:\legacy\export.jsonl.gz --output-dir gz-output
python main.py --input C:\legacy\batch.zip --output-dir zip-output
```

#### A legacy SQLite database

Read rows directly, with no file export. Column names must match the target schema.

```powershell
airlock-db --source-db C:\legacy\records.sqlite --output-dir C:\migration\run-02
```

- Choose tables with `--source-table`. Repeat it to read several tables in one run.
- Read a filtered view with one `--source-query` per database.

```powershell
airlock-db --source-db C:\legacy\records.sqlite --source-query "SELECT * FROM records WHERE status = 'POSTED'" --output-dir C:\migration\run-03
```

### Write outputs

#### JSONL migration file

Write accepted records as clean JSONL instead of SQLite. Use `--output-format both` for both targets.

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --output-format jsonl --output-dir jsonl-output
```

#### Pseudonymized fields

Account IDs, counterparties, and descriptions become one-run tokens. Report details are redacted.

```powershell
airlock-db --input C:\legacy\export.jsonl --anonymize --output-dir C:\migration\run-02
```

### Handle large migrations

#### Resume an interrupted run

Commit and save progress every N records. Rerun the same command with `--resume` to continue where the run stopped.

```powershell
python main.py --input C:\legacy\big.jsonl --commit-every 50000 --output-dir C:\migration\run-05
python main.py --input C:\legacy\big.jsonl --commit-every 50000 --resume --output-dir C:\migration\run-05
```

- Both options need the SQLite target.
- `--resume` cannot combine with `--anonymize`, because pseudonyms are run-scoped.
- `--resume` skips writes only when the source file is unchanged (same size and modification time).

#### Preview before the full run

Stop after N accepted records. The output directory holds only the scanned prefix. The report, run summary, and any repair log record that the run was a preview.

```powershell
python main.py --input C:\legacy\big.jsonl --sample 5000 --output-dir preview-output
```

A preview is not a handoff. `verify` reports the unscanned records as missing, so rerun without `--sample` before verifying.

### Work with schemas

#### Validate against a client schema

Replace the built-in model with a JSON or YAML layout. The bundled example is a support-ticket schema.

```powershell
python main.py --input legacy/tickets.json --config examples/schema_config.json --output-dir ticket-output
```

#### Draft a schema from your data

`infer` reads the source with the normal readers and drafts a config from what is in the files. It uses conservative types and marks fields that need review.

```powershell
python main.py infer --input legacy/tickets.json --output schema_draft.yaml
python main.py --input legacy/tickets.json --config schema_draft.yaml --output-dir ticket-output
```

#### Verify a handoff

Before a target database leaves the machine, confirm it still matches its sources. `verify` re-reads the sources, rebuilds every stored row with the migration's own rules, and compares per-record digests against the committed database, which it opens read-only.

```powershell
python main.py verify --input legacy/tickets.json --config schema_draft.yaml --output-dir ticket-output
```

- Pass the same source and schema flags as the original run.
- Exit code `0` means the database matches the sources under the recorded duplicate policy. Exit code `1` means a mismatch or a missing target database. Exit code `2` means bad flags or config files.
- JSONL-only runs have no database, so `verify` refuses them.
- A changed, missing, or extra row fails the check, lists the row IDs, and writes `airlock_verify.json` into the output directory.
- Anonymized fields stay out of the digests, because their tokens use a run-scoped key that is never stored.

### Repair rejected rows

Write a repair log during migration, then review and fix rows.

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --error-log --output-dir output
python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json
```

Before you repair:

- Do not add `--anonymize` to a migration whose log you plan to repair. Anonymized logs omit rejected values and are not repairable.
- Pass the same `--config` and `--constraints` used for the migration. A fingerprint mismatch stops the session.
- The database defaults to `target_migration.db` beside the error log. Change it with `--output-db`.

Prompt keys:

| Key | Action |
| --- | --- |
| `e` | Edit the row |
| `p` | Set a field |
| `r` | Remove a field |
| `d` | Drop the row |
| `q` | Finish |

Related options:

- `repair --emit-decisions decisions.jsonl` writes an inert template with one `skip` line per repairable entry. Edit it to script fixes. It refuses to replace an existing file without `--overwrite`.
- `repair --decisions decisions.jsonl` applies a decisions file.
- `python main.py report --output-dir output` rebuilds the report with a Repair outcomes section.

Resume and checkpoint rules are in [docs/repair.md](docs/repair.md).

### Try real data and measure speed

#### Public dataset sample

A 10-row sample of real UCI Online Retail records runs through the dynamic-schema path.

```powershell
python main.py --input examples/real_data/online_retail_sample.jsonl --config examples/real_data/online_retail_schema.json --anonymize --output-dir real-data-output
```

[examples/real_data/README.md](examples/real_data/README.md) also covers converting and running MoneyData, a real anonymized bank ledger, through the built-in schema. It includes attribution and license terms.

#### Benchmark throughput

Measures JSONL parsing, dynamic-schema validation, SQLite writes, and report generation on synthetic records.

```powershell
python scripts/benchmark_migration.py --rows 100000 --runs 3 --batch-size 500
```

#### Soak a large migration

Generates a synthetic JSONL source, migrates it with the real CLI, and asserts the stored row count, summary counts, wall time, and peak memory against an optional ceiling. Results land in `soak_verification.json`.

```powershell
python scripts/soak_migration.py --rows 1000000 --max-peak-rss-mib 300
```

- On the development machine, 1,000,000 rows migrate in about 37 seconds (27k records/s) at a 41 MiB peak. Peak memory is the same at 20,000 rows, so buffers stay flat as row count grows.
- Peak RSS is exact for a single run on Linux or macOS, where the platform reports the finished child's high-water mark.
- Other setups sample the live process every 50 ms.
- A platform that cannot report memory leaves it unmeasured instead of failing the ceiling check.
- CI runs a 100,000-row variant with the same ceiling.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | No record or source issues, and SQLite committed |
| `1` | Completed with issues: rejected rows, an incomplete source, a skipped conflicting ID, or a storage failure. The report is still written when the output directory permits it |
| `2` | Command-line, configuration, output-directory, requested repair-log, or report-writing error |

## Documentation

| Doc | Covers |
| --- | --- |
| [CLI reference](docs/cli-reference.md) | Every option, output file, size limit, conflict rule, anonymization detail, and the constraints and dynamic-schema formats |
| [Repair workflow](docs/repair.md) | Session keys, staging, resume behavior, privacy rules |
| [Roadmap](docs/roadmap.md) | Planned upgrades and how each one is verified |
| [Real-data examples](examples/real_data/README.md) | Public datasets, attribution, conversion notes |
| [Verification skill](.cursor/skills/verify-airlock-db/SKILL.md) | End-to-end checks for this repo, plus a reusable [output verifier](.cursor/skills/verify-airlock-db/scripts/verify_migration_output.py) |

## License

MIT. See [LICENSE](LICENSE).
