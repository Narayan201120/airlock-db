# Compressed inputs

Covers `.gz` files and `.zip` archives of supported records.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_archive_input.py ci-archives
```

The script builds a plain JSONL fixture, its gzip and zip twins, a scan directory that holds only a compressed file, and two broken archives. It runs the real CLI against each and compares stored rows and summary counts to the plain run.

Expected result: `archive_input_ok scenarios=6 evidence=...`.

## Rules

- One `.gz` layer over a supported suffix: `x.jsonl.gz`, `x.csv.gz`, and so on. A `.gz` inside a zip, or a zip inside a zip, is skipped as a nested archive.
- Zip members validate in name order. Directories and unsupported suffixes are skipped, and every member gets its own source label such as `batch.zip\inner.jsonl`.
- Nothing is extracted to disk. The plain readers handle the decompressed bytes, so size limits and issue codes are identical.
- A corrupt archive is a source issue, `archive_error` for zip and `io_error` for gzip, and never crashes the run.

## Manual check

```powershell
python -c "import gzip,pathlib; gzip.open('sample.jsonl.gz','wb').write(pathlib.Path('sample.jsonl').read_bytes())"
python main.py --input sample.jsonl.gz --output-dir gz-run
```
