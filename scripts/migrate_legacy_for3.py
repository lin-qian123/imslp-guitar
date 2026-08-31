#!/usr/bin/env python
"""Copy the verified legacy trio library into its IMSLP-named category.

The source tree is never modified. Mixed-instrument files are retained under
``quarantine/mixed-instrument`` in the target and removed from the active
manifest before the inherited renderer rebuilds the local indexes.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path


MIXED_TOKENS = ("double bass", "bass guitar", "contrabass")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_mixed(record: dict) -> bool:
    section = str(record.get("section", "")).casefold()
    return any(token in section for token in MIXED_TOKENS)


def filter_status_log(path: Path, excluded_paths: set[str]) -> None:
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = [row for row in reader if row.get("relative_path") not in excluded_paths]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def migrate(source: Path, target: Path) -> dict:
    source = source.resolve()
    target = target.resolve()
    if not source.is_dir():
        raise RuntimeError(f"source library does not exist: {source}")
    if target.exists():
        raise RuntimeError(f"target already exists: {target}")
    if source == target or source in target.parents or target in source.parents:
        raise RuntimeError("source and target must be separate sibling trees")

    shutil.copytree(source, target, copy_function=shutil.copy2)

    manifest_path = target / "metadata/score_manifest.json"
    manifest = read_json(manifest_path)
    excluded = [record for record in manifest if is_mixed(record)]
    active = [record for record in manifest if not is_mixed(record)]
    excluded_paths = {str(record["relative_path"]) for record in excluded}

    quarantine = target / "quarantine/mixed-instrument"
    for record in excluded:
        relative = Path(record["relative_path"])
        current = target / relative
        destination = quarantine / relative
        if not current.is_file():
            raise RuntimeError(f"expected mixed-instrument PDF is missing: {current}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(current, destination)

    write_json(manifest_path, active)
    write_json(quarantine / "manifest.json", excluded)

    counts = Counter(str(record["work_id"]) for record in active)
    catalog_path = target / "metadata/catalog.json"
    catalog = read_json(catalog_path)
    for work in catalog:
        work["score_file_count"] = counts.get(str(work["work_id"]), 0)
    write_json(catalog_path, catalog)
    filter_status_log(target / "logs/download_status.csv", excluded_paths)

    build_script = target / "scripts/build_library.py"
    subprocess.run([sys.executable, str(build_script), "render"], cwd=target, check=True)
    verification = subprocess.run(
        [sys.executable, str(build_script), "verify"],
        cwd=target,
        check=False,
        capture_output=True,
        text=True,
    )
    if verification.returncode:
        raise RuntimeError(f"target verification failed:\n{verification.stdout}\n{verification.stderr}")

    return {
        "source": str(source),
        "target": str(target),
        "active_pdf_records": len(active),
        "quarantined_mixed_records": len(excluded),
        "verification": json.loads(verification.stdout),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(args.source, args.target), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
