# CLI wins: previews, source queries, and decisions templates

Covers `--sample N`, `--source-query SQL`, and `repair --emit-decisions`.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_cli_wins.py ci-cli-wins
```

The script runs the real CLI: a sampled clean run, a limit larger than the input, a sampled run with an error log handed to `repair`, an emitted template run as-is and then with one edited line, a filtered SELECT that is migrated and verified, every refusal path, a query that fails at runtime, and a query with duplicate column names.

Expected result: `cli_wins_ok scenarios=11 evidence=...`.

## Rules

- A sampled run stops once N accepted records are written. The target, the report, and `airlock_summary.json` describe only the scanned prefix; `records.sample_limit` and `records.sample_reached` carry the preview state, and the HTML report shows a preview notice. A limit larger than the input leaves `sample_reached` false and adds no notice.
- A repair log written by a sampled run is marked incomplete, and `repair` prints the "did not finish" warning for it.
- `--source-query` takes exactly one single-statement SELECT (or `WITH ... SELECT`) per `--source-db`. Other statements, embedded semicolons, a query without its database, a query combined with `--source-table`, or a query count that does not match the database count exit with status 2 before any run starts.
- Query rows stream under a `(query)` label and pass the same validation as table rows. A query that fails at runtime is a `source_db_error` issue with exit code 1, and duplicate column names are `source_db_query_columns`, because one row object cannot hold both values.
- A run made with `--source-query` stays verifiable: `verify` takes the same flag and must report a consistent target.
- `--emit-decisions` writes one inert `{"entry": N, "action": "skip"}` line per repairable entry. Running the template as-is changes nothing and reports the skipped count. Editing one line applies exactly that decision. The flag refuses to clobber unless `--overwrite` is passed, cannot be combined with `--decisions`, and exits with status 2 when the log has no repairable entries (for example an anonymized log).

## Manual check

```powershell
python main.py --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --sample 4 --output-dir sample-run
python main.py --input examples\messy_transactions.json --constraints examples\constraints.json --error-log --output-dir repair-run
python main.py repair --error-log repair-run\airlock_errors.jsonl --constraints examples\constraints.json --emit-decisions decisions.jsonl
```
