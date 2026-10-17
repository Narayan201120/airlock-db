# AirLock DB

AirLock DB is a local migration utility for financial transaction files. It reads JSON, JSONL, NDJSON, and XML, validates records against a strict Pydantic schema, records schema drift, writes valid transactions to SQLite, and creates a standalone HTML report.

The program does not make network requests. The report uses inline CSS and SVG, so it can be opened on an air-gapped workstation.

## Requirements

- Python 3.10 or newer
- Pydantic 2.6 or newer and earlier than 3
- PyYAML 6.x for YAML schema configuration
- SQLite, included with Python

Install the package in a connected development environment with:

```powershell
python -m pip install .
```

For an air-gapped environment, make Pydantic and the Python build tools available from an approved local wheelhouse, then install without an index:

```powershell
python -m pip install --no-index --find-links C:\offline\wheelhouse .
```

You can also run from a source checkout with `python main.py` if Pydantic 2 is already installed.

## Run a migration

```powershell
python main.py --input examples\messy_transactions.json --constraints examples\constraints.json --output-dir migration-output
```

Pass more than one source by repeating `--input`. Each path can be a supported file or a directory scanned recursively.

```powershell
airlock-db --input C:\legacy\batch-a --input C:\legacy\batch-b.jsonl --output-dir C:\migration\run-01
```

Options:

| Option | Purpose |
| --- | --- |
| `--input PATH` | Required. A JSON, JSONL, NDJSON, or XML file, or a directory. Repeat to add sources. |
| `--constraints JSON` | Optional local JSON file with additional business constraints. |
| `--config PATH` | Optional JSON or YAML file defining a dynamic record schema. |
| `--output-dir DIR` | Output directory. Defaults to `output`. |
| `--error-log [PATH]` | Opt in to a bounded rejected-row JSONL log for repair. Relative paths are under the output directory; with no path, use `airlock_errors.jsonl`. |
| `--batch-size N` | SQLite write batch size from 1 to 10,000. Defaults to 500. |
| `--max-report-issues N` | Issue entries embedded in the report, from 0 to 10,000. Defaults to 1000. |
| `--conflict-policy POLICY` | `skip` existing transaction IDs (default) or `replace` them with changed input values. |
| `--anonymize` | Pseudonymize selected text fields before SQLite storage and redact issue details in the report. |
| `--verbose` | Show informational progress and summary log messages. |
| `--version` | Print the AirLock DB version. |

The rejected-row sidecar is opt in. Without `--anonymize`, it contains rejected source values and validation details, so protect it like the original input data. Anonymized migrations omit raw rejected values; an optional sidecar contains redacted, non-repairable entries.

The command writes `target_migration.db` and `airlock_report.html` into the output directory, plus `airlock_errors.jsonl` only when requested. It commits valid rows together in one SQLite transaction. A large transaction needs free disk space for the database and SQLite's rollback journal. The repair sidecar is capped at 100,000 rejected records and 256 MiB; each entry is capped at 8 MiB, its completion marker reports omitted records, and each row's issue trace is capped at 100 entries. Amounts are stored as normalized fixed-point strings with two fractional digits, preserving their value without exponent notation. Managed version 1 databases are upgraded in 1,000-row pages when opened; AirLock DB refuses to adopt an existing unversioned or incompatible `transactions` table.

## Streaming and record limits

Input files are read incrementally. Top-level JSON arrays, JSONL/NDJSON lines, and XML record elements are decoded one record at a time; the whole source file is never loaded at once. To bound temporary Python objects as well as input buffers, one JSON value is limited to 512 KiB of characters, one JSONL line is limited to 512 KiB of source bytes, and one XML record is limited to 512 KiB of text, 4 MiB of source markup, and 10,000 elements. Oversized records are reported as `record_too_large`; JSON or XML parsing may stop for that source when it cannot safely resume at the next record.

These limits keep the parser working set bounded independently of the number of records in a file. Directory scans inspect at most 10,000 directories and retain at most 10,000 entries from one directory; larger directories are reported and skipped. SQLite flushes a pending batch when it reaches either `--batch-size` or an estimated 4 MiB of row payload, whichever comes first. The report retains at most `--max-report-issues` entries and is assembled in memory. These controls bound AirLock DB's application buffers; they do not guarantee a fixed process RSS because Python, Pydantic, SQLite, and the operating system use additional memory.

