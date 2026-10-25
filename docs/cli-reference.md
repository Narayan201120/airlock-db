# CLI reference

Options, outputs, limits, and configuration formats. See [the guide](guide.md) for the repair workflow and the air-gap transfer, and [the README](../README.md) for setup and quick start.

## Options

| Option | Purpose |
| --- | --- |
| `--input PATH` | A JSON, JSONL, NDJSON, XML, CSV, or TSV file, a `.gz` file or `.zip` archive of those, or a directory scanned recursively. Repeat to add sources. At least one `--input` or `--source-db` is required. |
| `--source-db PATH` | A local SQLite database read as an input source. Repeat for more databases. |
| `--source-table NAME` | Table to read from each `--source-db`. Repeat to read several tables in one run. Defaults to the only table found; required when the database holds several. |
| `--source-query SQL` | A single SELECT statement read from each `--source-db` instead of a table. One query per database; cannot be combined with `--source-table`. |
| `--csv-delimiter CHAR` | Delimiter for CSV input. Defaults to a comma, or a tab for `.tsv` files. |
| `--csv-encoding NAME` | Text encoding for CSV input. Defaults to `utf-8-sig` (tolerates a BOM). |
| `--input-encoding NAME` | Text encoding for JSON, JSONL, CSV, and TSV input. Defaults to `utf-8-sig`. For CSV and TSV, `--csv-encoding` wins when both are given. |
| `--fixed-width PATH` | JSON or YAML layout that reads every input file as fixed-width records instead of choosing a reader by suffix. |
| `--constraints JSON` | Optional local JSON file with additional business constraints. The six built-in keys serve the built-in schema; `field_rules` serves dynamic schemas. |
| `--config PATH` | Optional JSON or YAML file defining a dynamic record schema. |
| `--output-dir DIR` | Output directory. Defaults to `output`. |
| `--output-format FORMAT` | `sqlite` (default), `jsonl`, or `both`. Chooses the migration target format. |
| `--commit-every N` | Commit accepted records and save resumable progress every N records. Requires the SQLite target. |
| `--resume` | Skip writing records at or below a saved checkpoint from an earlier `--commit-every` run. |
| `--sample N` | Stop after N accepted records are written. The target holds only the scanned prefix, and the report, summary, and repair log record the preview. |
| `--dry-run` | Read and validate every source, print counts and issue codes, and write nothing. Refuses `--error-log`, `--commit-every`, `--resume`, and `--sample`. |
| `--error-log [PATH]` | Opt in to a bounded rejected-row JSONL log for repair. Relative paths land under the output directory. With no path, uses `airlock_errors.jsonl`. |
| `--batch-size N` | SQLite write batch size, from 1 to 10,000. Defaults to 500. |
| `--max-report-issues N` | Issue entries embedded in the report, from 0 to 10,000. Defaults to 1000. |
| `--conflict-policy POLICY` | `skip` existing record IDs (default) or `replace` them with the changed input values. |
| `--anonymize` | Pseudonymize selected text fields before SQLite storage and redact issue details in the report. |
| `--verbose` | Show informational progress and summary log messages. |
| `--version` | Print the AirLock DB version. |

## Outputs

The command writes `airlock_report.html` and `airlock_summary.json` into the output directory, plus `target_migration.db` for the SQLite target and `target_migration.jsonl` for the JSONL target, depending on `--output-format`. `airlock_errors.jsonl` appears only when `--error-log` is requested.

- Valid rows commit together in one SQLite transaction. A large transaction needs free disk space for the database and SQLite's rollback journal.
- Amounts are stored as normalized fixed-point strings with two fractional digits, preserving their value without exponent notation.
- Managed version 1 databases are upgraded in 1,000-row pages when opened. AirLock DB refuses to adopt an existing unversioned or incompatible `transactions` table.
- `airlock_summary.json` is a machine-readable copy of the run: counts, drift categories, storage statistics, source labels, a manifest of the source files with their sizes and SHA-256 digests, and the exit code. CI jobs and dashboards can read it without parsing the HTML. Anonymize mode redacts source labels and reduces the manifest to labels.

### JSONL target output

