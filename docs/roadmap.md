# Roadmap

Current state: v0.3.0 shipped all of Tier 2, Tier 3, and Tier 4: report regeneration and scripted repair decisions, multi-table and CSV/TSV ingestion, resumable migrations, richer reports, compressed inputs, schema inference, target verification, preview runs and SQL source queries, and a memory soak at 1M rows. The GitHub Actions workflow runs the full end-to-end suite (fixture migration, real-data sample, JSONL target, CSV and archives, multi-table and custom-query source databases, schema inference, target verification, repair resume, scripted decisions with report refresh, preview runs, checkpoint/resume, XML equivalence, and a reduced memory soak) on every push, on Linux and Windows. Version tags build the wheel and sdist and attach them to the GitHub release. Tier 5 is planned for v0.4.0, and each item lands as its own verified commit.

## Ground rules

- Stay local-first. No new runtime dependencies. Stdlib or already-present packages only.
- Every feature lands with an end-to-end check wired into CI before its commit.
- The report stays a single self-contained file with no scripts or external assets.
- No network behavior, no telemetry.

## Tier 1: shipped in v0.1.1

### 1. Windows CI job

- What: run the verify workflow on `windows-latest` beside `ubuntu-latest`, using Git Bash as the shell on both.
- Why: the XML equivalence script already hit a Windows-only file-lock bug. Paths, URIs, and locking differ enough to deserve a gate.
- Verification: both jobs green on one push.

### 2. Release artifacts

- What: a workflow that builds the sdist and wheel on every `v*` tag and attaches them to the GitHub release.
- Why: air-gapped installs need a downloadable wheel without PyPI. The release page becomes the wheelhouse source.
- Scope: `python -m build`, then `gh release upload`. Needs `build` in the workflow only.
- Verification: cut v0.1.1 with a docs change, download the wheel from the release, install it in a clean venv with `--no-index`, run one migration.

### 3. Machine-readable run summary

- What: write `airlock_summary.json` next to the report. Counts, drift categories, storage stats, exit-relevant flags, and source labels. Anonymize mode redacts labels.
- Why: CI and dashboards can consume it without scraping HTML. It also becomes the input for item 4.
- Verification: the output verifier checks that summary counts match the report and database.

## Tier 2: shipped in v0.2.0

### 4. Report regeneration

- What: `airlock-db report --output-dir DIR` rebuilds `airlock_report.html` from `airlock_summary.json` plus the repair checkpoints, adding a repair-outcomes section.
- Why: the report freezes at migration time while repair moves the database ahead. Today there is no way to see the final state in one page.
- Depends on: item 3.
- Verification: end to end in CI. Migrate, repair, regenerate, checks pass over both runs.

### 5. Non-interactive repair

- What: `repair --decisions decisions.jsonl` applies scripted decisions (`set field`, `remove`, `drop`, `edit`) with the same validation, staging, and checkpoints as the interactive session.
- Why: air-gapped operations often need an auditable, repeatable batch fix rather than a live session.
- Scope: validate the decisions file up front, refuse unknown entries, keep the decision fingerprints and resume behavior identical.
- Verification: CI drives a decisions file, then resumes with `q` and asserts the resumed counts.

### 6. Multi-table source databases

- What: make `--source-table` repeatable so one `--source-db` run can validate several tables with the same schema.
- Why: legacy databases hold related tables; the one-table limit forces exports.
- Verification: CI builds a two-table database, migrates both, and checks per-table counts in the report.

## Tier 3: shipped in v0.2.0

### 7. CSV and TSV ingestion

- `--input file.csv` with delimiter options, BOM and encoding handling, and header-to-schema validation. Stdlib `csv` only.

### 8. Resumable large migrations

- An opt-in checkpoint mode that commits in batches and records progress, trading single-transaction atomicity for restartability on very large sources.

### 9. Report UX without scripts

- Per-source breakdown table and issue-category grouping using `<details>` only. No JavaScript, by design.

## Tier 4: shipped in v0.3.0

### 10. Compressed inputs

