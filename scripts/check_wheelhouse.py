"""Check that a wheelhouse installs the project with no network access.

Scans a directory of wheels, walks the dependency closure of the local
``airlock_db`` wheel by reading the ``Requires-Dist`` lines in every wheel's
``.dist-info/METADATA``, and writes a manifest of filenames, sizes, and
SHA-256 hashes. Copy the directory to the target machine and re-run the same
command with ``--verify`` to prove the transfer kept every byte.

The checker uses only the standard library, so it runs on a machine that has
nothing installed yet.

This is not a full PEP 440 implementation. Versions compare on dot-separated
release numbers with an optional trailing label, and the supported operators
are ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, and ``~=``. A compatible
release such as ``~=2.6`` means ``>=2.6, <3``. ``==X.*`` and ``!=X.*`` match
on the release prefix; other operators after ``.*`` are rejected.

Every ``Requires-Dist`` line without an ``extra`` marker counts as required,
including lines that mention the Python version or the platform. That is
conservative on purpose, so a wheelhouse this checker accepts holds at least
what a default install needs. Requirements are checked one line at a time, so
two lines that constrain the same distribution can each be satisfied by a
different wheel.

Any file that is not a wheel is a failure, except the manifest itself, so the
default location inside the wheelhouse stays usable.

Success prints one token line on stdout, failure prints ``wheelhouse:`` lines
and one token line on stderr:

    wheelhouse_ok wheels=N manifest=PATH
    wheelhouse_ok wheels=N manifest=PATH verified=1
    wheelhouse_failed failures=N

Exit codes: 0 the check passed, 1 the check failed, 2 the directory is
missing or ``--verify`` got no usable manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Sequence

_MANIFEST_FORMAT = "airlock-db-wheelhouse-manifest"
_MANIFEST_VERSION = 1
_MANIFEST_NAME = "install_manifest.json"
_ROOT_DISTRIBUTION = "airlock-db"
_ROOT_LABEL = "airlock_db"
_CHUNK_SIZE = 1024 * 1024

_WHEEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
_SPECIFIER = re.compile(r"^(~=|==|!=|<=|>=|<|>)(.+)$")


@dataclass(frozen=True)
class _Version:
    """Release numbers plus an optional trailing label. Not full PEP 440."""

    release: tuple[int, ...]
    label: str

    @classmethod
    def parse(cls, text: str) -> "_Version | None":
        cleaned = text.strip()
        if not cleaned:
            return None
        parts = cleaned.split(".")
        release: list[int] = []
        label = ""
        for index, part in enumerate(parts):
            if part.isdigit() and not label:
                release.append(int(part))
                continue
            label = ".".join(parts[index:])
            break
        if not release:
            return None
        return cls(tuple(release), label)


@dataclass(frozen=True)
class _Specifier:
    operator: str
    value: str


@dataclass(frozen=True)
class _Requirement:
    name: str
    distribution: str
    specifiers: tuple[_Specifier, ...]

    @property
    def text(self) -> str:
        joined = ",".join(f"{item.operator}{item.value}" for item in self.specifiers)
        return f"{self.name}{joined}" if joined else self.name


@dataclass(frozen=True)
class _Manifest:
    """The manifest body, typed so callers do not guess at its shape."""

    wheels: tuple[dict[str, object], ...]
    packages: dict[str, str]

    def to_json(self) -> dict[str, object]:
        return {
            "format": _MANIFEST_FORMAT,
            "version": _MANIFEST_VERSION,
            "wheels": list(self.wheels),
            "packages": self.packages,
        }


@dataclass(frozen=True)
class _Wheel:
    path: Path
    filename: str
    distribution: str
    raw_distribution: str
    version: str
    parsed_version: _Version


def _normalize(text: str) -> str:
    return re.sub(r"[-_.]+", "-", text.strip()).lower()


def _parse_wheel_name(path: Path) -> _Wheel:
    filename = path.name
    stem = filename[: -len(".whl")]
    parts = stem.split("-")
    if len(parts) == 5:
        name, version = parts[0], parts[1]
    elif len(parts) == 6:
        name, version, _build = parts[0], parts[1], parts[2]
    else:
        raise ValueError(
            f"expected 5 dash-separated fields, or 6 with a build tag, "
            f"found {len(parts)}"
        )
    if not _WHEEL_NAME.match(name):
        raise ValueError(f"unusable distribution name {name!r}")
    parsed = _Version.parse(version)
    if parsed is None:
        raise ValueError(f"unusable version {version!r}")
    return _Wheel(
        path=path,
        filename=filename,
        distribution=_normalize(name),
        raw_distribution=name,
        version=version,
        parsed_version=parsed,
    )


def _compare_versions(left: _Version, right: _Version) -> int:
    length = max(len(left.release), len(right.release))
    left_release = left.release + (0,) * (length - len(left.release))
    right_release = right.release + (0,) * (length - len(right.release))
    if left_release != right_release:
        return -1 if left_release < right_release else 1
    if left.label == right.label:
        return 0
    if not left.label:
        return 1
    if not right.label:
        return -1
    return -1 if left.label < right.label else 1


def _matches(version: _Version, specifier: _Specifier) -> bool:
    if specifier.value.endswith(".*"):
        base = _Version.parse(specifier.value[:-2])
        if base is None:
            return False
        prefix = version.release[: len(base.release)]
        equal = prefix == base.release
        return equal if specifier.operator == "==" else not equal
    spec = _Version.parse(specifier.value)
    if spec is None:
        return False
    comparison = _compare_versions(version, spec)
    if specifier.operator == "==":
        return comparison == 0
    if specifier.operator == "!=":
        return comparison != 0
    if specifier.operator == "<":
        return comparison < 0
    if specifier.operator == "<=":
        return comparison <= 0
    if specifier.operator == ">":
        return comparison > 0
    if specifier.operator == ">=":
        return comparison >= 0
    if len(spec.release) < 2:
        return False
    if comparison < 0:
        return False
    # ~=2.6 means >=2.6 and below the next prefix, so <3.
    upper = spec.release[:-1][:-1] + (spec.release[-2] + 1,)
    return _compare_versions(version, _Version(upper, "")) < 0


def _satisfies(version: _Version, requirement: _Requirement) -> bool:
    return all(_matches(version, item) for item in requirement.specifiers)


def _parse_requirement(text: str) -> _Requirement | None:
    """Return the requirement, or None for an optional extra."""

    body, _, marker = text.partition(";")
    if "extra" in marker.lower():
        return None
    match = _REQUIREMENT.match(body)
    if match is None:
        raise ValueError(f"no distribution name in {text.strip()!r}")
    name = match.group(1)
    remainder = match.group(2).strip()
    if remainder.startswith("("):
        if not remainder.endswith(")"):
            raise ValueError(f"unbalanced parentheses in {text.strip()!r}")
        remainder = remainder[1:-1]
    specifiers: list[_Specifier] = []
    for item in remainder.split(","):
        item = item.strip()
        if not item:
            continue
        specifier = _SPECIFIER.match(item)
        if specifier is None:
            raise ValueError(f"unsupported version specifier {item!r}")
        operator, raw = specifier.group(1), specifier.group(2).strip()
        if raw.endswith(".*") and operator not in ("==", "!="):
            raise ValueError(f"unsupported version specifier {item!r}")
        if _Version.parse(raw[:-2] if raw.endswith(".*") else raw) is None:
            raise ValueError(f"unsupported version {raw!r}")
        specifiers.append(_Specifier(operator, raw))
    return _Requirement(
        name=name, distribution=_normalize(name), specifiers=tuple(specifiers)
    )


def _metadata_text(wheel: _Wheel) -> str:
    with zipfile.ZipFile(wheel.path) as archive:
        members = sorted(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        if not members:
            raise ValueError("no .dist-info/METADATA member")
        return archive.read(members[0]).decode("utf-8", errors="replace")


def _distribution_name(wheel: _Wheel) -> str:
    try:
        message = Parser().parsestr(_metadata_text(wheel))
    except (OSError, zipfile.BadZipFile, ValueError):
        return wheel.raw_distribution
    name = message.get("Name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return wheel.raw_distribution


def _required_distributions(wheel: _Wheel) -> list[str]:
    message = Parser().parsestr(_metadata_text(wheel))
    return [line for line in (message.get_all("Requires-Dist") or []) if line.strip()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_manifest(wheels: Sequence[_Wheel]) -> _Manifest:
    entries = tuple(
        {
            "filename": wheel.filename,
            "size": wheel.path.stat().st_size,
            "sha256": _sha256(wheel.path),
        }
        for wheel in wheels
    )
    packages = {_distribution_name(wheel): wheel.version for wheel in wheels}
    return _Manifest(
        wheels=entries,
        packages={name: packages[name] for name in sorted(packages)},
    )


def _closure_failures(wheels: Sequence[_Wheel]) -> list[str]:
    by_name: dict[str, list[_Wheel]] = {}
    for wheel in wheels:
        by_name.setdefault(wheel.distribution, []).append(wheel)

    roots = by_name.get(_ROOT_DISTRIBUTION, [])
    if not roots:
        return [
            f"missing the local project wheel {_ROOT_LABEL}-*.whl; build it with "
            f"python -m pip wheel --no-deps --wheel-dir WHEELHOUSE ."
        ]

    failures: list[str] = []
    visited: set[str] = set()
    queue = list(roots)
    while queue:
        wheel = queue.pop(0)
        if wheel.filename in visited:
            continue
        visited.add(wheel.filename)
        try:
            requirements = _required_distributions(wheel)
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            failures.append(f"cannot read the metadata of {wheel.filename}: {exc}")
            continue
        for line in requirements:
            try:
                requirement = _parse_requirement(line)
            except ValueError as exc:
                failures.append(
                    f"cannot read a requirement of {wheel.filename}: {exc}"
                )
                continue
            if requirement is None:
                continue
            candidates = by_name.get(requirement.distribution, [])
            if not candidates:
                failures.append(
                    f"missing distribution {requirement.name} "
                    f"(required by {wheel.filename})"
                )
                continue
            matching = [
                candidate
                for candidate in candidates
                if _satisfies(candidate.parsed_version, requirement)
            ]
            if not matching:
                found = ", ".join(sorted(candidate.version for candidate in candidates))
                failures.append(
                    f"no wheel satisfies {requirement.text} "
                    f"(required by {wheel.filename}); the wheelhouse has {found}"
                )
                continue
            queue.extend(matching)
    return failures


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _read_manifest(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            f"wheelhouse: no manifest at {path}; run the checker first",
            file=sys.stderr,
        )
        return None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"wheelhouse: cannot read the manifest {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict) or payload.get("format") != _MANIFEST_FORMAT:
        print(
            f"wheelhouse: {path} is not a {_MANIFEST_FORMAT} manifest",
            file=sys.stderr,
        )
        return None
    return payload


def _compare_manifest(existing: dict[str, object], computed: _Manifest) -> list[str]:
    mismatches: list[str] = []
    recorded = existing.get("wheels")
    if not isinstance(recorded, list):
        return ["the manifest has no wheel list"]

    old: dict[str, dict[str, object]] = {}
    for entry in recorded:
        filename = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(filename, str):
            mismatches.append("the manifest has a wheel entry without a filename")
            continue
        old[filename] = entry
    new = {str(entry["filename"]): entry for entry in computed.wheels}
    for filename in sorted(set(old) - set(new)):
        mismatches.append(f"{filename} is in the manifest but not in the wheelhouse")
    for filename in sorted(set(new) - set(old)):
        mismatches.append(f"{filename} is in the wheelhouse but not in the manifest")
    for filename in sorted(set(old) & set(new)):
        recorded_entry, actual = old[filename], new[filename]
        if recorded_entry.get("size") != actual["size"]:
            mismatches.append(
                f"{filename}: size is {recorded_entry.get('size')!r} in the manifest "
                f"and {actual['size']} on disk"
            )
        if recorded_entry.get("sha256") != actual["sha256"]:
            mismatches.append(f"{filename}: the SHA-256 hash differs from the manifest")

    recorded_packages = existing.get("packages")
    if not isinstance(recorded_packages, dict):
        mismatches.append("the manifest has no package map")
    elif recorded_packages != computed.packages:
        for name in sorted(set(recorded_packages) | set(computed.packages)):
            before = recorded_packages.get(name, "missing")
            after = computed.packages.get(name, "missing")
            if before != after:
                mismatches.append(
                    f"package {name}: the manifest says {before} and the wheelhouse "
                    f"says {after}"
                )
    return mismatches


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check that a wheelhouse covers the project and its dependencies, "
            "then write or verify its install manifest."
        )
    )
    parser.add_argument(
        "wheelhouse",
        type=Path,
        help="directory that holds the wheels and the manifest",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=f"manifest path (default: WHEELHOUSE/{_MANIFEST_NAME})",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "compare the wheelhouse against an existing manifest instead of "
            "writing one"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    wheelhouse = args.wheelhouse.expanduser()
    if not wheelhouse.is_dir():
        print(
            f"wheelhouse: expected a directory of wheels at {wheelhouse}",
            file=sys.stderr,
        )
        return 2
    manifest_path = args.manifest or wheelhouse / _MANIFEST_NAME

    existing: dict[str, object] | None = None
    if args.verify:
        existing = _read_manifest(manifest_path)
        if existing is None:
            return 2

    failures: list[str] = []
    wheels: list[_Wheel] = []
    try:
        entries = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        print(
            f"wheelhouse: cannot read the directory {wheelhouse}: {exc}",
            file=sys.stderr,
        )
        return 2
    for entry in entries:
        if not entry.is_file():
            continue
        if _same_file(entry, manifest_path) or entry.name == _MANIFEST_NAME:
            continue
        if not entry.name.lower().endswith(".whl"):
            failures.append(
                f"{entry.name}: not a wheel; collect dependencies with "
                f"pip download --only-binary=:all:"
            )
            continue
        try:
            wheels.append(_parse_wheel_name(entry))
        except ValueError as exc:
            failures.append(f"{entry.name}: unusable wheel filename ({exc})")

    if not wheels:
        failures.append(f"no wheels found in {wheelhouse}")
    else:
        failures.extend(_closure_failures(wheels))

    if failures:
        for message in failures:
            print(f"wheelhouse: {message}", file=sys.stderr)
        print(f"wheelhouse_failed failures={len(failures)}", file=sys.stderr)
        return 1

    manifest = _build_manifest(wheels)
    total_bytes = sum(int(entry["size"]) for entry in manifest.wheels)
    print(f"Wheelhouse {wheelhouse}: {len(wheels)} wheels, {total_bytes:,} bytes.")

    if existing is not None:
        mismatches = _compare_manifest(existing, manifest)
        if mismatches:
            for message in mismatches:
                print(f"wheelhouse: {message}", file=sys.stderr)
            print(f"wheelhouse_failed failures={len(mismatches)}", file=sys.stderr)
            return 1
        print(f"wheelhouse_ok wheels={len(wheels)} manifest={manifest_path} verified=1")
        return 0

    try:
        with manifest_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(manifest.to_json(), indent=2))
            stream.write("\n")
    except OSError as exc:
        print(
            f"wheelhouse: cannot write the manifest {manifest_path}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(f"wheelhouse_ok wheels={len(wheels)} manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
