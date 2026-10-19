# Input encoding override

Covers `--input-encoding NAME`, which reads non-UTF-8 text through the JSON, JSONL, and line-based readers.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_input_encoding.py ci-encoding
```

The script requires every cp1252 fixture (JSONL, JSON array, TSV, CSV, and a gz member) to fail without the flag and migrate clean with it, requires the decoded text to survive into SQLite, and checks that a UTF-8-with-BOM file still reads without flags. It also drives `verify` and `infer` with the flag.

Expected result: `input_encoding_ok scenarios=9 evidence=...`.

## Rules

- The flag reaches the JSON reader, the JSONL byte parser, and the line-based TSV and header readers, which otherwise default to `utf-8-sig`. CSV keeps `--csv-encoding`; both flags are accepted together.
- XML reads its declaration and ignores the flag by design. Fixed-width layouts keep the encoding from their layout file.
- `verify` and `infer` honor the flag, so handoff checks and drafts read the same bytes as the migration.
- An unknown encoding name exits 2 before any input is read. A file that still cannot decode exits 1 as a source error.

## Manual check

```powershell
python main.py --input legacy\export-cp1252.jsonl --input-encoding cp1252 --output-dir encoding-run
python main.py verify --input legacy\export-cp1252.jsonl --input-encoding cp1252 --output-dir encoding-run
```
