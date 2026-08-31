#!/usr/bin/env python
"""Fill missing reference Chinese names while preserving all source names."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


ENDPOINT = "https://clients5.google.com/translate_a/t"


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def translate_batch(values: list[str], retries: int = 3) -> list[str]:
    query = urllib.parse.urlencode(
        {
            "client": "dict-chrome-ex",
            "sl": "auto",
            "tl": "zh-CN",
            "q": values,
        },
        doseq=True,
    )
    request = urllib.request.Request(
        f"{ENDPOINT}?{query}",
        headers={"User-Agent": "Codex-IMSLP-Library/1.0", "Accept": "application/json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.load(response)
            results = [
                re.sub(r"[\x00-\x1f]+", " ", str(item[0])).strip()
                if isinstance(item, list) and item else ""
                for item in payload
            ]
            if len(results) == len(values):
                return results
        except Exception:
            if attempt + 1 == retries:
                return [""] * len(values)
            time.sleep(attempt + 1)
    return [""] * len(values)


def fill(root: Path, workers: int) -> dict:
    metadata = root.resolve() / "metadata"
    works = read_json(metadata / "catalog.json", [])
    composer_path = metadata / "composer_translations_zh.json"
    title_path = metadata / "translations_zh.json"
    composers = read_json(composer_path, {})
    titles = read_json(title_path, {})
    missing_composers = sorted(
        {work["composer"] for work in works if not str(composers.get(work["composer"], "")).strip()},
        key=str.casefold,
    )
    missing_titles = sorted(
        {work["title_en"] for work in works if not str(titles.get(work["title_en"], "")).strip()},
        key=str.casefold,
    )
    jobs = [("composer", value) for value in missing_composers] + [
        ("title", value) for value in missing_titles
    ]
    batches = [jobs[index:index + 20] for index in range(0, len(jobs), 20)]
    completed: dict[tuple[str, str], str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(translate_batch, [value for _, value in batch]): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            for key, result in zip(batch, future.result()):
                completed[key] = result
            print(f"translations: {len(completed)}/{len(jobs)}", flush=True)

    added_composers = 0
    added_titles = 0
    for (kind, source), result in completed.items():
        if not result:
            continue
        if kind == "composer":
            composers[source] = result
            added_composers += 1
        else:
            titles[source] = result if result.startswith("《") else f"《{result}》"
            added_titles += 1
    write_json(composer_path, composers)
    write_json(title_path, titles)
    write_json(
        metadata / "translation_sources.json",
        {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": "Reference machine translations; exact IMSLP English names remain authoritative and visible.",
            "service": "Google Translate public web endpoint",
            "auto_composer_names": sorted(
                source for (kind, source), result in completed.items() if kind == "composer" and result
            ),
            "auto_work_titles": sorted(
                source for (kind, source), result in completed.items() if kind == "title" and result
            ),
        },
    )
    return {
        "requested": len(jobs),
        "added_composers": added_composers,
        "added_titles": added_titles,
        "failed": sum(not result for result in completed.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(fill(args.root, args.workers), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
