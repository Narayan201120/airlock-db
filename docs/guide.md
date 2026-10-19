# Guide

Workflow guides for the tasks around a migration. Every option, format, and limit lives in the [CLI reference](cli-reference.md); setup and the quick start are in the [README](../README.md).

## Repairing rejected rows

Rejected rows get a second pass. A migration can write a repair log of every rejected record, and the `repair` command reviews that log, revalidates corrections, and commits accepted fixes to SQLite.

### Create a repair log, then repair

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --error-log --output-dir output
python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json
```

After installing the package, the same command is available as `schema-sentry repair` (and `airlock-db repair`).

Do not add `--anonymize` to the migration if you intend to repair that log. Anonymized migrations omit rejected source values and redact issue details, so their entries are intentionally not repairable.

### Before any target is opened

For a dynamic layout, pass the same `--config` used for the migration. If the migration used business constraints, pass the same `--constraints` file. Schema and constraint fingerprints are checked before any target is opened, and a mismatch stops the session.

### The session

For each repairable row, the command displays its source, record number, rejected input, and Pydantic error details.

| Key | Action |
| --- | --- |
| `e` | Enter a complete corrected JSON object |
| `p` | Set one field to a JSON value. Quote text, such as `"UNKNOWN"` |
| `r` | Remove a field, such as an undocumented key |
| `d` | Drop the row |
| `q` | Finish the session |

Every edit is revalidated immediately. A correction that still fails shows its errors and returns to the prompt, so you can revise or drop the row.

Accepted corrections and drop decisions are staged while you review the log. SQLite commits the corrected rows and their progress checkpoints together after review. Ctrl+C rolls back that commit, and nothing partial lands in the database.

- The command accepts piped input as well as a terminal. EOF ends the session.
- The database defaults to `target_migration.db` beside the error log. Use `--output-db` to change it.
- The temporary staging file is capped at 256 MiB.

### Non-interactive repair

`--decisions decisions.jsonl` applies scripted decisions through the same validation, staging, and checkpoint pipeline as the interactive session, then commits. One JSON object per line:

```jsonl
{"entry": 1, "action": "drop"}
{"entry": 2, "action": "set", "field": "amount", "value": "5.00"}
{"entry": 3, "action": "remove", "field": "legacy_note"}
{"entry": 4, "action": "edit", "record": {"transaction_id": "...", "account_id": "...", "posted_at": "...", "amount": "1.00", "currency": "USD", "transaction_type": "DEBIT", "status": "POSTED", "counterparty": "Example"}}
```

Entries without decisions are left unhandled. Each line must use exactly one action: `drop`, `set`, `remove`, `edit`, or `skip`. `skip` is inert; it leaves the entry unchanged. Unknown actions, duplicate entries, and corrections that still fail validation exit with status 2 and commit nothing. Rerunning the same file resumes exactly like an interactive session.

### Draft the decision file from the log

`--emit-decisions decisions.jsonl` writes a starting file instead of reviewing interactively: one `{"entry": N, "action": "skip"}` line per repairable entry, in entry order. Because every line is inert, running the template as-is changes nothing and reports how many entries it skipped.

```powershell
python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json --emit-decisions output/decisions.jsonl
```

Edit the actions you want and rerun with `--decisions output/decisions.jsonl`. Entries below the log's raw-row limit have nothing to correct, so they are left out of the template and counted in the summary line. The flag refuses to replace an existing file unless `--overwrite` is passed, cannot be combined with `--decisions`, and exits with status 2 when there is nothing to decide, such as an anonymized log.

### Dry-run first

`--dry-run` requires `--decisions` and evaluates the file without writing anything: the database, the checkpoints, and the repair log stay untouched. It prints one line per decision (would correct, would drop, would skip, ignored, or correction still fails validation) and ends with a preview line:

```powershell
python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json --decisions output/decisions.jsonl --dry-run
# Preview run: 4 of 5 decisions would apply (2 correct, 1 drop, 1 skip); the database, the checkpoints, and the repair log were not touched.
```

Exit code `0` means every decision would apply, `1` means at least one would fail validation, and `2` means the file or the flags are wrong. Entries that a live run would ignore (already saved in SQLite, not repairable, or not present in the log) are reported as `ignored` and do not fail the run.

### Resume and checkpoints

If you stop with `q` and rerun the same log against the same database, completed corrections and drops are skipped. The session reports them as resumed.

- Checkpoints use SHA-256 fingerprints of the complete repair log and each decision.
- Progress is scoped to the conflict policy and anonymize mode.
- A concurrent session that submits a different decision for an already-handled entry is rolled back.

### A worked example

Drop the first two rejected fixture rows and finish before the third. Actions are answered one line at a time, and `q` ends the session and commits the staged decisions.

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --error-log --output-dir output
@('d','d','q') | python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json
```

Expected: `Repair complete: 0 corrected, 2 dropped, 0 non-repairable entries.` and a SQLite line ending in `2 repair decisions saved`. Rerun the second command with only `q` piped. The session reports `Resumed: 2` and changes nothing.