`--output-format jsonl` writes one compact JSON object per accepted record, in input order, to `target_migration.jsonl`. The file streams to a temporary name and renames into place, so a crash leaves either the previous file or no file. `--output-format both` writes the SQLite database and the JSONL file; each target commits independently.

`--conflict-policy` applies to the SQLite target only. The JSONL file contains every accepted record, including repeated IDs within one run, and no duplicate filtering is applied.

### Source database input

`--source-db` opens a local SQLite file read-only. Rows stream one at a time, and the file is never modified. Tables are chosen by `--source-table` (repeat it to read several tables in one run), or selected automatically when the database contains exactly one table; never selected silently when several exist. Each table gets its own source label and row numbering.

To read a filtered view instead, pass one `--source-query` per database and skip `--source-table` for the whole run. The statement must be a single `SELECT` (or `WITH ... SELECT`); anything else is refused before the run starts. It runs against the read-only connection, gets the `(query)` label, and its columns behave exactly like table columns. Two columns with the same name are reported as `source_db_query_columns`, because one row object cannot hold both values.

Column names behave exactly like JSON keys. A missing column is a missing-field error, an unknown column is an extra-field error, and every value passes through the same strict validation. A `--source-db` path cannot serve as the migration target database or the repair log.

### Resumable migrations

For very large sources, `--commit-every N` commits accepted records and saves per-source progress every N records, so an interrupted run continues instead of starting over. Progress lives in a `migration_progress` table inside the target database.

`--resume` skips writing records at or below the saved checkpoint when the source file is unchanged (same size and modification time). Records are still validated for the report and exit code; only storage writes and repair-log entries are skipped, and the run prints how many records it skipped. Resume matches the saved progress by schema, conflict policy, and source list.

Resume requires the SQLite target, cannot be combined with `--anonymize` (pseudonyms are run-scoped), and with `--error-log` the written log covers only the records processed by that run. Without `--commit-every`, behavior is unchanged: accepted rows commit together in one transaction at the end.

### Preview runs

`--sample N` stops a run after N accepted records are written, for a fast sanity check of a large source. The target, the report, and `airlock_summary.json` describe only the scanned prefix; the summary carries `records.sample_limit` and `records.sample_reached`, and the report shows a preview notice. A repair log written by a sampled run is marked incomplete, so `repair` warns that the log covers only part of the sources.

A preview is not a handoff. `verify` against a sampled target reports the unscanned records as missing, by design; rerun without `--sample` before verifying.

### Dry run

`--dry-run` reads every source through the normal readers, transforms, and constraints, then prints the counts and issue codes without writing anything: no target, report, summary, checkpoint, or repair log. It does not create the output directory.

```powershell
python main.py --input C:\legacy\export.jsonl --config client_schema.yaml --dry-run
```

Exit codes match a migration: `0` when the sources are clean, `1` when records or sources have issues, and `2` for bad flags or configuration. The output ends with a machine-readable line:

```text
dry_run_result checked=32 valid=8 invalid=24 issue_occurrences=26 files_written=0
```

`--dry-run` cannot be combined with `--error-log`, `--commit-every`, `--resume`, or `--sample`, because each one writes a file or depends on a target. Configuration errors still fail before any record is read.

### CSV and TSV input

`.csv` and `.tsv` files are read with the standard library `csv` module. The first non-blank line is the header; header names map to schema fields exactly like JSON keys, so missing and unknown columns produce the usual drift errors. Empty cells become `null`. A row with more values than the header defines is rejected as `csv_extra_values`.

Encoding defaults to `utf-8-sig`, which tolerates a byte-order mark, and the delimiter defaults to a comma or, for `.tsv`, a tab. Override the delimiter with `--csv-delimiter` and the encoding with `--csv-encoding`; `--input-encoding` also covers CSV and TSV when an export is not UTF-8. A missing or duplicate header stops the file with a clear `csv_header_missing` or `csv_duplicate_header` source error.

### Fixed-width input

`--fixed-width layout.json` reads undelimited positional text, such as a mainframe or COBOL handoff. The layout is a small JSON or YAML file with an optional `encoding` (default `utf-8-sig`), an optional `skip_lines` count for banner lines, and a `fields` list of named spans:

```json
{
  "encoding": "cp1252",
  "skip_lines": 1,
  "fields": [
    {"name": "transaction_id", "start": 1, "length": 36},
    {"name": "account_id", "start": 37, "length": 8}
  ]
}
```

