# Air-gap deployment

An air-gapped machine has no package index. `scripts/check_wheelhouse.py` inspects a directory of wheels, walks the dependency closure from the local `airlock-db` wheel, and writes an install manifest. The target machine re-runs the checker with `--verify` before it installs, so a truncated copy or a changed byte stops the deployment.

The manifest is `install_manifest.json`, and it sits beside the wheels. It lists each filename, size, SHA-256 hash, and the distribution name and version read from the wheel metadata. Two runs over the same directory produce byte-identical manifests. The checker uses only the standard library, so it runs before anything is installed.

Replace `WHEELHOUSE` in every command below with the directory that holds the wheels.

## Stage on the connected machine

Build the local wheel, because `airlock-db` is not on PyPI, then download the dependencies as wheels:

```sh
python -m pip wheel --no-deps --wheel-dir WHEELHOUSE .
python -m pip download --only-binary=:all: --dest WHEELHOUSE pydantic "PyYAML>=6,<7"
```

`--only-binary=:all:` refuses source distributions, so the directory holds wheels only. Check the closure and write the manifest:

```sh
python scripts/check_wheelhouse.py WHEELHOUSE
```

Expected: `wheelhouse_ok wheels=N manifest=WHEELHOUSE/install_manifest.json`, where `N` counts the wheels.

The check fails with exit code 1 when a dependency is missing, when no wheel satisfies a `Requires-Dist` specifier, when a file is not a wheel, or when the local `airlock_db` wheel is absent. Each failure prints one `wheelhouse:` line, then a `wheelhouse_failed failures=N` token. Exit code 2 means the directory does not exist.

This is the only step that uses the network.

## Move the directory

Copy the whole directory to the air-gapped machine. Keep the wheels and `install_manifest.json` together, because the manifest records the checksums of the wheels beside it. Do not install anything on this machine.

## Verify and install on the air-gapped machine

Re-run the checker in verify mode:

```sh
python scripts/check_wheelhouse.py WHEELHOUSE --verify
```

Expected: the same token, with `verified=1` appended. A size, hash, filename, or package mismatch prints a `wheelhouse:` line and exits 1. Copy the directory again instead of repairing single files by hand.

Create a virtual environment and install from the local directory:

```sh
python -m venv airlock-venv
python -m pip install --no-index --find-links WHEELHOUSE airlock-db
```

Call the environment's own interpreter. On Windows that is `airlock-venv\Scripts\python`, and on Linux and macOS it is `airlock-venv/bin/python`. `--no-index` ignores every package index, and `--find-links` reads only the local directory, so a missing wheel fails the install instead of reaching out to the network.

## Run a migration

```sh
python -m airlock_db --input examples/messy_transactions.json --output-dir output
```

The console scripts run the same command: `airlock-db` and `schema-sentry`. The built-in fixture has invalid rows, so the run exits 1 and still writes `airlock_summary.json`, `airlock_report.html`, and `target_migration.db`.

## Match the target platform

Build the wheelhouse for the operating system and the Python series of the air-gapped machine. A wheel filename names its tags, such as `cp312-cp312-win_amd64`, and pip skips a wheel whose tags do not fit. The continuous integration job stages and installs on Python 3.12. `airlock-db` requires Python 3.10 or newer, so a 3.10 or 3.11 target needs its own wheelhouse from a matching interpreter.

`pydantic-core` and `PyYAML` ship compiled wheels per interpreter, system, and processor. Stage the closure again on a matching machine when the target differs.

## Checker reference

| Flag | Effect |
| --- | --- |
| `WHEELHOUSE` | Directory to scan. Required |
| `--manifest PATH` | Manifest location. Defaults to `WHEELHOUSE/install_manifest.json` |
| `--verify` | Compare against the manifest and write nothing |

Exit codes are `0` for a pass, `1` for a failed check, and `2` for a missing directory or an absent or unreadable manifest in verify mode.

The version comparison covers the operators `==`, `!=`, `<`, `<=`, `>`, `>=`, and `~=` on release numbers. It is not a full PEP 440 implementation. A requirement counts as required unless its marker mentions `extra`, so a wheelhouse this checker accepts holds at least what a default install needs. The manifest ignores itself during the scan, and every other non-wheel file fails the check.