- What: read `.gz` files and `.zip` archives directly, streaming members through the same bounded readers. No extraction to disk and no nesting: one `.gz` layer over a supported suffix, or a flat zip of supported members.
- Why: air-gapped handoffs arrive as archives. Extracting them first writes unvalidated PII to disk.
- Scope: the same suffix list and size limits apply to the uncompressed content. Unsupported members are skipped, and a bad archive is a source error, not a crash.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_archive_input.py` builds gzip and zip twins of a checked fixture, migrates them, and requires stored rows and counts identical to the plain-file run. Wired into CI.

### 11. Schema inference command

- What: `airlock-db infer --input FILE` writes a draft dynamic-schema config from observed records. Inferred types are conservative, and the draft marks fields whose values conflict.
- Why: writing a client schema by hand is the slowest step for a new legacy layout.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_schema_infer.py` drafts a layout from the real-data sample and the messy fixture, migrates each source with its own draft, and requires the predicted outcome: zero invalid records for the clean sample, and failures only on the rows the draft flagged. Wired into CI.

### 12. Verification command

- What: `airlock-db verify --input ... --output-dir DIR` reconciles the committed target database against the records the run accepted: counts plus per-record digests.
- Why: operators need a handoff check that does not depend on the repository scripts.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_handoff.py` runs real migrations and requires an untouched target to verify, then requires changed, deleted, inserted, re-policied, and re-edited-source variants to fail with the matching counts. Wired into CI.

### 13. Small CLI wins

- What: `repair --emit-decisions` writes a decisions template for the error log, `--sample N` bounds a run for a quick previewing pass, and `--source-query SQL` reads a custom SELECT from a source database.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_cli_wins.py` drives all three through the real CLI: sampled runs stop at the requested count and mark themselves as previews, queries filter, verify, and are refused when malformed, and an emitted template stays inert until a line is edited. Wired into CI beside the repair and source-database steps.

### 14. Scale proof

- What: a scripted 1M-row soak run that records wall time and peak memory, plus a reduced variant in CI.
- Why: prove the streaming story at scale before the input surface grows again.
- Verification: `scripts/soak_migration.py` migrated 1,000,000 rows in 37 s at 26.8k records/s with a 40.7 MiB peak, flat from 20,000 rows (39.3 MiB) and 100,000 rows (40.9 MiB), while asserting stored rows and summary counts and holding a 300 MiB ceiling. CI runs the 100,000-row variant on Linux.

## Tier 5: planned (target v0.4.0)

### 15. Normalization rules in dynamic schema configs (shipped)

- What: an optional `transforms` list per field in JSON and YAML schema configs. Stdlib-only rules run in listed order before validation: trim whitespace, fold case, parse a datetime with an explicit strptime pattern into ISO 8601, normalize decimal text with a places bound, and map enum aliases to canonical values. A value a rule cannot convert is rejected with a `transform_failed` reason that names the field and the rule.
- Why: legacy exports carry padded, mixed-case, and locale-formatted values that are fixable on the spot. Today a `31/12/2025` date rejects the row and pushes it into per-row repair.
- Scope: dynamic schema configs only; the built-in financial model keeps its current strictness. Transforms feed the schema fingerprint and `verify` re-applies them, so digests stay comparable. The summary gains a normalized-fields count.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_transforms.py` migrates a dirty fixture with padded text, European dates and decimals, and enum aliases, and requires the exact normalized stored values, two `transform_failed` rejections naming the field and the rule, a passing `verify` and a failing tampered one, distinct fingerprints for alias variants, three fail-fast config errors, and a repair session that normalizes corrected values the same way (7 scenarios). Wired into CI.

### 16. Fixed-width positional input (shipped)

- What: `--fixed-width PATH` points to a small JSON or YAML layout: an encoding, an optional skip count for banner lines, and one entry per field with a 1-based start and a length. Values stream through the same validation as every other reader. Overlapping spans, out-of-order spans, and duplicate names fail before the run starts.
- Why: mainframe and Cobol handoffs arrive as undelimited text. Today they are rejected as unsupported files.
- Scope: one record layout per file, no record-type branching. A short line becomes a per-row `fixed_width_short_line` issue instead of a crash. The flag routes archive members and directory scans, and it threads through `verify` and `infer`.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_fixed_width.py` migrates a fixed-width twin of a delimited fixture and requires identical stored rows and counts (3/3/0), a passing `verify` and a failing one without the flag, identical records through a directory scan, a gzip layer, and a zip member whose `.dat` suffix is otherwise unsupported, a cp1252 layout that keeps a `Müller & Söhne GmbH` counterparty while an all-padding span becomes null, one short line in a three-row file rejecting exactly that row with `fixed_width_short_line`, three fail-fast layout errors that write no summary, and an `infer` draft that then migrates the same file (9 scenarios). Wired into CI.

