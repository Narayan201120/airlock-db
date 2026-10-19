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

The public-data path is documented in the [CLI reference](../../../docs/cli-reference.md#dynamic-schema-configuration). It uses the real UCI Online Retail sample and its client schema.

## Evidence

Keep `target_migration.db`, `airlock_report.html`, and `verification.json` in the run directory. The helper checks the SQLite integrity, row count, report chart, and absence of scripts or external assets. When asked to verify anonymization, pass `--anonymized-field` once per field. The helper checks the token prefix and length used by the built-in or dynamic schema. Add `--expect-type-mismatch-note` when the run includes a type mismatch; the helper then requires the no-conversion note in the report. For JSONL-only runs, add `--skip-sqlite` and `--expected-jsonl-rows N`; the helper then parses every JSONL line instead of opening a database. The helper also requires `airlock_summary.json` and checks its counts against the report. Pass `--allow-missing-summary` only for artifacts from older versions.

The command must exercise the real CLI. Do not replace it with direct calls to internal pipeline functions. A passing import or compile check is not enough.

## Cleanup

There is no long-running process to stop. Keep successful output directories as evidence. Remove only failed scratch runs after recording the failure. Do not delete the source examples or evidence as part of cleanup.

## Helpers

`scripts/verify_migration_output.py` reads a completed output directory in read-only mode. Use the invocation in the Drive section. It does not modify the database or report.

`scripts/verify_xml_equivalence.py INPUT [--constraints PATH | --config PATH]` migrates the same accepted records through the JSON and XML paths in temporary directories and compares every stored column. Run it whenever the XML reader changes. Expected output line: `xml_equals_json=true`.

`scripts/verify_archive_input.py [WORKDIR]` builds gzip and zip twins of a JSONL fixture and requires stored rows and counts identical to the plain run. Run it whenever input discovery or a reader changes. Expected token: `archive_input_ok scenarios=N`.

`scripts/verify_jsonl_reader.py [WORKDIR]` covers the reader edges the standard fixtures miss: LF, CRLF, and bare-CR line endings, blank lines, records split across chunk boundaries, oversized records, invalid UTF-8, duplicate JSON keys, and CSV rows with empty, short, or extra values, down to the stored row. Run it whenever a JSONL or CSV parsing path changes. Expected token: `jsonl_csv_reader_ok checks=N`.

`scripts/verify_schema_infer.py [WORKDIR]` drafts a layout from the example fixtures with `airlock-db infer`, then migrates each source with its own draft. The clean sample must pass with zero invalid records; the messy fixture must fail on exactly the rows its flagged conflicts predict. Run it whenever inference, the readers, or the dynamic schema change. Expected token: `schema_infer_ok scenarios=N`.

`scripts/verify_handoff.py [WORKDIR]` runs real migrations and then reconciles each committed target with its sources through `airlock-db verify`. The untouched target must be consistent; changed, deleted, inserted, re-policied, and re-edited-source variants must each fail with the matching counts. Run it whenever `verify`, the row builders, the readers, or the storage layout change. Expected token: `verify_handoff_ok scenarios=N`.

`scripts/verify_jsonl_target.py [WORKDIR]` migrates fixtures to a JSONL-only target and requires the untouched file to verify, then requires altered, deleted, appended, and corrupt rows to fail with exact counts, both conflict policies to pick the matching duplicate, an anonymized run to exclude its token fields while a changed business field still fails, a two-target run to verify its database, and a missing target to fail with a clear message. Run it whenever `verify`, the JSONL writer, the row builders, or the readers change. Expected token: `jsonl_target_ok scenarios=N`.

`scripts/verify_source_manifest.py [WORKDIR]` requires every recorded manifest entry to match an independent SHA-256 and the file's size, and requires a same-size swapped source to fail with the file named while every row still matches. A deleted source and an added file must land in the manifest report, an untouched copy and an old summary must pass, and an anonymized run must keep labels only. Run it whenever the manifest, `verify`, the readers, or the summary shape change. Expected token: `source_manifest_ok scenarios=N`.

`scripts/verify_decisions_dry_run.py [WORKDIR]` builds a migration with four rejected rows, requires a dry-run to leave the database, the checkpoints, and the repair log byte-identical while printing one planned outcome per entry, then applies the same file for real and requires the predicted counts to match. A decision that would fail validation exits 1 without writing, parse errors and a missing `--decisions` exit 2, an unused entry is ignored, and a second dry-run reports the entries the live run saved. Run it whenever `repair`, the decisions parser, or the repair progress checkpoints change. Expected token: `decisions_dry_run_ok scenarios=N`.

`scripts/verify_input_encoding.py [WORKDIR]` requires every cp1252 fixture (JSONL, JSON array, TSV, CSV, and a gz member) to fail without `--input-encoding` and read clean with it, with the decoded text intact in SQLite. CSV keeps `--csv-encoding` and also accepts the fallback, a UTF-8-with-BOM file still reads without flags, `verify` and `infer` honor the flag, and unknown names exit 2 (migration and verify) or 1 (infer). Run it whenever a reader, the encoding plumbing, or a subcommand's reader flags change. Expected token: `input_encoding_ok scenarios=N`.

`scripts/verify_run_compare.py [WORKDIR]` runs two migrations over mutated sources, then requires `report --compare` to reconcile them: the stdout line and the delta section must equal the diff computed from the two summaries, including a new issue category, a resolved one, and per-source changes. It runs the command twice for a byte-identical section, requires a missing baseline to exit 2 without rewriting the report, requires a run compared with itself to report all zeros, and re-checks that the report stays script-free and self-contained. Run it whenever the report, the run summary schema, or the report refresh path changes. Expected token: `run_compare_ok scenarios=N`.

`scripts/verify_dynamic_constraints.py [WORKDIR]` migrates a dynamic schema with a `field_rules` file and requires exactly the predicted rejections per reason (`constraint_enum`, `constraint_range`, `constraint_pattern`, `constraint_required_any_of`) with their exact messages, a clean run over valid rows, and the absent/null/empty-string edge cases. It requires six bad rules files and the mixed `required_any_of` entry to stop the run with exit 2, requires `--constraints` plus `--config` to stay refused when the file carries built-in keys at migration, repair, and verify, and requires a repair session to accept the same fingerprint and reject a variant. Run it whenever the constraints loader, the constraints fingerprint, or the validation path changes. Expected token: `dynamic_constraints_ok scenarios=N`.

`scripts/verify_cli_wins.py [WORKDIR]` exercises `--sample`, `--source-query`, and `repair --emit-decisions` against the real CLI. Sampled runs must stop at the requested count and mark themselves as previews; queries must filter, stay verifiable, and be refused when malformed; an emitted decisions template must be inert until a line is edited. Run it whenever the migration loop, the database reader, or the repair decisions parser change. Expected token: `cli_wins_ok scenarios=N`.

`scripts/verify_transforms.py [WORKDIR]` migrates a dirty fixture through per-field `transforms` (trim, case folding, datetime, decimal, alias) and requires the exact normalized stored values, the `transform_failed` rejections, a passing `verify`, distinct fingerprints for rule variants, fail-fast config errors, and a repair session that normalizes corrected values the same way. Run it whenever transforms, the schema config, the repair validation path, or `verify` change. Expected token: `transforms_ok scenarios=N`.

`scripts/verify_fixed_width.py [WORKDIR]` builds a fixed-width twin of a delimited fixture, migrates both with the built-in schema, and requires identical stored rows and counts, then requires a passing `verify` (and a failing one without the flag), identical records through a directory scan, a gzip layer, and a zip member with an otherwise unsupported suffix, a cp1252 layout that keeps non-ASCII text while an all-padding span becomes null, one short line rejecting exactly that row with `fixed_width_short_line`, fail-fast layout errors that write no summary, and an `infer` draft that then migrates the same file. Run it whenever the fixed-width reader, the layout loader, input discovery, or `verify`/`infer` change. Expected token: `fixed_width_ok scenarios=N`.

`scripts/soak_migration.py --rows N [--max-peak-rss-mib M] [WORKDIR]` is the scale check, not an end-to-end check: it generates a synthetic JSONL source, migrates it with the real CLI, and asserts the stored rows, the summary counts, and the peak memory of the migration process against a ceiling. CI runs the reduced 100,000-row variant; the 1,000,000-row soak is a local command. Run it whenever the streaming readers, the store batching, or the pipeline change. Expected token: `soak_ok rows=N runs=N seconds=N peak_rss_mib=N`.
