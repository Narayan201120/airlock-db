# Built-in transaction migration

## Sub-features

- Read JSON, JSONL, NDJSON, or XML sources, or rows from a local SQLite database with `--source-db`.
- Apply the built-in financial transaction model and optional business constraints.
- Write accepted records to SQLite, a JSONL migration file, or both.
- Optionally pseudonymize selected values with `--anonymize`.

## How to get to it (user POV)

Run `python main.py --input PATH --output-dir DIR`. Add `--constraints examples\constraints.json` to apply the bundled example rules.

## Driving it with PowerShell

Use the built-in fixture and the verifier in [the parent skill](../SKILL.md). It checks the actual CLI output in a new temp directory. The fixture should yield 32 checked records, 8 valid rows, 24 invalid rows, and a committed SQLite database with 8 rows.

Source database ingestion: build a legacy SQLite table whose columns match the schema, run `--source-db legacy.db`, and expect the same counts as the equivalent file input. A database with several tables and no `--source-table` must produce a `source_db_table_required` source error and zero checked records.

JSONL output: run with `--output-format jsonl` and verify with `--skip-sqlite --expected-jsonl-rows N`. The run must not write `target_migration.db`, and every JSONL line must parse.

For real bank rows, follow the MoneyData conversion instructions in [the public-data notes](../../../../examples/real_data/README.md). The full acceptance run checked 6,567 records, committed 6,555, and rejected 12 whose source descriptions were numeric rather than text. The report states that no automatic conversion was applied. The adapter's account alias, GBP currency, `POSTED` status, and midnight UTC timestamp are test conventions, not fields present in the source.

XML ingestion is covered by `scripts/verify_xml_equivalence.py`. It migrates the input through the JSON path, re-serializes every stored row as an XML record, migrates that file, and requires identical tables:

```powershell
python .cursor/skills/verify-airlock-db/scripts/verify_xml_equivalence.py examples/messy_transactions.json --constraints examples/constraints.json
```

## Gotchas

- Exit code `1` means the run completed with validation or source issues. Check the report and database before treating it as a command failure.
- Do not reuse an output directory unless duplicate handling is what you intend to verify.
- Anonymized tokens use a new key for each run, so token values differ between runs.
