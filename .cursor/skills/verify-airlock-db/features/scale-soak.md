# Scale soak

Covers `scripts/soak_migration.py`, the check that proves memory stays flat as the row count grows.

## Drive

Reduced variant (what CI runs on Linux):

```powershell
python scripts\soak_migration.py --rows 100000 --max-peak-rss-mib 300 ci-soak
```

Full soak (local, about two minutes including fixture generation):

```powershell
python scripts\soak_migration.py --rows 1000000 --max-peak-rss-mib 300 ci-soak-1m
```

Expected result: `soak_ok rows=... runs=1 seconds=... peak_rss_mib=... evidence=...` and `soak_verification.json` in the work directory.

## Rules

- The run must end with exit code `0`, exactly one managed `transactions` table, and a row count equal to `--rows`.
- The run summary must agree: `records.checked` and `records.valid` equal `--rows`, `records.invalid` and `records.file_errors` are `0`, and storage reports `status: committed` with `inserted` equal to `--rows`.
- The peak RSS must stay under `--max-peak-rss-mib` when a platform reader is available. Peak RSS is exact with a single run on Linux and macOS (`getrusage`); Windows and multi-run cases sample the live process every 50 ms; a platform with no reader records `null` and does not fail the ceiling check.
- Flatness is the point: the recorded peaks were 39.3 MiB at 20,000 rows, 40.9 MiB at 100,000 rows, and 40.7 MiB at 1,000,000 rows, at about 27k records/s. A large increase at higher row counts means a buffer is accumulating records.

## Manual check

```powershell
python scripts\soak_migration.py --rows 100000 --max-peak-rss-mib 300 soak-run
python -c "import json; print(json.load(open('soak-run/soak_verification.json'))['status'])"
```