`start` is a 1-based column and `length` counts columns, spaces included. Padding inside a span is trimmed, and an all-padding span becomes `null` exactly like an empty CSV cell. With the flag set, every discovered file, archive member, and directory-scan result is read with this one layout; the file suffix no longer selects a reader. Overlapping spans, out-of-order spans, duplicate names, and unknown keys stop the run before any input is read. A line shorter than the layout rejects that record with a `fixed_width_short_line` issue and the run continues. `verify` accepts the same flag to replay the original reader, and `infer` accepts it to draft a layout from fixed-width data.

### Compressed input

`.gz` files and `.zip` archives are read directly. A `.gz` file is one compressed stream, so `export.jsonl.gz` reads as JSONL. A `.zip` archive is a flat set of members: every file member with a supported suffix is validated in name order, each under its own source label, and unsupported members are skipped. Nested archives are ignored.

Compression is a container only. The same readers, size limits, and issue codes apply to the decompressed bytes, and nothing is extracted to disk. A file that cannot be read as gzip or zip becomes a source-level `io_error` or `archive_error`, and the run continues.

## Repair log privacy and caps

The rejected-row sidecar is opt in. Without `--anonymize`, it contains rejected source values and validation details, so protect it like the original input data. Anonymized migrations omit raw rejected values; their optional sidecar contains redacted, non-repairable entries.

The sidecar is capped at 100,000 rejected records and 256 MiB. Each entry is capped at 8 MiB, its completion marker reports omitted records, and each row's issue trace is capped at 100 entries.

## Streaming and record limits

Input files are read incrementally. Top-level JSON arrays, JSONL/NDJSON lines, and XML record elements are decoded one record at a time. The whole source file is never loaded at once.

Per-record ceilings (file size and total row count remain unbounded):

- One JSON value: 512 KiB of characters.
- One JSONL line: 512 KiB of source bytes.
- One XML record: 512 KiB of text, 4 MiB of source markup, and 10,000 elements.
- One CSV/TSV row: 512 KiB of joined field text.

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

The six keys above apply only to the built-in financial transaction schema. `field_rules` carries per-record business rules for the built-in schema and for dynamic ones, and combines with `--config`:

```json
{
  "field_rules": [
    {"field": "status", "enum": ["open", "closed"]},
    {"field": "quantity", "min": 1, "max": 500},
    {"field": "sku", "pattern": "^SKU-[0-9]{4}$"},
    {"required_any_of": ["email", "phone"]}
  ]
}
```

Each entry names one field with any of `enum`, `min`, `max`, and `pattern`, or lists `required_any_of` fields. Rules run after validation, on records that passed the schema, and each failure rejects the record with its own reason: `constraint_enum`, `constraint_range`, `constraint_pattern`, or `constraint_required_any_of`. Enum values compare numerically when both sides are numbers and by trimmed text otherwise, case-sensitive. `min` and `max` are inclusive, `pattern` must match the whole text value, and `required_any_of` passes when any listed field holds a value that is not null or empty. Rules apply only when the field is present; nulls pass, and required-ness stays the schema's job. Unknown keys inside an entry, two entries for one field, an entry that mixes `required_any_of` with field keys, an empty enum, `min` above `max`, and invalid regexes stop the run with exit 2. A file that carries any of the six built-in keys cannot be combined with `--config`; one that carries only `field_rules` can.

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

### Normalizing field values

A field may declare a `transforms` list. Rules run in listed order before validation, and only on string values; other types pass through untouched. A value a rule cannot convert rejects the record with a `transform_failed` issue that names the field and the rule.

- `{"trim": true}` strips surrounding whitespace. Use it when `strip_whitespace` is `false`.
- `{"lowercase": true}` and `{"uppercase": true}` fold case, including on enum fields so folded values can match the declared choices.
- `{"datetime": "%d/%m/%Y"}` parses a `date` or `datetime` field with an explicit `strptime` pattern and writes ISO 8601. A pattern with time directives needs a `datetime` field and writes a timestamp; a date-only pattern writes `YYYY-MM-DD`.
- `{"decimal": {"dot": false, "places": 2}}` reads decimal text, treats the selected character as the decimal separator (the other one must group digits in threes), rounds half up to `places` decimal places when given, and writes plain dot-separated text. `dot` defaults to `true`; `places` is optional.
- `{"alias": {"n/a": "HIGH"}}` maps exact source values to declared enum choices. Unmapped values pass through and are validated against the enum as usual.

