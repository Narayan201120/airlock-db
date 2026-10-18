# Fixed-width input

Covers `--fixed-width PATH`, which reads undelimited positional text through a JSON or YAML layout of named column spans.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_fixed_width.py ci-fixed-width
```

The script builds a fixed-width twin of a delimited fixture, migrates both, and compares stored rows and counts. It then checks the same records through a directory scan, a gzip file, and a zip member, a cp1252 layout, one short line, three bad layouts, and an `infer` draft of the same file.

Expected result: `fixed_width_ok scenarios=9 evidence=...`.

## Rules

- One layout per run: `encoding` (default `utf-8-sig`), optional `skip_lines` for banner lines, and a `fields` list of `{"name", "start", "length"}` entries. `start` is 1-based; `length` counts columns, spaces included.
- The flag replaces suffix-based reader selection everywhere: plain files, directory scans, gzip streams, and zip members (including members whose suffix is otherwise unsupported).
- Padding inside a span is trimmed, and an all-padding span becomes `null`, matching the CSV reader's empty cell. Columns beyond the last span are ignored.
- A line shorter than the layout rejects that record with `fixed_width_short_line` and the run continues. Blank physical lines and banner lines are skipped.
- Overlapping spans, out-of-order spans, duplicate names, unknown keys, and unknown encodings fail with exit 2 before any input is read.
- `verify` replays the same layout before comparing digests, and `infer` drafts a layout from fixed-width data with the same flag.

## Manual check

```powershell
python main.py --input legacy\export.dat --fixed-width layout.json --output-dir fw-run
python main.py verify --input legacy\export.dat --fixed-width layout.json --output-dir fw-run
```
