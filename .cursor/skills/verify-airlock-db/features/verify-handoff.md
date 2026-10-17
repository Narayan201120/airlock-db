# Handoff verification

Covers `airlock-db verify --input PATH --output-dir DIR`, which reconciles a committed target database with the sources that produced it.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_handoff.py ci-handoff
```

The script runs real migrations, verifies each untouched target, then mutates copies: one changed stored value, one deleted row, one inserted row, one changed source file, and the wrong duplicate policy. It also covers an anonymized dynamic run, the built-in schema, a missing database, and a mismatched schema config.

Expected result: `verify_handoff_ok scenarios=10 evidence=...`.

## Rules

- The target database is opened read-only. The check never writes to it; it writes `airlock_verify.json` into the output directory.
- Digests come from the migration's own row builders, so a value that the migration would store differently is reported as changed instead of passing.
- Duplicate keys follow the recorded policy: `skip` keeps the first occurrence, `replace` keeps the last. The policy comes from `--conflict-policy` or from `airlock_summary.json`, and a mismatch between the two prints a warning.
- An anonymized run keeps its anonymized fields out of the digests, because the tokens use a run-scoped key that is never stored. Everything else in the row must still match.
- Exit code `0` means consistent; `1` means changed, missing, or unexpected rows, or a missing target database; `2` means the flags or files themselves are wrong.
- JSONL-only runs have no target database and are refused with a clear message.

## Manual check

```powershell
python main.py --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --anonymize --output-dir handoff-run
python main.py verify --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --output-dir handoff-run
```
