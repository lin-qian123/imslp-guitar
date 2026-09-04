#!/usr/bin/env python
"""Validate the deployable IMSLP guitar site without network access."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Mapping

from export_public_site import validate_imslp_url


FORBIDDEN_KEYS = {
    "download_url",
    "verified_download_url",
    "relative_path",
    "relative_directory",
    "filename",
    "sha1_imslp",
    "sha256",
    "local_path",
}
FORBIDDEN_TEXT = (
    re.compile(r"file://", re.IGNORECASE),
    re.compile(r"/Volumes/", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])scores[/\\]", re.IGNORECASE),
    re.compile(r"\.pdf(?:$|[?#])", re.IGNORECASE),
)


class PublicSiteValidationError(ValueError):
    """Raised when a public catalog or static asset is unsafe to deploy."""


def fail(message: str) -> None:
    raise PublicSiteValidationError(message)


def check_forbidden(value: object, context: str = "catalog") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                fail(f"{context}: forbidden field {key!r}")
            check_forbidden(child, f"{context}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            check_forbidden(child, f"{context}[{index}]")
    elif isinstance(value, str):
        for pattern in FORBIDDEN_TEXT:
            if pattern.search(value):
                fail(f"{context}: forbidden public value {value!r}")


def require_list(payload: Mapping[str, object], name: str) -> list[object]:
    value = payload.get(name)
    if not isinstance(value, list):
        fail(f"{name} must be a list")
    return value


def validate_payload(payload: object) -> dict[str, int]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        fail("catalog must use schema_version 1")
    check_forbidden(payload)
    categories = require_list(payload, "categories")
    works = require_list(payload, "works")
    families = require_list(payload, "families")
    family_ids = {
        family.get("id") for family in families if isinstance(family, dict)
    }
    if len(family_ids) != len(families) or None in family_ids:
        fail("family IDs must be unique and non-empty")

    category_ids: set[int] = set()
    category_work_counts: dict[int, int] = {}
    source_links = 0
    for expected_id, raw in enumerate(categories):
        if not isinstance(raw, dict):
            fail("category rows must be objects")
        category_id = raw.get("id")
        if category_id != expected_id:
            fail("category IDs must be contiguous and ordered")
        family = raw.get("family")
        if family not in family_ids:
            fail(f"category {category_id} references an unknown family")
        if raw.get("kind") not in {"original", "arrangement"}:
            fail(f"category {category_id} has an invalid kind")
        work_count = raw.get("work_count")
        if type(work_count) is not int or work_count < 0:
            fail(f"category {category_id} has an invalid work_count")
        url = raw.get("imslp_url")
        if not isinstance(url, str):
            fail(f"category {category_id} has no IMSLP URL")
        try:
            validate_imslp_url(url, f"category {category_id}")
        except ValueError as exc:
            raise PublicSiteValidationError(str(exc)) from exc
        category_ids.add(category_id)
        category_work_counts[category_id] = work_count
        source_links += 1

    seen_work_ids: set[str] = set()
    observed_memberships: Counter[int] = Counter()
    previous_sort_key: tuple[str, str, str] | None = None
    for raw in works:
        if not isinstance(raw, dict):
            fail("work rows must be objects")
        work_id = raw.get("id")
        if not isinstance(work_id, str) or not work_id:
            fail("work IDs must be non-empty strings")
        if work_id in seen_work_ids:
            fail(f"duplicate work ID: {work_id}")
        seen_work_ids.add(work_id)
        for field in ("title_en", "title_zh", "composer_en", "composer_zh"):
            if not isinstance(raw.get(field), str) or not str(raw[field]).strip():
                fail(f"work {work_id} has an invalid {field}")
        if not str(raw["title_zh"]).startswith("《") or not str(
            raw["title_zh"]
        ).endswith("》"):
            fail(f"work {work_id} has an invalid Chinese display title")
        url = raw.get("imslp_url")
        if not isinstance(url, str):
            fail(f"work {work_id} has no IMSLP URL")
        try:
            validate_imslp_url(url, f"work {work_id}")
        except ValueError as exc:
            raise PublicSiteValidationError(str(exc)) from exc
        memberships = raw.get("category_ids")
        if (
            not isinstance(memberships, list)
            or not memberships
            or any(type(value) is not int for value in memberships)
            or len(set(memberships)) != len(memberships)
            or memberships != sorted(memberships)
            or any(value not in category_ids for value in memberships)
        ):
            fail(f"work {work_id} has invalid category memberships")
        observed_memberships.update(memberships)
        sort_key = (
            str(raw["composer_en"]).casefold(),
            str(raw["title_en"]).casefold(),
            work_id,
        )
        if previous_sort_key is not None and sort_key < previous_sort_key:
            fail("works must use deterministic composer/title ordering")
        previous_sort_key = sort_key
        source_links += 1

    if dict(observed_memberships) != category_work_counts:
        fail("category work counts do not equal work memberships")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        fail("summary must be an object")
    expected_summary = {
        "category_count": len(categories),
        "category_record_count": sum(observed_memberships.values()),
        "unique_work_count": len(works),
    }
    for key, expected in expected_summary.items():
        if summary.get(key) != expected:
            fail(f"summary {key} does not match catalog contents")
    integrity = payload.get("integrity")
    if isinstance(integrity, dict) and integrity.get("public_score_file_links") != 0:
        fail("public score-file link count must be zero")
    return {
        "categories": len(categories),
        "category_records": sum(observed_memberships.values()),
        "unique_works": len(works),
        "source_links": source_links,
        "score_file_links": 0,
    }


def validate_public_site(root: Path) -> dict[str, int]:
    root = root.resolve()
    files = [path for path in root.rglob("*") if path.is_file()]
    if any(path.is_symlink() for path in files):
        fail("public site must not contain symbolic links")
    score_files = [path for path in files if path.suffix.casefold() == ".pdf"]
    if score_files:
        fail(f"public site contains score files: {score_files[0]}")
    catalog_path = root / "data/catalog.json"
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicSiteValidationError(f"cannot read {catalog_path}") from exc
    report = validate_payload(payload)
    asset_paths = (
        root / "index.html",
        root / "assets/app.js",
        root / "assets/site.css",
    )
    for path in asset_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PublicSiteValidationError(f"cannot read public asset: {path}") from exc
        for pattern in FORBIDDEN_TEXT:
            if pattern.search(text):
                fail(f"public asset contains a forbidden value: {path}")
    script = (root / "assets/app.js").read_text(encoding="utf-8")
    if "innerHTML" in script:
        fail("public script must not inject catalog data with innerHTML")
    if 'fetch("data/catalog.json"' not in script:
        fail("public script does not load the versioned catalog")
    hero_path = root / "assets/archive-hero.webp"
    try:
        hero_header = hero_path.read_bytes()[:12]
    except OSError as exc:
        raise PublicSiteValidationError("public hero artwork is missing") from exc
    if not (hero_header.startswith(b"RIFF") and hero_header[8:12] == b"WEBP"):
        fail("public hero artwork is not a valid WebP asset")
    favicon_path = root / "assets/favicon.png"
    try:
        favicon_header = favicon_path.read_bytes()[:8]
    except OSError as exc:
        raise PublicSiteValidationError("public favicon is missing") from exc
    if favicon_header != b"\x89PNG\r\n\x1a\n":
        fail("public favicon is not a valid PNG asset")
    report["site_bytes"] = sum(path.stat().st_size for path in files)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("public_site"))
    args = parser.parse_args()
    print(json.dumps(validate_public_site(args.root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