### 17. Verification for JSONL targets (shipped)

- What: `verify` accepts a JSONL-only run. When the database is absent and `target_migration.jsonl` is present, accepted records are rebuilt from the sources exactly as the SQLite path does and compared per record against the file, which is read only.
- Why: JSONL is a full handoff format here, and it is the one output `verify` refuses today.
- Scope: the same duplicate and conflict-policy semantics as the SQLite path, the same exit codes, and an `airlock_verify.json` whose `target` block names the checked file. No sidecar digests; the JSONL rows stay canonical.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_jsonl_target.py` migrates a fixture to a JSONL-only target and requires the untouched file to verify (4 matched, exit 0) before requiring an altered row (changed 1), a deleted row (missing 1), an appended row (unexpected 1), and a corrupt line (exit 1 with its line number) to fail with exactly those counts; a duplicate-id fixture proves the recorded policy with altered and deleted duplicates landing differently under `skip` and `replace`; anonymized fields stay out of the digests while a changed business field still fails; a two-target run verifies its database; and a missing target fails with `nothing to verify`. The report's `target` block names the checked file (10 scenarios). Wired into CI.

### 18. Audit manifest in the run summary (shipped)

- What: `airlock_summary.json` records a `source_manifest` with one entry per expanded source file: label, size, and SHA-256, beside the version and schema fingerprint it already carries. `verify` re-reads each source, hashes it in bounded chunks, and compares size and digest against the manifest.
- Why: a handoff should prove which bytes the run validated. A swapped file of the same shape passes `verify` silently today.
- Scope: each source file gets one streaming hash read in bounded chunks, beside the read the run already does; memory stays flat regardless of file size. Anonymized runs keep labels only. A summary without a manifest keeps verifying with a notice.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_source_manifest.py` requires each recorded entry to match an independent SHA-256 and the file's size, and requires a same-size swapped source to fail `verify` with the file named while every row still matches. A deleted source and an added file land in the manifest report, an untouched copy passes, an old summary passes with a skip notice, and an anonymized run keeps labels only (9 scenarios). Wired into CI.

### 19. Decisions dry-run (shipped)

- What: `repair --decisions FILE --dry-run` parses and evaluates the file, prints one line per entry with the planned outcome, and exits without writing to the database, the checkpoints, or the repair log. It requires `--decisions`.
- Why: scripted fixes are all or nothing today. The first staged change opens the store and commits, so a typo in one line writes the other rows first.
- Scope: the same parsing, validation, and fingerprint checks as a live run. Exit 0 when every entry would apply, 1 when an entry would fail validation, 2 for parse errors and log mismatches. Output follows the `--sample` notice convention.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_decisions_dry_run.py` requires a dry-run to leave the database, the checkpoints, and the repair log byte-identical while predicting one outcome per entry (2 correct, 1 drop, 1 skip, and an unused entry ignored), applies the same file for real and requires the predicted counts (2 corrected, 1 dropped, 3 decisions saved, 2 rows added), reports the saved entries as ignored on a second dry-run, and requires a decision that would fail validation (exit 1) and parse errors and a missing `--decisions` (exit 2) to write nothing (5 scenarios). Wired into CI.

### 20. Input encoding override (shipped)

- What: `--input-encoding NAME` sets the text encoding for readers that currently hard-code `utf-8-sig`: the JSON reader, the JSONL byte parser, and the line-based TSV and header readers. `--csv-encoding` remains for CSV.
- Why: air-gapped exports are frequently cp1252 or latin-1. Today they fail with an `encoding_error` even though the data is readable.
- Scope: XML stays declaration-driven by design, and fixed-width layouts keep the encoding from their layout file. The flag threads through `verify` and `infer` beside the existing CSV options.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_input_encoding.py` requires every cp1252 fixture (JSONL, JSON array, TSV, CSV, and a gz member) to fail without the flag and migrate clean with it, the decoded text to survive into SQLite, CSV to keep `--csv-encoding` while accepting `--input-encoding`, a UTF-8-with-BOM file to read without flags, `verify` and `infer` to honor the flag, and unknown names to exit 2 or 1 (9 scenarios). Wired into CI.

