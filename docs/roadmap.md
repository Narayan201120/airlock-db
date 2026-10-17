# Roadmap

Current state: v0.2.0 shipped all of Tier 2 and Tier 3: report regeneration and scripted repair decisions, multi-table and CSV/TSV ingestion, resumable migrations, and richer reports. The GitHub Actions workflow runs the full end-to-end suite (fixture migration, real-data sample, JSONL target, CSV, multi-table source database, repair resume, scripted decisions with report refresh, XML equivalence, source-database ingestion, checkpoint/resume) on every push, on Linux and Windows. Version tags build the wheel and sdist and attach them to the GitHub release. Tier 4 is in progress; each item lands as its own verified commit.

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

## Tier 4: in progress (target v0.3.0)

### 10. Compressed inputs

- What: read `.gz` files and `.zip` archives directly, streaming members through the same bounded readers. No extraction to disk and no nesting: one `.gz` layer over a supported suffix, or a flat zip of supported members.
- Why: air-gapped handoffs arrive as archives. Extracting them first writes unvalidated PII to disk.
- Scope: the same suffix list and size limits apply to the uncompressed content. Unsupported members are skipped, and a bad archive is a source error, not a crash.
- Verification: `scripts/verify_archive_input.py` builds gzip and zip twins of a checked fixture, migrates them, and requires stored rows and counts identical to the plain-file run. Wired into CI.

### 11. Schema inference command

- What: `airlock-db infer --input FILE` writes a draft dynamic-schema config from observed records. Inferred types are conservative, and the draft marks fields whose values conflict.
- Why: writing a client schema by hand is the slowest step for a new legacy layout.
- Verification: infer a draft from the messy fixture, migrate with that draft, and require zero drift for the fields the draft claims.

### 12. Verification command

- What: `airlock-db verify --input ... --output-dir DIR` reconciles the committed target database against the records the run accepted: counts plus per-record digests.
- Why: operators need a handoff check that does not depend on the repository scripts.
- Verification: tampered rows and tampered inputs must each fail with a clear message.

### 13. Small CLI wins

- What: `repair --emit-decisions` writes a decisions template for the error log, `--sample N` bounds a run for a quick previewing pass, and `--source-query SQL` reads a custom SELECT from a source database.
- Verification: each option gets an end-to-end check in CI beside the existing repair and source-database steps.

### 14. Scale proof

- What: a scripted 1M-row soak run that records wall time and peak memory, plus a reduced variant in CI.
- Why: prove the streaming story at scale before the input surface grows again.
- Verification: the soak script asserts stored rows, summary counts, and a memory ceiling; CI runs the reduced variant.

## Explicitly out of scope

- XLSX ingestion. The dependency cost is too high; the preparation-script pattern covers it.
- A unit-test suite. The project rule is end-to-end verification, and CI already runs it.
- Any cloud, sync, or telemetry feature.

## Sequencing

Items 1 and 2 are small and pay off immediately. Item 3 unlocks item 4, which closes the repair-versus-report gap. Items 5 and 6 deepen the two operational stories: repair and legacy databases. Tier 3 items start only when a concrete use case arrives.