When an input transaction ID already exists, `skip` keeps the database row. The duplicate-row count includes all repeated IDs; the changed-ID conflict count is a subset identifying rows whose values differ. Under `skip`, changed values make the command exit with status `1` and appear in the HTML report. Use `--conflict-policy replace` to apply changed values from the current input; the report counts changed existing rows as updated, and those resolved conflicts do not cause exit status `1`. Repeated IDs with different values inside one input batch follow the same policy: the first value is kept with `skip`, while the last value is kept with `replace`.

## Anonymized migration output

Add `--anonymize` to pseudonymize `account_id`, `counterparty`, and `description` before a validated record reaches SQLite. Tokens use keyed HMAC-SHA256 with a random key held in memory for this process. Equal field values receive the same token during one run; the key is not saved or included in the report. The report replaces source paths with run-scoped labels and redacts issue messages and rejected input values. Transaction IDs, timestamps, amounts, currencies, transaction types, and statuses remain unchanged. The option reduces direct identifier exposure; it does not hide identifying patterns in financial values or timing.

Because the key is new for every run, pseudonyms differ on a later run. If you reuse a database containing the same transaction IDs, anonymized values may be treated as changed-row conflicts. Keep anonymized output separate from non-anonymized migrations.

## Constraints file

The target schema always enforces required fields, data types, enumerations, timestamp format, amount precision, and unknown-key rejection. A constraints file adds run-specific rules:

```json
{
  "allowed_currencies": ["USD", "EUR"],
  "allowed_account_ids": ["ACCT-001", "ACCT-002"],
  "allowed_transaction_types": ["DEBIT", "CREDIT"],
  "allowed_statuses": ["PENDING", "POSTED"],
  "min_amount": "0.01",
  "max_amount": "100000.00"
}
```

All keys are optional. Unknown keys, duplicate keys, invalid enum values, and inconsistent amount bounds stop the run before input is processed. The bundled `examples\constraints.json` shows a complete example. If you omit `--constraints`, only the target schema applies.

## Dynamic schema configuration

Use `--config` to validate records against a client-specific layout instead of the built-in financial transaction model:

```powershell
python main.py --input legacy\tickets.json --config examples\schema_config.json --output-dir ticket-output
```

The same layout is available as YAML in `examples\schema_config.yaml`. YAML uses PyYAML's safe loader; duplicate keys and aliases are rejected. Both formats are limited to 1 MiB and compiled once at startup into a Pydantic v2 model with `extra="forbid"`.

A layout has `version: 1`, a Python-style `model_name`, an `id_field`, and a `fields` object. Each field specifies a type (`string`, `integer`, `number`, `decimal`, `boolean`, `uuid`, `date`, `datetime`, or `enum`) and may add `required`, `strict`, string `min_length` / `max_length` / `pattern`, numeric `gt` / `ge` / `lt` / `le`, or decimal `max_digits` / `decimal_places`. Enum fields use a non-empty `values` list. Fields are required by default; `required: false` makes them optional and defaults missing values to `null`. Unknown field options and invalid combinations fail before input processing.

Every dynamic layout must identify a required `id_field`; it becomes the SQLite primary key and uses the selected `--conflict-policy`. Dynamic records retain all configured fields in a managed table named `dynamic_records_<schema fingerprint>` inside `target_migration.db`. Different layouts use separate tables. Decimal values are stored as text to avoid floating-point changes. The built-in layout continues to use the existing `transactions` table when `--config` is omitted.

The legacy `--constraints` file applies only to the built-in financial transaction schema and cannot be combined with `--config`; express those rules as field constraints in the layout. To use `--anonymize` with a dynamic layout, mark one or more eligible string fields with `anonymize: true`. These fields must accept a 32-character token, cannot have a regex or enum restriction, and cannot be the `id_field`. The report redacts validation inputs and source paths as it does for the built-in model.

For an air-gapped installation, include the PyYAML wheel alongside Pydantic in the approved local wheelhouse before installing the project with `--no-index`.

## Interactive repair

