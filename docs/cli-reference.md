# CLI reference

Options, outputs, limits, and configuration formats. See [the repair guide](repair.md) for the interactive workflow and [the README](../README.md) for setup and quick start.

## Options

| Option | Purpose |
| --- | --- |
| `--input PATH` | Required. A JSON, JSONL, NDJSON, or XML file, or a directory scanned recursively. Repeat to add sources. |
| `--constraints JSON` | Optional local JSON file with additional business constraints. Built-in schema only. |
| `--config PATH` | Optional JSON or YAML file defining a dynamic record schema. |
| `--output-dir DIR` | Output directory. Defaults to `output`. |
| `--error-log [PATH]` | Opt in to a bounded rejected-row JSONL log for repair. Relative paths land under the output directory. With no path, uses `airlock_errors.jsonl`. |
| `--batch-size N` | SQLite write batch size, from 1 to 10,000. Defaults to 500. |
| `--max-report-issues N` | Issue entries embedded in the report, from 0 to 10,000. Defaults to 1000. |
| `--conflict-policy POLICY` | `skip` existing record IDs (default) or `replace` them with the changed input values. |
| `--anonymize` | Pseudonymize selected text fields before SQLite storage and redact issue details in the report. |
| `--verbose` | Show informational progress and summary log messages. |
| `--version` | Print the AirLock DB version. |

## Outputs

The command writes `target_migration.db` and `airlock_report.html` into the output directory, plus `airlock_errors.jsonl` only when `--error-log` is requested.

- Valid rows commit together in one SQLite transaction. A large transaction needs free disk space for the database and SQLite's rollback journal.
- Amounts are stored as normalized fixed-point strings with two fractional digits, preserving their value without exponent notation.
- Managed version 1 databases are upgraded in 1,000-row pages when opened. AirLock DB refuses to adopt an existing unversioned or incompatible `transactions` table.

## Repair log privacy and caps

The rejected-row sidecar is opt in. Without `--anonymize`, it contains rejected source values and validation details, so protect it like the original input data. Anonymized migrations omit raw rejected values; their optional sidecar contains redacted, non-repairable entries.

The sidecar is capped at 100,000 rejected records and 256 MiB. Each entry is capped at 8 MiB, its completion marker reports omitted records, and each row's issue trace is capped at 100 entries.

## Streaming and record limits

Input files are read incrementally. Top-level JSON arrays, JSONL/NDJSON lines, and XML record elements are decoded one record at a time. The whole source file is never loaded at once.

Per-record ceilings (file size and total row count remain unbounded):

- One JSON value: 512 KiB of characters.
- One JSONL line: 512 KiB of source bytes.
- One XML record: 512 KiB of text, 4 MiB of source markup, and 10,000 elements.

Oversized records are reported as `record_too_large`. JSON or XML parsing may stop for that source when it cannot safely resume at the next record.

Other boundaries:

- Directory scans inspect at most 10,000 directories and retain at most 10,000 entries from one directory. Larger directories are reported and skipped.
- SQLite flushes a pending batch when it reaches either `--batch-size` or an estimated 4 MiB of row payload, whichever comes first.
- The report retains at most `--max-report-issues` entries and is assembled in memory.

These controls bound AirLock DB's application buffers. They do not guarantee a fixed process RSS, because Python, Pydantic, SQLite, and the operating system use additional memory.

## Conflict handling

When an input transaction ID already exists, `skip` keeps the database row. The duplicate-row count includes all repeated IDs. The changed-ID conflict count is the subset whose values differ.

- Under `skip`, changed values make the command exit with status `1` and appear in the HTML report.
- `--conflict-policy replace` applies changed values from the current input. The report counts changed existing rows as updated, and those resolved conflicts do not cause exit status `1`.
- Repeated IDs with different values inside one input batch follow the same policy: `skip` keeps the first value, `replace` keeps the last value.

## Anonymized migration output

Add `--anonymize` to pseudonymize `account_id`, `counterparty`, and `description` before a validated record reaches SQLite.

