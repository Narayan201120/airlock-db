# Schema inference

Covers `airlock-db infer --input PATH`, which drafts a dynamic-schema config from observed records.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_schema_infer.py ci-infer
```

The script drafts a layout from the real-data sample and from the messy fixture, migrates each source with its own draft, and checks the stored counts. It also checks JSON output, the overwrite guard, an explicit `--id-field`, an unknown id field, an input with no records, and a gzip source.

Expected result: `schema_infer_ok scenarios=8 evidence=...`.

## Rules

- The draft is a normal layout. It loads with `--config` and is meant to be reviewed, not trusted.
- Types come from observed values only: JSON numbers become `integer`, `number`, or `decimal`, decimal-shaped strings become `decimal`, ISO text becomes `date` or `datetime`, dash-formatted UUIDs become `uuid`, everything else stays `string`. No enum or length limits are invented.
- A field is `required` only when every scanned record has a non-null value for it.
- When all values of a field are strings, a mixed set of text formats drafts as `string`, which accepts every observed value.
- When value kinds conflict and no safe type exists, the draft uses the most common kind and flags the field under `Conflicts` in the command output. Migration then reports the minority rows as invalid.
- YAML drafts carry each note as a comment above the field. `--limit` bounds the scan (default 10,000 records, `0` scans everything). An existing output file is refused unless `--overwrite` is passed.
- Field names that a layout cannot hold, such as names with spaces or keywords, are skipped and listed in the output.

## Manual check

```powershell
python main.py infer --input examples/real_data/online_retail_sample.jsonl --output schema_draft.yaml
python main.py --input examples/real_data/online_retail_sample.jsonl --config schema_draft.yaml --output-dir inferred-run
```