Transforms are declared normalization, not automatic coercion: a value no rule converts still fails instead of being guessed. Rules feed the schema fingerprint, so editing them targets a new table and a new resume namespace, and `verify` re-applies the same rules before comparing digests. The run summary counts each changed field value under `records.normalized_fields`.

### Drafting a layout from data

`airlock-db infer` writes a starter layout from the records themselves:

```powershell
python main.py infer --input legacy/tickets.json --output schema_draft.yaml
python main.py --input legacy/tickets.json --config schema_draft.yaml --output-dir ticket-output
```

The draft reads the input with the normal readers, including directories, compressed files, CSV, and fixed-width files under `--fixed-width`, then classifies each field from the values it sees. Types stay conservative: numeric text keeps its exact spelling as `decimal`, ISO text becomes `date` or `datetime`, and mixed text formats fall back to `string`, which accepts every observed value. A field is `required` only when it is present and non-null in every scanned record.

Conflicts are marked, not hidden. When no safe type fits the observed values, the draft uses the most common kind, prints the field under `Conflicts`, and the first migration reports the minority rows as invalid. YAML drafts carry these notes as comments. Review the draft before trusting it; `infer` never writes lengths, patterns, or enum lists.

Options: `--output` picks the draft path and format (`.yaml`, `.yml`, or `.json`, default `schema_draft.yaml`), `--model-name` renames the model, `--id-field` overrides the detected identity field, `--limit` bounds the scan (default 10,000 records, `0` scans everything), and `--overwrite` replaces an existing file. An identity field is detected from names such as `id`, `uuid`, or `*_id`, preferring values that look like UUIDs; otherwise the first field with no nulls is used.

### Storage

Every dynamic layout must identify a required `id_field`. It becomes the SQLite primary key and uses the selected `--conflict-policy`. Dynamic records retain all configured fields in a managed table named `dynamic_records_<schema fingerprint>` inside `target_migration.db`. Different layouts use separate tables. Decimal values are stored as text to avoid floating-point changes. The built-in layout continues to use the existing `transactions` table when `--config` is omitted.

### Anonymizing dynamic fields

To use `--anonymize` with a dynamic layout, mark one or more eligible string fields with `anonymize: true`. These fields must accept a 32-character token, cannot have a regex or enum restriction, and cannot be the `id_field`. The report redacts validation inputs and source paths as it does for the built-in model.

### Air-gapped installs