- Tokens use keyed HMAC-SHA256 with a random key held in memory for this process. Equal field values receive the same token during one run. The key is not saved or included in the report.
- The report replaces source paths with run-scoped labels and redacts issue messages and rejected input values.
- Transaction IDs, timestamps, amounts, currencies, transaction types, and statuses remain unchanged.
- The option reduces direct identifier exposure. It does not hide identifying patterns in financial values or timing.

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

All keys are optional. Unknown keys, duplicate keys, invalid enum values, and inconsistent amount bounds stop the run before input is processed. The bundled `examples/constraints.json` shows a complete example. If you omit `--constraints`, only the target schema applies.

The constraints file applies only to the built-in financial transaction schema and cannot be combined with `--config`. Express those rules as field constraints in the layout instead.

## Dynamic schema configuration

Use `--config` to validate records against a client-specific layout instead of the built-in financial transaction model:

```powershell
python main.py --input legacy/tickets.json --config examples/schema_config.json --output-dir ticket-output
```

The same layout is available as YAML in `examples/schema_config.yaml`. YAML uses PyYAML's safe loader; duplicate keys and aliases are rejected. Both formats are limited to 1 MiB and compiled once at startup into a Pydantic v2 model with `extra="forbid"`.

### Layout format

A layout has `version: 1`, a Python-style `model_name`, an `id_field`, and a `fields` object. Each field specifies a type (`string`, `integer`, `number`, `decimal`, `boolean`, `uuid`, `date`, `datetime`, or `enum`) and may add:

- `required` (default `true`; `false` makes missing values `null`)
- `strict`
- string `min_length`, `max_length`, `pattern`
- numeric `gt`, `ge`, `lt`, `le`
- decimal `max_digits`, `decimal_places`
- `values`, a non-empty list, for enum fields
- `anonymize` (see below)

Unknown field options and invalid combinations fail before input processing.

### Storage

Every dynamic layout must identify a required `id_field`. It becomes the SQLite primary key and uses the selected `--conflict-policy`. Dynamic records retain all configured fields in a managed table named `dynamic_records_<schema fingerprint>` inside `target_migration.db`. Different layouts use separate tables. Decimal values are stored as text to avoid floating-point changes. The built-in layout continues to use the existing `transactions` table when `--config` is omitted.

### Anonymizing dynamic fields

To use `--anonymize` with a dynamic layout, mark one or more eligible string fields with `anonymize: true`. These fields must accept a 32-character token, cannot have a regex or enum restriction, and cannot be the `id_field`. The report redacts validation inputs and source paths as it does for the built-in model.

### Air-gapped installs

Include the PyYAML wheel alongside Pydantic in the approved local wheelhouse before installing the project with `--no-index`.

## Validation and report behavior

Invalid records do not enter SQLite. The pipeline keeps processing later records where it can, and the report counts missing fields, extra fields, type mismatches, unsupported enum values, configured constraint failures, malformed input, and incomplete files.

When a value fails a type check, the issue message states that no automatic type conversion was applied. AirLock DB never coerces a source value to make it pass validation.

The HTML report contains the success/failure SVG, drift counts, SQLite write totals, and a bounded issue table. Issue previews are capped. Setting `--max-report-issues 0` omits issue rows while still showing the omitted count. Expand an issue's input details in the table to inspect its captured value. The report embeds no scripts, fonts, stylesheets, or other external resources.

The report is written during migration. A repair session updates the database and its checkpoints, but does not regenerate the report.

## Performance benchmark

To measure JSONL parsing, dynamic-schema validation, SQLite writes, and report generation together on synthetic records, run:

```powershell
python scripts/benchmark_migration.py --rows 100000 --runs 3 --batch-size 500
```

The benchmark writes its fixture and outputs to a temporary directory, verifies the migrated row count after every run, and reports per-run throughput and median elapsed time. It includes CLI startup, so compare runs on the same machine with the same row count, schema, and batch size.

- Add `--invalid-every 10` to make every tenth row fail enum validation and measure the error path.
- Use `--config PATH` to benchmark another compatible dynamic schema.
