#!/usr/bin/env python
"""Freeze IMSLP's current acoustic-guitar chamber categories into a config."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from imslp_library.client import ImslpClient
from imslp_library.mixed_categories import FAMILY_ZH, classify_mixed_category


def read_pure_names(path: Path) -> frozenset[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(item["name"] for item in payload["categories"])


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def discover(pure_config: Path) -> dict[str, object]:
    pure_names = read_pure_names(pure_config)
    client = ImslpClient(
        user_agent="imslp-guitar-chamber-library/1.0 (category discovery)"
    )
    categories = []
    exclusions: Counter[str] = Counter()
    for category in client.allcategories(prefix="For"):
        if "guitar" not in category.name.casefold():
            continue
        selected, reason = classify_mixed_category(
            category, pure_category_names=pure_names
        )
        if selected is None:
            exclusions[reason or "unknown"] += 1
        else:
            categories.append(selected)
    categories.sort(key=lambda item: (item.display_group, item.name.casefold()))
    return {
        "schema_version": 1,
        "version": datetime.now().astimezone().strftime("%Y-%m-%d.1"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "https://imslp.org/api.php",
        "scope": {
            "description": "Classical/acoustic guitar with other instruments; instrumental chamber music only",
            "includes_original_and_arrangement_categories": True,
            "excludes": [
                "pure guitar categories already in config/categories.json",
                "voice and chorus",
                "electric, bass, Hawaiian, steel and slide guitar",
                "orchestra and other large ensembles",
                "electronics, synthesizer and tape",
                "alternative solo-instrument categories",
            ],
        },
        "display_groups": FAMILY_ZH,
        "category_count": len(categories),
        "page_count_sum": sum(item.page_count for item in categories),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "categories": [item.as_dict() for item in categories],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pure-config", type=Path, default=Path("config/categories.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("config/mixed_categories.json")
    )
    args = parser.parse_args()
    payload = discover(args.pure_config)
    write_json_atomic(args.output, payload)
    print(json.dumps({
        "output": str(args.output),
        "category_count": payload["category_count"],
        "page_count_sum": payload["page_count_sum"],
        "groups": dict(Counter(
            item["display_group"] for item in payload["categories"]
        )),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
