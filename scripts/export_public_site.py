#!/usr/bin/env python
"""Export the public, PDF-free IMSLP guitar search catalog.

The offline library keeps category-local PDF links.  The public site is a
separate product: it deduplicates works by IMSLP work ID, keeps every approved
category membership, and links only to canonical IMSLP work/category pages.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Mapping

from render_master_index import category_info


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "public_site/data/catalog.json"
COMPOSER_OVERRIDES = (
    PROJECT_ROOT / "metadata/translations/composer_overrides_reviewed_zh.json"
)

FAMILY_LABELS = {
    "pure": ("纯吉他", "Pure guitar"),
    "strings": ("吉他与弦乐", "Guitar & strings"),
    "woodwinds": ("吉他与木管", "Guitar & woodwinds"),
    "brass": ("吉他与铜管", "Guitar & brass"),
    "keyboard_reed": ("吉他与键盘／自由簧", "Guitar, keyboard & free reed"),
    "plucked": ("吉他与拨弦乐器", "Guitar & plucked strings"),
    "percussion": ("吉他与打击乐", "Guitar & percussion"),
    "mixed_chamber": ("吉他室内乐", "Guitar chamber music"),
}
FAMILY_ORDER = {name: index for index, name in enumerate(FAMILY_LABELS)}


class PublicExportError(ValueError):
    """Raised when local metadata is unsafe or inconsistent for publication."""


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicExportError(f"cannot read JSON: {path}") from exc


def required_text(row: Mapping[str, object], name: str, context: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PublicExportError(f"{context}: {name} must be a non-empty string")
    return value.strip()


def validate_imslp_url(value: str, context: str) -> str:
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not (host == "imslp.org" or host.endswith(".imslp.org"))
        or not parsed.path.startswith("/wiki/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PublicExportError(f"{context}: invalid IMSLP URL: {value!r}")
    return value


def load_config(path: Path) -> dict[str, object]:
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PublicExportError(f"invalid category config: {path}")
    categories = payload.get("categories")
    if not isinstance(categories, list):
        raise PublicExportError(f"category config has no list: {path}")
    return payload


def configured_categories(root: Path) -> list[dict[str, object]]:
    pure = load_config(root / "config/categories.json")
    mixed = load_config(root / "config/mixed_categories.json")
    configured: list[dict[str, object]] = []
    seen_names: set[str] = set()

    for source, family_override in ((pure, "pure"), (mixed, None)):
        for raw in source["categories"]:
            if not isinstance(raw, dict):
                raise PublicExportError("category entries must be objects")
            name = required_text(raw, "name", "category")
            if name in seen_names:
                raise PublicExportError(f"duplicate configured category: {name}")
            seen_names.add(name)
            family = family_override or required_text(raw, "display_group", name)
            if family not in FAMILY_LABELS:
                raise PublicExportError(f"{name}: unsupported display group {family!r}")
            kind = required_text(raw, "kind", name)
            if kind not in {"original", "arrangement"}:
                raise PublicExportError(f"{name}: invalid kind {kind!r}")
            name_zh = (
                category_info(name)["zh"]
                if family == "pure"
                else required_text(raw, "name_zh", name)
            )
            category_url = validate_imslp_url(
                required_text(raw, "url", name), f"category {name}"
            )
            configured.append(
                {
                    "name": name,
                    "name_zh": name_zh,
                    "kind": kind,
                    "family": family,
                    "imslp_url": category_url,
                }
            )

    configured.sort(
        key=lambda item: (
            FAMILY_ORDER[str(item["family"])],
            str(item["kind"]) == "arrangement",
            str(item["name"]).casefold(),
        )
    )
    return configured


def load_composer_overrides(root: Path) -> dict[str, str]:
    path = root / "metadata/translations/composer_overrides_reviewed_zh.json"
    if not path.is_file():
        return {}
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PublicExportError(f"invalid composer override catalog: {path}")
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise PublicExportError(f"composer override entries must be an object: {path}")
    overrides: dict[str, str] = {}
    for source, translated in entries.items():
        if not isinstance(source, str) or not source.strip():
            raise PublicExportError(f"invalid composer override source: {path}")
        if not isinstance(translated, str) or not translated.strip():
            raise PublicExportError(f"invalid composer override value for {source!r}")
        overrides[source.strip()] = translated.strip()
    return overrides


def work_identity(
    work: Mapping[str, object],
    context: str,
    composer_overrides: Mapping[str, str],
) -> tuple[dict[str, object], str]:
    work_id = required_text(work, "work_id", context)
    title_en = required_text(work, "title_en", context)
    title_zh = required_text(work, "title_zh", context)
    if not (title_zh.startswith("《") and title_zh.endswith("》")):
        raise PublicExportError(f"{context}: title_zh must use Chinese book-title marks")
    composer_en = required_text(work, "composer", context)
    observed_composer_zh = required_text(work, "composer_zh", context)
    composer_zh = composer_overrides.get(composer_en, observed_composer_zh)
    imslp_url = validate_imslp_url(
        required_text(work, "imslp_url", context), f"work {work_id}"
    )
    return ({
        "id": work_id,
        "title_en": title_en,
        "title_zh": title_zh,
        "composer_en": composer_en,
        "composer_zh": composer_zh,
        "imslp_url": imslp_url,
    }, observed_composer_zh)


def reviewed_at(root: Path) -> str:
    path = root / "metadata/translations/title_overrides_reviewed_zh.json"
    if not path.is_file():
        return ""
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise PublicExportError(f"invalid review catalog: {path}")
    value = payload.get("reviewed_at", "")
    if not isinstance(value, str):
        raise PublicExportError(f"invalid reviewed_at value: {path}")
    return value


def build_public_catalog(root: Path) -> dict[str, object]:
    root = root.resolve()
    categories = configured_categories(root)
    composer_overrides = load_composer_overrides(root)
    composer_variants: dict[str, set[str]] = defaultdict(set)
    works_by_id: dict[str, dict[str, object]] = {}
    category_record_count = 0

    for category_id, category in enumerate(categories):
        directory = root / str(category["name"])
        catalog_path = directory / "metadata/catalog.json"
        catalog = read_json(catalog_path)
        if not isinstance(catalog, list):
            raise PublicExportError(f"category catalog must be a list: {catalog_path}")
        category["id"] = category_id
        category["work_count"] = len(catalog)
        seen_in_category: set[str] = set()
        for raw_work in catalog:
            if not isinstance(raw_work, dict):
                raise PublicExportError(f"invalid work row: {catalog_path}")
            identity, observed_composer_zh = work_identity(
                raw_work, str(catalog_path), composer_overrides
            )
            work_id = str(identity["id"])
            composer_variants[str(identity["composer_en"])].add(observed_composer_zh)
            if work_id in seen_in_category:
                raise PublicExportError(
                    f"duplicate work {work_id} in category {category['name']}"
                )
            seen_in_category.add(work_id)
            existing = works_by_id.get(work_id)
            if existing is None:
                works_by_id[work_id] = {**identity, "category_ids": [category_id]}
            else:
                existing_identity = {
                    key: existing[key]
                    for key in (
                        "id",
                        "title_en",
                        "title_zh",
                        "composer_en",
                        "composer_zh",
                        "imslp_url",
                    )
                }
                if existing_identity != identity:
                    raise PublicExportError(
                        f"work {work_id} identity drift across categories"
                    )
                existing["category_ids"].append(category_id)
            category_record_count += 1

    unresolved_composers = {
        name: sorted(values)
        for name, values in composer_variants.items()
        if len(values) > 1 and name not in composer_overrides
    }
    if unresolved_composers:
        first = sorted(unresolved_composers)[0]
        raise PublicExportError(
            f"composer translation conflict for {first}: {unresolved_composers[first]}"
        )
    unknown_overrides = sorted(set(composer_overrides) - set(composer_variants))
    if unknown_overrides:
        raise PublicExportError(
            f"composer overrides do not occur in this snapshot: {unknown_overrides[0]}"
        )

    works = sorted(
        works_by_id.values(),
        key=lambda item: (
            str(item["composer_en"]).casefold(),
            str(item["title_en"]).casefold(),
            str(item["id"]),
        ),
    )
    families = [
        {"id": key, "name_zh": labels[0], "name_en": labels[1]}
        for key, labels in FAMILY_LABELS.items()
        if any(category["family"] == key for category in categories)
    ]
    pure_config = load_config(root / "config/categories.json")
    mixed_config = load_config(root / "config/mixed_categories.json")
    return {
        "schema_version": 1,
        "summary": {
            "category_count": len(categories),
            "category_record_count": category_record_count,
            "unique_work_count": len(works),
            "reviewed_at": reviewed_at(root),
        },
        "sources": {
            "imslp": "https://imslp.org/",
            "pure_category_snapshot": pure_config.get("version", ""),
            "mixed_category_snapshot": mixed_config.get("version", ""),
        },
        "integrity": {
            "composer_count": len(composer_variants),
            "composer_translation_conflicts_resolved": sum(
                len(values) > 1 for values in composer_variants.values()
            ),
            "composer_overrides_applied": len(composer_overrides),
            "works_in_multiple_categories": sum(
                len(work["category_ids"]) > 1 for work in works
            ),
            "public_score_file_links": 0,
        },
        "families": families,
        "categories": categories,
        "works": works,
    }


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def export_public_catalog(root: Path, output: Path) -> dict[str, object]:
    payload = build_public_catalog(root)
    write_json_atomic(output, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = export_public_catalog(args.root, args.output)
    print(
        json.dumps(
            {"output": str(args.output.resolve()), **payload["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
