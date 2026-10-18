# Rejected-row repair

## Sub-features

- Create an opt-in JSONL sidecar of rejected records with `--error-log`.
- Review and edit rejected records through the `repair` subcommand.
- Revalidate corrections before committing them to SQLite.
- Apply scripted decisions non-interactively with `--decisions decisions.jsonl`.
- Preview a decisions file without writing with `--decisions ... --dry-run`.
- Rebuild the report after repair with `python main.py report --output-dir DIR`.

## How to get to it (user POV)

Run a migration with `--error-log`, then call `python main.py repair --error-log PATH`. Pass the same constraints or schema configuration used for the migration.

## Driving it with PowerShell

Create a repairable log first. Do not add `--anonymize`; anonymized logs omit rejected rows and cannot be repaired.

```powershell
$run = Join-Path $env:LOCALAPPDATA "Temp\opencode\airlock-db-verify\repair-run-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Force -Path $run | Out-Null
python main.py --input C:\data\moneydata.jsonl --error-log --output-dir $run
# Expect exit code 1. The log is $run\airlock_errors.jsonl with 12 rejected rows.
```

Drive the review with piped answers. This session fixes the first entry, drops the second, and finishes before the third.

```powershell
@('p','counterparty','"UNKNOWN"','d','q') | python main.py repair --error-log (Join-Path $run 'airlock_errors.jsonl')
# Expect "Repair complete: 1 corrected, 1 dropped, 0 non-repairable entries."
# Expect a SQLite line with "1 inserted ... 2 repair decisions saved".
```

The corrected row is committed with `counterparty` set to `UNKNOWN`; the dropped row is absent; the database holds 6,556 rows. Rerun with only `q` to prove the checkpoint behavior. Expect `Resumed: 2 correction or drop decisions already saved in SQLite.` and `SQLite: no new database changes`.

## Gotchas

- A non-anonymized sidecar can contain source values. Protect it like the input file.
- An anonymized migration omits rejected source values, so those entries cannot be repaired from the sidecar.
- `--dry-run` writes nothing and applies the same parsing, validation, and fingerprint checks as a live run. It exits 0 when every decision would apply, 1 when one would fail validation, and 2 for parse errors or log mismatches.
- Corrections and progress checkpoints commit together. Re-running a completed repair log skips handled rows.
