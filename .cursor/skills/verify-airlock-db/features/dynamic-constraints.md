# Constraints for dynamic schemas

Covers the `field_rules` block in a constraints file, which extends business rules to dynamic schemas beside the built-in model.

## Drive

```powershell
python .cursor\skills\verify-airlock-db\scripts\verify_dynamic_constraints.py ci-constraints
```

The script runs a dynamic migration with the rules and requires exactly the predicted rejections per reason (2 enum, 2 range, 1 pattern, 1 required-any-of) with the exact messages, plus a clean run over valid rows and the absent, null, and empty-string edge cases. It also requires six bad rules files to stop the run with exit 2, and covers the same rules file at migration, repair, and verify.

Expected result: `dynamic_constraints_ok scenarios=7 evidence=...`.

## Rules

- An entry is per-field (`field` plus any of `enum`, `min`, `max`, `pattern`) or a `required_any_of` group. An entry that mixes both kinds is rejected before the run starts.
- Rules run after transforms and validation, for built-in and dynamic records alike, each with its own reason code: `constraint_enum`, `constraint_range`, `constraint_pattern`, `constraint_required_any_of`.
- `enum` compares numeric-aware, `pattern` matches the text form in full, ranges include both bounds, and `required_any_of` treats null and empty strings as missing. Rules skip fields that are absent from the record.
- The parsed file feeds the fingerprint, so editing rules starts a new resume namespace. An empty rule set leaves existing digests unchanged.
- `--constraints` combines with `--config` only when the file contains `field_rules` alone.

## Manual check

```powershell
python main.py --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --constraints field_rules.json --output-dir constraints-run
python main.py verify --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --constraints field_rules.json --output-dir constraints-run
```

A rules file for the retail sample:

```json
{
  "field_rules": [
    { "field": "quantity", "min": 1, "max": 100000 },
    { "field": "stock_code", "pattern": "[0-9]{5}[A-Z]?" },
    { "required_any_of": ["description", "stock_code"] }
  ]
}
```
