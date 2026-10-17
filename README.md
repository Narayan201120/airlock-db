# AirLock DB

AirLock DB is a local-first migration tool for air-gapped environments. It reads legacy JSON, JSONL, NDJSON, XML, CSV, and TSV files, plain or compressed, validates every record against a strict schema, writes accepted records to a SQLite database, and produces one self-contained HTML report.

It makes no network requests. The report embeds its CSS and SVG, so it opens on a workstation with no internet access.

## What it does

- Streams input one record at a time, so memory stays bounded as files grow.
- Reads JSON, JSONL, NDJSON, XML, CSV, and TSV files, plain or compressed (`.gz`, `.zip`), or rows from a local SQLite database.
- Validates against the built-in financial-transaction model, or any client layout defined in a JSON or YAML config.
- Drafts a starter layout from the files themselves with `airlock-db infer`, with conflicts marked for review.
- Never crashes on a bad row. Rejected rows are reported with per-field reasons and the run continues.
- Reports schema drift: missing fields, undocumented extras, type mismatches, and new enum values.
- Writes accepted records to SQLite, a JSONL migration file, or both, with `skip` or `replace` conflict handling for SQLite.
- Optionally pseudonymizes sensitive fields with a run-scoped key that is never stored.
- Provides an interactive repair session for rejected rows, with crash-safe checkpoints.

## Requirements

- Python 3.10 or newer
- Pydantic 2.6 or newer and earlier than 3
- PyYAML 6.x, needed only for YAML schema configs
- SQLite, included with Python

## Install

On a connected machine:

```powershell
python -m pip install .
```

In an air-gapped environment, stage the wheels in an approved local wheelhouse, then install without an index:

```powershell
python -m pip install --no-index --find-links C:\offline\wheelhouse .
```

From a source checkout, `python main.py` works directly if Pydantic 2 is installed.

## Quick start

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --output-dir output
```

The run prints a summary and writes its files:

```text
Records checked: 32
Valid: 8; invalid: 24
SQLite: output/target_migration.db (committed)
Report: output/airlock_report.html
```

Open `output/airlock_report.html` to inspect the chart, drift counts, and issue table. Use a fresh output directory for each run. Existing rows are treated as duplicates or conflicts.

## Common tasks

### Migrate several sources

Repeat `--input`. Each path can be a supported file or a directory scanned recursively.

```powershell
airlock-db --input C:\legacy\batch-a --input C:\legacy\batch-b.jsonl --output-dir C:\migration\run-01
```

### Migrate from a legacy SQLite database

Read rows straight from a local database, without exporting files first. Column names must match the target schema.

```powershell
airlock-db --source-db C:\legacy\records.sqlite --output-dir C:\migration\run-02
```

Choose tables with `--source-table`, repeated to read several in one run.

### Migrate CSV or TSV files

Header names must match the schema. Delimiters default by file suffix; override with `--csv-delimiter` and `--csv-encoding` for legacy exports.

```powershell
python main.py --input C:\legacy\accounts.csv --output-dir csv-output
```

### Migrate compressed files

Read a gzip file or a zip archive directly. Archives stream, never extract, and every member with a supported suffix is validated.

```powershell
python main.py --input C:\legacy\export.jsonl.gz --output-dir gz-output
python main.py --input C:\legacy\batch.zip --output-dir zip-output
```

### Export a JSONL migration file

Write the accepted records as a clean JSONL file instead of a SQLite database. Use `both` to get both targets.

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --output-format jsonl --output-dir jsonl-output
```

### Resume a large migration

Commit and save progress every N records, then continue where an interrupted run stopped.

```powershell
python main.py --input C:\legacy\big.jsonl --commit-every 50000 --output-dir C:\migration\run-05
python main.py --input C:\legacy\big.jsonl --commit-every 50000 --resume --output-dir C:\migration\run-05
```

### Validate a client-specific schema

Replace the built-in model with a JSON or YAML layout. The bundled example is a support-ticket schema.

```powershell
python main.py --input legacy/tickets.json --config examples/schema_config.json --output-dir ticket-output
```

### Draft a schema from your data

Do not write the first layout from scratch. `infer` reads the source with the normal readers and drafts a config from what is actually in the files, with conservative types and the fields that need review marked in the output.

```powershell
python main.py infer --input legacy/tickets.json --output schema_draft.yaml
python main.py --input legacy/tickets.json --config schema_draft.yaml --output-dir ticket-output
```

### Run with pseudonymized fields

Account IDs, counterparties, and descriptions become one-run tokens. Report details are redacted.

```powershell
airlock-db --input C:\legacy\export.jsonl --anonymize --output-dir C:\migration\run-02
```

### Repair rejected rows

Write a repair log during migration, then review and fix rows. At each prompt: `e` edit, `p` set field, `r` remove field, `d` drop, `q` finish.

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --error-log --output-dir output
python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json
```

Full behavior, including resume and checkpoint rules: [docs/repair.md](docs/repair.md). Scripted fixes use `--decisions decisions.jsonl`, and `python main.py report --output-dir output` rebuilds the report with a Repair outcomes section.

### Test with real public data

A 10-row sample of actual UCI Online Retail records runs through the dynamic-schema path. The same notes cover converting and running MoneyData, a real anonymized bank ledger, through the built-in schema. Attribution and license terms are included.

```powershell
python main.py --input examples/real_data/online_retail_sample.jsonl --config examples/real_data/online_retail_schema.json --anonymize --output-dir real-data-output
```

See [examples/real_data/README.md](examples/real_data/README.md).

### Benchmark throughput

Measures JSONL parsing, dynamic-schema validation, SQLite writes, and report generation on synthetic records.

```powershell
python scripts/benchmark_migration.py --rows 100000 --runs 3 --batch-size 500
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | No record or source issues, and SQLite committed |
| `1` | Completed with issues: rejected rows, an incomplete source, a skipped conflicting ID, or a storage failure. The report is still written when the output directory permits it |
| `2` | Command-line, configuration, output-directory, requested repair-log, or report-writing error |

## Documentation

- [CLI reference](docs/cli-reference.md): every option, output file, size limit, conflict rule, anonymization detail, and the constraints and dynamic-schema formats.
- [Repair workflow](docs/repair.md): the session keys, staging, resume behavior, and privacy rules.
- [Roadmap](docs/roadmap.md): planned upgrades and how each one is verified.
- [Real-data examples](examples/real_data/README.md): public datasets, attribution, and conversion notes.
- [Verification skill](.cursor/skills/verify-airlock-db/SKILL.md): the end-to-end checks for this repo and a reusable [output verifier](.cursor/skills/verify-airlock-db/scripts/verify_migration_output.py).

## License

MIT. See [LICENSE](LICENSE).
