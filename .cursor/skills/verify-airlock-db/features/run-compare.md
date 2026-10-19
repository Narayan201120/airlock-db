# Run-to-run comparison

Covers `airlock-db report --output-dir DIR --compare OTHER_DIR`, which adds a delta section to the rebuilt report.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_run_compare.py ci-compare
```

The script runs two migrations over mutated sources (rejected rows fixed, a different issue category introduced, a third source added) and requires the stdout line and delta section to match the diff computed from the two summaries. It reruns the command for a byte-identical section, compares a run with itself for all zeros, and requires a missing baseline to exit 2 without rewriting the report.

Expected result: `run_compare_ok scenarios=7 evidence=...`.

## Rules

- The delta covers record-count changes, issue categories that appeared or disappeared, and per-source changes. It reads the two `airlock_summary.json` files only: no database reads and no new files.
- The rebuilt report stays self-contained and script-free.
- A category that did not exist before is shown as new, never as a percentage.
- A missing baseline exits 2 and leaves the existing report untouched.

## Manual check

```powershell
python main.py --input examples\messy_transactions.json --output-dir compare-a
python main.py --input examples\messy_transactions.json --output-dir compare-b
python main.py report --output-dir compare-b --compare compare-a
```

Both fixture runs exit 1 with rejected rows. The comparison reports all zeros because the two inputs are identical.
