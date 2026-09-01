#!/usr/bin/env python
"""Reconcile the noisy allcategories feed with exact local category members."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json_atomic(path: Path, payload: object) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def reconcile(root: Path, config_path: Path) -> dict[str, object]:
    payload = read_json(config_path)
    previous = payload.get("local_reconciliation", {})
    retained = []
    removed = []
    adjusted = []
    unverified = []
    for category in payload["categories"]:
        members_path = root / category["name"] / "metadata/category_members.json"
        if not members_path.is_file():
            retained.append(category)
            unverified.append(category["name"])
            continue
        members = read_json(members_path, [])
        actual = len(members) if isinstance(members, list) else 0
        listed = int(category["page_count"])
        if actual == 0 and listed > 0:
            removed.append({
                "name": category["name"],
                "allcategories_page_count": listed,
                "reason": "exact categorymembers query returned zero works",
            })
            continue
        if actual != listed:
            adjusted.append({
                "name": category["name"],
                "allcategories_page_count": listed,
                "categorymembers_page_count": actual,
            })
            category = {**category, "page_count": actual}
        retained.append(category)
    payload["categories"] = retained
    payload["category_count"] = len(retained)
    payload["page_count_sum"] = sum(int(item["page_count"]) for item in retained)
    previous_removed = previous.get("removed", []) if isinstance(previous, dict) else []
    removed_by_name = {
        item["name"]: item for item in [*previous_removed, *removed]
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    all_removed = [removed_by_name[name] for name in sorted(removed_by_name, key=str.casefold)]
    payload["local_reconciliation"] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "removed_count": len(all_removed),
        "adjusted_count": len(adjusted),
        "unverified_count": len(unverified),
        "removed": all_removed,
        "adjusted": adjusted,
        "unverified": unverified,
    }
    write_json_atomic(config_path, payload)
    return {
        "category_count": len(retained),
        "page_count_sum": payload["page_count_sum"],
        "removed_count": len(all_removed),
        "adjusted_count": len(adjusted),
        "unverified_count": len(unverified),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("config/mixed_categories.json"))
    args = parser.parse_args()
    print(json.dumps(
        reconcile(args.root.resolve(), args.config.resolve()),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