Create a repair log during migration, then review rejected rows and write accepted corrections into its SQLite target:

```powershell
python main.py --input examples\messy_transactions.json --constraints examples\constraints.json --error-log --output-dir output
python main.py repair --error-log output\airlock_errors.jsonl --constraints examples\constraints.json
```

After installing the package, the same command is available as `schema-sentry repair` (and `airlock-db repair`).

For a dynamic layout, pass the same schema config used for migration. If the migration used business constraints, pass the same `--constraints` file. Schema and constraint fingerprints are checked before any target is opened.

For each repairable row, the command displays its source, record number, rejected input, and Pydantic error details. Choose `e` to enter a complete corrected JSON object, `p` to set a field to a JSON value (quote text such as `"UNKNOWN"`), `r` to remove a field such as an undocumented key, `d` to drop the row, or `q` to finish the session. Every edit is revalidated. Accepted corrections and drop decisions are staged while you review the log; SQLite commits the corrected rows and their progress checkpoints together after review. Ctrl+C rolls back that commit. If you stop with `q` and rerun the same log against the same database, completed corrections and drops are skipped. Checkpoints use SHA-256 fingerprints of the complete repair log and each decision, and are scoped to conflict policy and anonymize mode. A concurrent session that submits a different decision for an already handled entry is rolled back. The temporary staging file is capped at 256 MiB. The database defaults to `target_migration.db` beside the error log and can be changed with `--output-db`. The command accepts piped input as well as a terminal, and EOF ends the session.

When a migration uses `--anonymize`, the repair log omits rejected input values and redacts issue details. Those entries are intentionally not repairable from that log. When repairing a non-anonymized log, add `--anonymize` to pseudonymize accepted rows before storage and redact source labels, record previews, and validation details in the terminal. Repair pseudonyms use a new run key and will differ from pseudonyms created by the original migration.

## Validation and report behavior

Invalid records do not enter SQLite. The pipeline keeps processing later records where it can, and the report counts missing fields, extra fields, type mismatches, unsupported enum values, configured constraint failures, malformed input, and incomplete files.

When a value fails a type check, the issue message states that no automatic type conversion was applied. AirLock DB never coerces a source value to make it pass validation.

The HTML report contains the success/failure SVG, drift counts, SQLite write totals, and a bounded issue table. Issue previews are capped, and setting `--max-report-issues 0` omits issue rows while still showing the omitted count. Expand an issue's input details in the table to inspect its captured value. The report embeds no scripts, fonts, stylesheets, or other external resources.

## Test with public real data

`examples\real_data\online_retail_sample.jsonl` contains 10 actual line items from the UCI Online Retail dataset. The sample omits the source `CustomerID` field. Its dynamic schema accepts the original local timestamps without assigning a timezone. See [the sample notes](examples/real_data/README.md) for attribution and license details.

Run it with:

```powershell
python main.py --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --anonymize --output-dir real-data-output
```

This checks real source values through the dynamic-schema migration path. It does not test native XLSX ingestion. AirLock DB accepts JSON, JSONL, NDJSON, and XML only.

The same notes describe how to test the built-in financial schema with MoneyData, a public, anonymized bank transaction dataset. Its optional XLSX-to-JSONL converter is separate from AirLock DB and documents the source fields that need test-only normalization.

## Local performance benchmark

To measure JSONL parsing, dynamic-schema validation, SQLite writes, and report generation together on synthetic records, run:

```powershell
python scripts/benchmark_migration.py --rows 100000 --runs 3 --batch-size 500
```

The benchmark writes its fixture and outputs to a temporary directory, verifies the migrated row count after every run, and reports per-run throughput and median elapsed time. It includes CLI startup, so compare runs on the same machine with the same row count, schema, and batch size. Add `--invalid-every 10` to make every tenth row fail enum validation and measure the error path. Use `--config PATH` to benchmark another compatible dynamic schema.

Exit codes:

- `0`: validation completed without record or source issues and SQLite committed.
- `1`: validation found issues, an input source was incomplete, a changed transaction ID was skipped, or SQLite could not commit. The HTML report is still written when the output directory permits it.
- `2`: command-line, schema or constraints configuration, output-directory, requested repair-log, or report-writing error.
