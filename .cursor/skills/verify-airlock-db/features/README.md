# AirLock DB feature map

- [Built-in transaction migration](builtin-migration.md) validates financial transaction records and writes accepted rows to SQLite.
- [Dynamic schema migration](dynamic-schema.md) validates client-specific fields. The real-data example uses public UCI retail records.
- [Rejected-row repair](repair.md) reviews rejected records and commits accepted corrections.
