# Schema transforms

Covers the per-field `transforms` list in a dynamic schema config, which normalizes source text before validation.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_transforms.py ci-transforms
```

The script migrates a fixture with padded text, European dates and decimals, and enum aliases through `trim`, case folding, `datetime`, `decimal`, and `alias` rules. It then mutates the target, repairs the rejected rows with `--decisions`, and checks three configs that must fail before input is processed.

Expected result: `transforms_ok scenarios=7 evidence=...`.

## Rules

- Rules run in listed order, before validation, and only touch string values. `trim`, `lowercase`, and `uppercase` take no parameters; `datetime`, `decimal`, and `alias` are typed to `date`/`datetime`, `decimal`, and `enum` fields.
- A value a rule cannot convert rejects the record with a `transform_failed` issue that names the field and the rule. Nothing is guessed.
- `datetime` writes ISO 8601, `decimal` writes plain dot-separated text, and `alias` maps exact source values to declared enum choices.
- Rules feed the schema fingerprint, so editing them targets a new table and resume namespace. `verify` re-applies the same rules before comparing digests, and a repair session normalizes corrected values the same way.
- The summary counts changed field values under `records.normalized_fields`. Rejected records contribute nothing.

## Manual check

```powershell
python main.py --input legacy\tickets.jsonl --config layout.yaml --error-log --output-dir transform-run
python main.py repair --error-log transform-run\airlock_errors.jsonl --config layout.yaml
```
