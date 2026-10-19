# Dynamic schema migration

## Sub-features

- Load a client schema from JSON or YAML with `--config`.
- Reject undocumented fields and validate configured field types and limits.
- Guard fields with `field_rules` beside the schema config (see [Constraints for dynamic schemas](dynamic-constraints.md)).
- Store records in a schema-specific SQLite table.
- Pseudonymize declared string fields with `--anonymize`.

## How to get to it (user POV)

Run `python main.py --input PATH --config SCHEMA.json --output-dir DIR`. The checked-in real-data example is `examples\real_data\online_retail_sample.jsonl` with `examples\real_data\online_retail_schema.json`.

## Driving it with PowerShell

```powershell
$run = Join-Path $env:LOCALAPPDATA "Temp\opencode\airlock-db-real-data\run-$(Get-Date -Format yyyyMMdd-HHmmss)"
python main.py --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --anonymize --output-dir $run
$code = $LASTEXITCODE
if ($code -ne 0) { throw "Expected exit code 0 for the public sample, got $code" }
python .cursor\skills\verify-airlock-db\scripts\verify_migration_output.py $run --expected-checked 10 --expected-valid 10 --expected-invalid 0 --expected-rows 10 --evidence (Join-Path $run verification.json)
```

## Gotchas

- The sample comes from real UCI Online Retail records. Its customer ID column was omitted.
- The original invoice timestamp has no timezone. Keep it naive unless the source owner defines the timezone.
- The full UCI file is XLSX. AirLock DB does not ingest XLSX, so convert it to a supported format first.