Include the PyYAML wheel alongside Pydantic in the approved local wheelhouse before installing the project with `--no-index`. The [guide](guide.md#moving-an-install-into-an-air-gapped-machine) walks the two-machine transfer.

## Checking configuration

`check` loads configuration files without reading input and writes nothing:

```powershell
python main.py check --config client_schema.yaml --constraints field_rules.json --fixed-width layout.yaml
```

Pass at least one of `--config`, `--constraints`, or `--fixed-width`. The command uses the same loaders and cross-file rules as a migration: a schema file combined with a constraints file that carries built-in keys is refused, because built-in rules belong to the built-in schema. Every file loads before anything prints, so a bad file produces one error and no partial report.

The output names the schema, the rule counts, the layout details, and the fingerprints that the repair log and the run summary record:

```text
Schema: SupportTicket (6 fields, 0 transforms, fingerprint eff366dfaab0237e)
Constraints: 0 built-in rules, 4 field rules (fingerprint 7d7e4fd1f678ca9c376db5e456b735d9618c2eaa02f97a1cd1dc98b693b2f4a9)
Layout: 3 fields, encoding utf-8-sig, skip_lines 1, record_length 38
Check: configuration is valid. No files were written.
check_ok schema=dynamic schema_fields=6 transforms=0 builtin_rules=0 field_rules=4 layout_fields=3
```

Exit codes: `0` valid, `2` a bad flag or a file that does not load. No input is read, so no source, target, report, or output directory is created.

## Verifying a migration handoff

After a run, `verify` reconciles the committed target database with the sources that produced it:

```powershell
python main.py verify --input legacy/tickets.json --config schema_draft.yaml --output-dir ticket-output
```

The command re-reads the sources with the flags of the original run (`--input`, `--source-db`, `--source-table`, `--source-query`, `--csv-delimiter`, `--csv-encoding`, `--input-encoding`, `--fixed-width`, `--config`, or `--constraints`). It rebuilds every stored row with the migration's own row builder, computes one digest per record, and compares those digests with the rows in `target_migration.db`, which it opens read-only. Changed, missing, and unexpected rows are reported with the first 10 IDs of each kind, and the same result lands in `airlock_verify.json` inside the output directory.

Each source file is also hashed again and compared with the `source_manifest` recorded by the run, by size and SHA-256, so a swapped file of the same shape fails with the file named even when every row still matches. An anonymized run keeps labels only, and a summary without a manifest skips the check with a notice.

When the database is absent but `target_migration.jsonl` is present, the file is verified the same way: each line is read as the model's JSON dump, and the `target` block in `airlock_verify.json` names the checked file. A two-target run verifies its database, which is canonical.

Duplicate keys follow the recorded policy: `skip` keeps the first occurrence, `replace` keeps the last. `--conflict-policy` overrides the policy recorded in `airlock_summary.json`, and a disagreement prints a warning. For anonymized runs the anonymized fields are excluded from the digests, because the tokens use a run-scoped key that is never stored; `--anonymized` forces that behavior when the summary is missing.

Exit codes: `0` consistent, `1` mismatch or a missing target, `2` bad flags or configuration files.

## Validation and report behavior

Invalid records do not enter SQLite. The pipeline keeps processing later records where it can, and the report counts missing fields, extra fields, type mismatches, unsupported enum values, configured constraint failures, malformed input, and incomplete files.

When a value fails a type check, the issue message states that no automatic type conversion was applied. AirLock DB never coerces a source value to make it pass validation.

The HTML report contains the success/failure SVG, drift counts, SQLite write totals, and a bounded issue table. Issue previews are capped. Setting `--max-report-issues 0` omits issue rows while still showing the omitted count. Expand an issue's input details in the table to inspect its captured value. The report embeds no scripts, fonts, stylesheets, or other external resources.

The report is written during migration. A repair session updates the database and its checkpoints, but does not regenerate the report. Rebuild it after a repair pass with `python main.py report --output-dir DIR`. Add `--compare OTHER_DIR` to append a delta section that reconciles the run against another run's `airlock_summary.json`: record-count changes, issue categories that appeared or disappeared, and per-source changes. The delta reads the two summaries only, and a missing or malformed baseline summary stops the command with exit code 2 before the report is rewritten.

## Performance benchmark

To measure JSONL parsing, dynamic-schema validation, SQLite writes, and report generation together on synthetic records, run:

```powershell
python scripts/benchmark_migration.py --rows 100000 --runs 3 --batch-size 500
```

The benchmark writes its fixture and outputs to a temporary directory, verifies the migrated row count after every run, and reports per-run throughput and median elapsed time. It includes CLI startup, so compare runs on the same machine with the same row count, schema, and batch size.

- Add `--invalid-every 10` to make every tenth row fail enum validation and measure the error path.
- Use `--config PATH` to benchmark another compatible dynamic schema.

## Memory soak

`scripts/soak_migration.py` proves the streaming story at scale: it generates a synthetic JSONL source, runs the real CLI once per `--runs`, and asserts the stored row count, the summary counts (`checked`/`valid`/`invalid`, `file_errors`, storage `inserted`/`status`), the wall time, and the peak memory against `--max-peak-rss-mib` when given.

```powershell
python scripts/soak_migration.py --rows 1000000 --max-peak-rss-mib 300 ci-soak
```

The optional `work_dir` keeps the generated source, each run's output directory, and `soak_verification.json`; without it the script uses a fresh temporary directory. Peak RSS is exact when a single run is measured on Linux or macOS (`getrusage` reports the finished child's high-water mark). On Windows, and with `--runs` above 1, the live process is sampled every 50 ms, so the number is a close approximation. When no platform reader is available the evidence records `null` and the ceiling is not checked, never failed.
