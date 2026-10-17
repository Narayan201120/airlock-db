---
name: verify-airlock-db
description: Verify the AirLock DB CLI, migrations, SQLite output, and offline report after code changes.
---

# Verify AirLock DB

## Surface

AirLock DB is a short-lived command-line tool. It has no server to keep running. Its main user path reads JSON, JSONL, NDJSON, or XML, validates each record, writes accepted records to SQLite, and creates an HTML report.

## Launch

From the repository root, run `python main.py --help`. Each migration command starts a new process and exits when it finishes. Do not start multiple runs against the same output directory.

## Doctor

Run these read-only checks from the repository root:

```powershell
python --version
python -c "import pydantic, yaml; print('pydantic', pydantic.__version__); print('PyYAML', yaml.__version__)"
python main.py --version
```

Python must be 3.10 or newer. Pydantic must be version 2, and PyYAML must be installed.

## Drive

Run the built-in migration against the intentionally messy example in a fresh temporary output directory:

```powershell
$run = Join-Path $env:LOCALAPPDATA "Temp\opencode\airlock-db-verify\run-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Force -Path $run | Out-Null
python main.py --input examples\messy_transactions.json --constraints examples\constraints.json --anonymize --output-dir $run
$code = $LASTEXITCODE
if ($code -ne 1) { throw "Expected exit code 1 for the fixture's rejected rows, got $code" }
python .cursor\skills\verify-airlock-db\scripts\verify_migration_output.py $run --expected-checked 32 --expected-valid 8 --expected-invalid 24 --expected-rows 8 --anonymized-field account_id --expect-type-mismatch-note --evidence (Join-Path $run verification.json)
```

Exit code `1` is expected for this fixture. It contains 24 invalid records. The verifier must confirm that the CLI still writes the 8 valid records and the report.

The public-data path is documented in [features/dynamic-schema.md](features/dynamic-schema.md). It uses the real UCI Online Retail sample and its client schema.

## Evidence

Keep `target_migration.db`, `airlock_report.html`, and `verification.json` in the run directory. The helper checks the SQLite integrity, row count, report chart, and absence of scripts or external assets. When asked to verify anonymization, pass `--anonymized-field` once per field. The helper checks the token prefix and length used by the built-in or dynamic schema. Add `--expect-type-mismatch-note` when the run includes a type mismatch; the helper then requires the no-conversion note in the report.

The command must exercise the real CLI. Do not replace it with direct calls to internal pipeline functions. A passing import or compile check is not enough.

## Cleanup

There is no long-running process to stop. Keep successful output directories as evidence. Remove only failed scratch runs after recording the failure. Do not delete the source examples or evidence as part of cleanup.

## Helpers

`scripts/verify_migration_output.py` reads a completed output directory in read-only mode. Use the invocation in the Drive section. It does not modify the database or report.
