# Built-in transaction migration

## Sub-features

- Read JSON, JSONL, NDJSON, or XML sources.
- Apply the built-in financial transaction model and optional business constraints.
- Write accepted records to SQLite and a standalone HTML report.
- Optionally pseudonymize selected values with `--anonymize`.

## How to get to it (user POV)

Run `python main.py --input PATH --output-dir DIR`. Add `--constraints examples\constraints.json` to apply the bundled example rules.

## Driving it with PowerShell

Use the built-in fixture and the verifier in [the parent skill](../SKILL.md). It checks the actual CLI output in a new temp directory. The fixture should yield 32 checked records, 8 valid rows, 24 invalid rows, and a committed SQLite database with 8 rows.

For real bank rows, follow the MoneyData conversion instructions in [the public-data notes](../../../../examples/real_data/README.md). The full acceptance run checked 6,567 records, committed 6,555, and rejected 12 whose source descriptions were numeric rather than text. The report states that no automatic conversion was applied. The adapter's account alias, GBP currency, `POSTED` status, and midnight UTC timestamp are test conventions, not fields present in the source.

XML is covered by an equivalence check. Wrap 10 JSONL records in `<transaction>` elements, run the XML file and the JSONL file into separate output directories, then compare every stored column of both `transactions` tables. The tables must match exactly, and both runs must report 10 of 10 records valid.

## Gotchas

- Exit code `1` means the run completed with validation or source issues. Check the report and database before treating it as a command failure.
- Do not reuse an output directory unless duplicate handling is what you intend to verify.
- Anonymized tokens use a new key for each run, so token values differ between runs.
