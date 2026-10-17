"""End-to-end check for `airlock-db infer`.

Drafts a layout from real fixtures, then migrates with the draft and requires
the predicted outcome: a clean sample migrates with zero issues, and a messy
fixture fails on exactly the rows its conflicts flagged.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SAMPLE = REPO_ROOT / "examples" / "real_data" / "online_retail_sample.jsonl"
MESSY = REPO_ROOT / "examples" / "messy_transactions.json"


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode, result.stdout + result.stderr


def _summary_counts(output_dir: Path) -> tuple[int, int, int]:
    summary = json.loads((output_dir / "airlock_summary.json").read_text(encoding="utf-8"))
    records = summary["records"]
    return records["checked"], records["valid"], records["invalid"]


def _jsonl_field_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            names.update(json.loads(line).keys())
    return names


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that `infer` drafts layouts that migrate their source."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        type=Path,
        help="scratch directory; defaults to a fresh temp directory",
    )
    args = parser.parse_args()
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="airlock-infer-"))
    _reset_dir(work)

    import yaml

    failures: list[str] = []
    scenarios: dict[str, object] = {}

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    # 1. Clean sample: draft covers every field; migration with it is clean.
    draft = work / "retail.yaml"
    code, text = _run(["infer", "--input", str(SAMPLE), "--output", str(draft)])
    check("sample infer exit", code == 0, f"exit={code}\n{text[-500:]}")
    layout = yaml.safe_load(draft.read_text(encoding="utf-8"))
    check("sample version", layout.get("version") == 1, f"version={layout.get('version')}")
    check("sample model_name", layout.get("model_name") == "InferredRecord", str(layout.get("model_name")))
    check("sample id_field", layout.get("id_field") == "source_row", str(layout.get("id_field")))
    check("sample strip", layout.get("strip_whitespace") is False, str(layout.get("strip_whitespace")))
    expected_fields = _jsonl_field_names(SAMPLE)
    check(
        "sample field set",
        set(layout.get("fields", {})) == expected_fields,
        f"draft={sorted(layout.get('fields', {}))} expected={sorted(expected_fields)}",
    )
    types = {name: spec.get("type") for name, spec in layout.get("fields", {}).items()}
    check("sample source_row type", types.get("source_row") == "integer", str(types.get("source_row")))
    check("sample invoice_date type", types.get("invoice_date") == "datetime", str(types.get("invoice_date")))
    check("sample unit_price type", types.get("unit_price") == "decimal", str(types.get("unit_price")))
    check("sample quantity type", types.get("quantity") == "integer", str(types.get("quantity")))
    code, text = _run(["--input", str(SAMPLE), "--config", str(draft), "--output-dir", str(work / "out-sample")])
    check("sample migrate exit", code == 0, f"exit={code}\n{text[-500:]}")
    counts = _summary_counts(work / "out-sample")
    check("sample migrate counts", counts == (10, 10, 0), f"counts={counts}")
    scenarios["sample"] = {"counts": list(counts), "fields": len(expected_fields)}

    # 2. Messy fixture: conflicts are flagged, and exactly those rows fail.
    messy_draft = work / "messy.yaml"
    code, text = _run(["infer", "--input", str(MESSY), "--output", str(messy_draft)])
    check("messy infer exit", code == 0, f"exit={code}\n{text[-500:]}")
    check("messy conflict posted_at", "posted_at" in text, "posted_at is not listed as a conflict")
    check("messy conflict amount", "amount" in text, "amount is not listed as a conflict")
    messy_layout = yaml.safe_load(messy_draft.read_text(encoding="utf-8"))
    check("messy id_field", messy_layout.get("id_field") == "account_id", str(messy_layout.get("id_field")))
    code, text = _run(["--input", str(MESSY), "--config", str(messy_draft), "--output-dir", str(work / "out-messy")])
    check("messy migrate exit", code == 1, f"exit={code}\n{text[-500:]}")
    counts = _summary_counts(work / "out-messy")
    check("messy migrate counts", counts == (32, 30, 2), f"counts={counts}")
    scenarios["messy"] = {"counts": list(counts)}

    # 3. JSON output carries the same field set.
    json_draft = work / "retail.json"
    code, text = _run(["infer", "--input", str(SAMPLE), "--output", str(json_draft)])
    check("json infer exit", code == 0, f"exit={code}\n{text[-500:]}")
    layout_json = json.loads(json_draft.read_text(encoding="utf-8"))
    check(
        "json field set",
        set(layout_json.get("fields", {})) == expected_fields,
        f"json={sorted(layout_json.get('fields', {}))}",
    )
    scenarios["json"] = {"fields": len(layout_json.get("fields", {}))}

    # 4. Overwrite guard.
    code, text = _run(["infer", "--input", str(SAMPLE), "--output", str(json_draft)])
    check("overwrite guard exit", code == 1, f"exit={code}")
    check("overwrite guard message", "already exists" in text, text[-300:])
    code, text = _run(["infer", "--input", str(SAMPLE), "--output", str(json_draft), "--overwrite"])
    check("overwrite allowed exit", code == 0, f"exit={code}\n{text[-300:]}")
    scenarios["overwrite"] = {"guard": True}

    # 5. Explicit id field is honored and forced required.
    override = work / "override.yaml"
    code, text = _run(
        ["infer", "--input", str(MESSY), "--output", str(override), "--id-field", "transaction_id"]
    )
    check("id override exit", code == 0, f"exit={code}\n{text[-300:]}")
    override_layout = yaml.safe_load(override.read_text(encoding="utf-8"))
    check("id override field", override_layout.get("id_field") == "transaction_id", str(override_layout.get("id_field")))
    check(
        "id override required",
        override_layout["fields"]["transaction_id"]["required"] is True,
        str(override_layout["fields"]["transaction_id"]),
    )
    scenarios["id_override"] = {"id_field": "transaction_id"}

    # 6. Unknown id field is refused.
    code, text = _run(
        ["infer", "--input", str(SAMPLE), "--output", str(work / "bad.yaml"), "--id-field", "nope"]
    )
    check("bad id exit", code == 1, f"exit={code}")
    check("bad id message", "not observed" in text, text[-300:])
    scenarios["bad_id"] = {"exit": code}

    # 7. An input with no records is refused.
    empty = work / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    code, text = _run(["infer", "--input", str(empty), "--output", str(work / "empty.yaml")])
    check("empty input exit", code == 1, f"exit={code}")
    check("empty input message", "no records were read" in text, text[-300:])
    scenarios["empty"] = {"exit": code}

    # 8. Compressed input uses the same readers.
    import gzip

    compressed = work / "sample.jsonl.gz"
    with gzip.open(compressed, "wb") as stream:
        stream.write(SAMPLE.read_bytes())
    gz_draft = work / "gz.yaml"
    code, text = _run(["infer", "--input", str(compressed), "--output", str(gz_draft), "--overwrite"])
    check("gz infer exit", code == 0, f"exit={code}\n{text[-300:]}")
    gz_layout = yaml.safe_load(gz_draft.read_text(encoding="utf-8"))
    check(
        "gz field set",
        set(gz_layout.get("fields", {})) == expected_fields,
        f"gz={sorted(gz_layout.get('fields', {}))}",
    )
    scenarios["gz"] = {"fields": len(gz_layout.get("fields", {}))}

    evidence = work / "schema_infer_verification.json"
    evidence.write_text(
        json.dumps(
            {
                "status": "passed" if not failures else "failed",
                "scenarios": scenarios,
                "failures": failures,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if failures:
        for message in failures:
            print(f"FAIL {message}", file=sys.stderr)
        print(f"schema_infer_failed failures={len(failures)} work_dir={work}", file=sys.stderr)
        return 1
    print(f"schema_infer_ok scenarios={len(scenarios)} evidence={evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
