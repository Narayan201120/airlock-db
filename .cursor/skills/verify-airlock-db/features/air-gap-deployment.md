# Air-gap deployment

Covers `scripts/check_wheelhouse.py` and `docs/deployment.md`, the two-machine transfer story for offline installs.

## Drive

```powershell
python scripts\check_wheelhouse.py WHEELHOUSE
python scripts\check_wheelhouse.py WHEELHOUSE --verify
```

The first pass confirms the directory covers `airlock-db` and its dependency closure from wheel filenames and `*.dist-info/METADATA`, then writes `WHEELHOUSE\install_manifest.json` with sizes and SHA-256 digests. `--verify` re-reads the directory and compares the filename set, sizes, hashes, and package versions.

Expected results: `wheelhouse_ok wheels=N manifest=PATH` after a write, and `wheelhouse_ok wheels=N manifest=PATH verified=1` after a passing check. A mismatch prints `wheelhouse_failed failures=N` and exits 1. A missing directory, or an absent or unreadable manifest in verify mode, exits 2.

## Rules

- The checker is fully offline and reads the directory only. The one network step, `pip download` on the connected side, is user-run and documented in `docs/deployment.md`.
- Manifest bytes are stable: sorted entries and no timestamps. Two runs over an unchanged directory are byte-identical.
- The scan skips the selected manifest path, and any file named `install_manifest.json`, so a stale manifest never counts as a wheel.
- Dependency names and versions come from wheel metadata. `Requires-Dist` markers for `extra` are skipped; everything else counts as required. The comparator is stdlib-only, so the checker needs no dependencies of its own.

## Manual check

```powershell
python -m pip wheel --no-deps --wheel-dir wheelhouse .
python -m pip download --only-binary=:all: --dest wheelhouse pydantic "PyYAML>=6,<7"
python scripts\check_wheelhouse.py wheelhouse
python scripts\check_wheelhouse.py wheelhouse --verify
python -m pip install --no-index --find-links wheelhouse airlock-db
python -m airlock_db --help
```
