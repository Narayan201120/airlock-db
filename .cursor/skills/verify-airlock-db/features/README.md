# AirLock DB feature map

- [Built-in transaction migration](builtin-migration.md) validates financial transaction records and writes accepted rows to SQLite.
- [Dynamic schema migration](dynamic-schema.md) validates client-specific fields. The real-data example uses public UCI retail records.
- [Schema transforms](schema-transforms.md) normalizes source text with per-field rules before validation.
- [Constraints for dynamic schemas](dynamic-constraints.md) guards fields with enum, range, pattern, and required-any-of rules.
- [Fixed-width input](fixed-width-input.md) reads undelimited positional text through a layout of named column spans.
- [Compressed inputs](archives.md) reads `.gz` files and `.zip` archives of supported records directly.
- [Input encoding override](input-encoding.md) reads non-UTF-8 text with `--input-encoding`.
- [Schema inference](schema-infer.md) drafts a dynamic-schema config from observed records.
- [Rejected-row repair](repair.md) reviews rejected records and commits accepted corrections.
- [CLI wins](cli-wins.md) covers preview runs, SQL source queries, and decisions templates.
- [Handoff verification](verify-handoff.md) reconciles a committed target with the sources that produced it.
- [Run-to-run comparison](run-compare.md) adds deltas between two runs to the rebuilt report.
- [Scale soak](scale-soak.md) proves memory stays flat at 1M rows.
- [Air-gap deployment](air-gap-deployment.md) stages and checks an offline wheelhouse for transfer.
