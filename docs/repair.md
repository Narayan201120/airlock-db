# Interactive repair

Rejected rows get a second pass. A migration can write a repair log of every rejected record, and the `repair` command reviews that log, revalidates corrections, and commits accepted fixes to SQLite.

## Create a repair log, then repair

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --error-log --output-dir output
python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json
```

After installing the package, the same command is available as `schema-sentry repair` (and `airlock-db repair`).

Do not add `--anonymize` to the migration if you intend to repair that log. Anonymized migrations omit rejected source values and redact issue details, so their entries are intentionally not repairable.

## Before any target is opened

For a dynamic layout, pass the same `--config` used for the migration. If the migration used business constraints, pass the same `--constraints` file. Schema and constraint fingerprints are checked before any target is opened, and a mismatch stops the session.

## The session

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

## Non-interactive repair

`--decisions decisions.jsonl` applies scripted decisions through the same validation, staging, and checkpoint pipeline as the interactive session, then commits. One JSON object per line:

```jsonl
{"entry": 1, "action": "drop"}
{"entry": 2, "action": "set", "field": "amount", "value": "5.00"}
{"entry": 3, "action": "remove", "field": "legacy_note"}
{"entry": 4, "action": "edit", "record": {"transaction_id": "...", "account_id": "...", "posted_at": "...", "amount": "1.00", "currency": "USD", "transaction_type": "DEBIT", "status": "POSTED", "counterparty": "Example"}}
```

Entries without decisions are left unhandled. Unknown actions, duplicate entries, and corrections that still fail validation exit with status 2 and commit nothing. Rerunning the same file resumes exactly like an interactive session.

## Resume and checkpoints

If you stop with `q` and rerun the same log against the same database, completed corrections and drops are skipped. The session reports them as resumed.

- Checkpoints use SHA-256 fingerprints of the complete repair log and each decision.
- Progress is scoped to the conflict policy and anonymize mode.
- A concurrent session that submits a different decision for an already-handled entry is rolled back.

## A worked example

Drop the first two rejected fixture rows and finish before the third. Actions are answered one line at a time, and `q` ends the session and commits the staged decisions.

```powershell
python main.py --input examples/messy_transactions.json --constraints examples/constraints.json --error-log --output-dir output
@('d','d','q') | python main.py repair --error-log output/airlock_errors.jsonl --constraints examples/constraints.json
```

Expected: `Repair complete: 0 corrected, 2 dropped, 0 non-repairable entries.` and a SQLite line ending in `2 repair decisions saved`. Rerun the second command with only `q` piped. The session reports `Resumed: 2` and changes nothing.

Where a row needs only one field corrected, use `p` instead of `d`. Quote text values, for example `p`, then `counterparty`, then `"UNKNOWN"`. See [the real-data notes](../examples/real_data/README.md) for a bank ledger whose rejects are all single-field fixes.

## Rebuild the report

Migration writes `airlock_report.html` once, and repair moves the database ahead of it. Rebuild the report at any time:

```powershell
python main.py report --output-dir output
```

The refreshed report rebuilds from `airlock_summary.json`, adds the current database row count, and shows a "Repair outcomes" section with log entries, corrected, dropped, and pending counts whenever a repair log exists.

## Repairing with --anonymize

When repairing a non-anonymized log, add `--anonymize` to pseudonymize accepted rows before storage and redact source labels, record previews, and validation details in the terminal. Repair pseudonyms use a new run key and will differ from pseudonyms created by any earlier migration.