### 21. Run-to-run comparison (shipped)

- What: `airlock-db report --output-dir DIR --compare OTHER_DIR` adds a delta section to the rebuilt report: record-count changes, issue categories that appeared or disappeared, and per-source changes. It reads the two `airlock_summary.json` files only.
- Why: after a repair pass the interesting number is what changed since the previous run, not the absolute count.
- Scope: no database reads, no new files, one self-contained report. A category that did not exist before is shown as new, not as a percentage.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_run_compare.py` runs two migrations over mutated sources (rejected rows fixed, a different issue category introduced, a third source added) and requires the stdout line and the delta section to match the diff computed from the two summaries. It runs the command twice for a byte-identical section, requires a missing baseline to exit 2 without rewriting the report, requires a run compared with itself to report all zeros, and re-checks that the report stays script-free and self-contained (7 scenarios). Wired into CI.

### 22. Constraints for dynamic schemas (shipped)

- What: a `field_rules` block in the constraints file extends business rules beyond the built-in model: per-field enum sets, numeric ranges, regex patterns, and required-any-of groups. Rules run after validation on dynamic records, each with its own reason: `constraint_enum`, `constraint_range`, `constraint_pattern`, `constraint_required_any_of`.
- Why: client layouts deserve the same guards the built-in model gets. Today `--constraints` refuses anything but the built-in schema.
- Scope: declarative JSON only; the built-in keys stay as they are, and `field_rules` apply to the built-in model and to dynamic ones. The fingerprint covers the full parsed file in canonical key order, so repair resume keeps working; an empty rule set leaves every existing digest unchanged.
- Verification: `.cursor/skills/verify-airlock-db/scripts/verify_dynamic_constraints.py` runs a dynamic migration with the rules and requires exactly the predicted rejections per reason (2 enum, 2 range, 1 pattern, 1 required-any-of) with the exact messages, a clean run over valid rows, absent/null/empty-string edge cases, six bad rules files (including an entry that mixes `required_any_of` with field keys) that all stop the run with exit 2, the combination rule at migration, repair, and verify, a repair session that accepts the same fingerprint and rejects a variant, and verify with and without the rules file (7 scenarios). Wired into CI.

### 23. Air-gap deployment kit

- What: `scripts/check_wheelhouse.py` inspects an offline wheelhouse directory and confirms it covers `airlock-db` and its dependencies from wheel filenames and metadata, then writes an install manifest with sizes and hashes. `docs/deployment.md` documents the two-machine transfer: stage on a connected machine, verify, move, install with `--no-index`.
- Why: the install story is one README paragraph today. A kit that is checked before the jump and written down removes the guesswork.
- Scope: the checker is fully offline and reads the directory only. The single network step is the documented, user-run `pip download` on the connected side.
- Verification: a CI step builds a wheelhouse with `pip download`, runs the checker, installs from it into a clean venv with `--no-index`, and runs one migration. Ubuntu-only, mirroring the soak step. Wired into CI.

## Explicitly out of scope

- XLSX ingestion. The dependency cost is too high; the preparation-script pattern covers it.
- A unit-test suite. The project rule is end-to-end verification, and CI already runs it.
- Any cloud, sync, or telemetry feature.

## Sequencing

Items 1 and 2 are small and pay off immediately. Item 3 unlocks item 4, which closes the repair-versus-report gap. Items 5 and 6 deepen the two operational stories: repair and legacy databases. Tier 5 runs in the picked order: normalization rules, fixed-width input, JSONL verification, the audit manifest, and decisions dry-run first; then the input encoding override, run comparison, dynamic constraints, and the deployment kit. Each item stands alone and lands as its own verified commit.