Where a row needs only one field corrected, use `p` instead of `d`. Quote text values, for example `p`, then `counterparty`, then `"UNKNOWN"`. See [the real-data notes](../examples/real_data/README.md) for a bank ledger whose rejects are all single-field fixes.

### Rebuild the report

Migration writes `airlock_report.html` once, and repair moves the database ahead of it. Rebuild the report at any time:

```powershell
python main.py report --output-dir output
```

The refreshed report rebuilds from `airlock_summary.json`, adds the current database row count, and shows a "Repair outcomes" section with log entries, corrected, dropped, and pending counts whenever a repair log exists.

### Repairing with --anonymize

When repairing a non-anonymized log, add `--anonymize` to pseudonymize accepted rows before storage and redact source labels, record previews, and validation details in the terminal. Repair pseudonyms use a new run key and will differ from pseudonyms created by any earlier migration.

## Moving an install into an air-gapped machine

An air-gapped machine has no package index. `scripts/check_wheelhouse.py` inspects a directory of wheels, walks the dependency closure from the local `airlock-db` wheel, and writes an install manifest. The target machine re-runs the checker with `--verify` before it installs, so a truncated copy or a changed byte stops the deployment.

The manifest is `install_manifest.json`, and it sits beside the wheels. It lists each filename, size, SHA-256 hash, and the distribution name and version read from the wheel metadata. Two runs over the same directory produce byte-identical manifests. The checker uses only the standard library, so it runs before anything is installed.

Replace `WHEELHOUSE` in every command below with the directory that holds the wheels.

### Stage on the connected machine

Build the local wheel, because `airlock-db` is not on PyPI, then download the dependencies as wheels:

```sh
python -m pip wheel --no-deps --wheel-dir WHEELHOUSE .
python -m pip download --only-binary=:all: --dest WHEELHOUSE pydantic "PyYAML>=6,<7"
```

`--only-binary=:all:` refuses source distributions, so the directory holds wheels only. Check the closure and write the manifest:

```sh
python scripts/check_wheelhouse.py WHEELHOUSE
```

Expected: `wheelhouse_ok wheels=N manifest=WHEELHOUSE/install_manifest.json`, where `N` counts the wheels.

The check fails with exit code 1 when a dependency is missing, when no wheel satisfies a `Requires-Dist` specifier, when a file is not a wheel, or when the local `airlock_db` wheel is absent. Each failure prints one `wheelhouse:` line, then a `wheelhouse_failed failures=N` token. Exit code 2 means the directory does not exist.

This is the only step that uses the network.

### Move the directory

Copy the whole directory to the air-gapped machine. Keep the wheels and `install_manifest.json` together, because the manifest records the checksums of the wheels beside it. Do not install anything on this machine.

### Verify and install on the air-gapped machine

Re-run the checker in verify mode:

```sh
python scripts/check_wheelhouse.py WHEELHOUSE --verify
```

Expected: the same token, with `verified=1` appended. A size, hash, filename, or package mismatch prints a `wheelhouse:` line and exits 1. Copy the directory again instead of repairing single files by hand.

Create a virtual environment and install from the local directory:

```sh
python -m venv airlock-venv
python -m pip install --no-index --find-links WHEELHOUSE airlock-db
```

Call the environment's own interpreter. On Windows that is `airlock-venv\Scripts\python`, and on Linux and macOS it is `airlock-venv/bin/python`. `--no-index` ignores every package index, and `--find-links` reads only the local directory, so a missing wheel fails the install instead of reaching out to the network.

### Run a migration

```sh
python -m airlock_db --input examples/messy_transactions.json --output-dir output
```

The console scripts run the same command: `airlock-db` and `schema-sentry`. The built-in fixture has invalid rows, so the run exits 1 and still writes `airlock_summary.json`, `airlock_report.html`, and `target_migration.db`.

### Match the target platform

Build the wheelhouse for the operating system and the Python series of the air-gapped machine. A wheel filename names its tags, such as `cp312-cp312-win_amd64`, and pip skips a wheel whose tags do not fit. The continuous integration job stages and installs on Python 3.12. `airlock-db` requires Python 3.10 or newer, so a 3.10 or 3.11 target needs its own wheelhouse from a matching interpreter.

`pydantic-core` and `PyYAML` ship compiled wheels per interpreter, system, and processor. Stage the closure again on a matching machine when the target differs.

### Checker reference

| Flag | Effect |
| --- | --- |
| `WHEELHOUSE` | Directory to scan. Required |
| `--manifest PATH` | Manifest location. Defaults to `WHEELHOUSE/install_manifest.json` |
| `--verify` | Compare against the manifest and write nothing |

Exit codes are `0` for a pass, `1` for a failed check, and `2` for a missing directory or an absent or unreadable manifest in verify mode.

The version comparison covers the operators `==`, `!=`, `<`, `<=`, `>`, `>=`, and `~=` on release numbers. It is not a full PEP 440 implementation. A requirement counts as required unless its marker mentions `extra`, so a wheelhouse this checker accepts holds at least what a default install needs. The manifest ignores itself during the scan, and every other non-wheel file fails the check.
