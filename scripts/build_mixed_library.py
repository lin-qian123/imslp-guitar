#!/usr/bin/env python
"""Build, resume, translate, download and verify guitar chamber categories."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Iterable

from fill_translations import translate_batch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILDER = PROJECT_ROOT / "scripts/build_category_library.py"
MASTER_RENDERER = PROJECT_ROOT / "scripts/render_master_index.py"
DEFAULT_CONFIG = PROJECT_ROOT / "config/mixed_categories.json"


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


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


def load_config(path: Path) -> dict[str, object]:
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError(f"Invalid mixed category config: {path}")
    categories = payload.get("categories")
    if not isinstance(categories, list) or not categories:
        raise RuntimeError(f"Mixed category config has no categories: {path}")
    names = [item.get("name") for item in categories if isinstance(item, dict)]
    if len(names) != len(categories) or len(set(names)) != len(names):
        raise RuntimeError("Mixed category config names are invalid or duplicated")
    return payload


def choose_categories(
    config: dict[str, object], names: list[str], limit: int | None,
    kind: str | None, library_root: Path, zero_manifest_only: bool,
) -> list[dict[str, object]]:
    categories = list(config["categories"])
    if kind:
        categories = [item for item in categories if item["kind"] == kind]
    if names:
        requested = set(names)
        available = {str(item["name"]) for item in categories}
        missing = sorted(requested - available)
        if missing:
            raise RuntimeError(f"Unknown configured category: {missing[0]}")
        categories = [item for item in categories if item["name"] in requested]
    if zero_manifest_only:
        categories = [
            item for item in categories
            if read_json(
                category_root(library_root, item) / "metadata/score_manifest.json"
            ) == []
            and bool(read_json(
                category_root(library_root, item) / "metadata/catalog.json", []
            ))
        ]
    if limit is not None:
        if limit < 1:
            raise RuntimeError("--limit-categories must be positive")
        categories = categories[:limit]
    return categories


def category_root(library_root: Path, category: dict[str, object]) -> Path:
    return library_root / str(category["name"])


def builder_environment(library_root: Path, category: dict[str, object]) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "IMSLP_LIBRARY_ROOT": str(category_root(library_root, category)),
        "IMSLP_PAGE_CACHE_DIR": str(library_root / "metadata/.cache/pages"),
        "IMSLP_CATEGORY_NAME": str(category["name"]),
        "IMSLP_CATEGORY_KIND": str(category["kind"]),
        "IMSLP_GUITAR_COUNT": str(category["guitar_count"]),
        "IMSLP_TARGET_INSTRUMENT": str(category["target_instrument"]),
        "IMSLP_PDF_KIND_LABEL": f"{category['name_zh']}目标编制 PDF",
        "IMSLP_ALLOW_MIXED_TARGET": "1",
    })
    return env


def run_builder(
    library_root: Path,
    category: dict[str, object],
    arguments: list[str],
    *,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), *arguments],
        cwd=PROJECT_ROOT,
        env=builder_environment(library_root, category),
        text=True,
        capture_output=capture,
        check=False,
    )


def metadata_complete(root: Path, expected_works: int) -> bool:
    catalog = read_json(root / "metadata/catalog.json")
    manifest = read_json(root / "metadata/score_manifest.json")
    return (
        isinstance(catalog, list)
        and len(catalog) == expected_works
        and isinstance(manifest, list)
        and (root / "index.html").is_file()
    )


def write_run_report(library_root: Path, command: str, records: list[dict[str, object]]) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = library_root / f"metadata/mixed_runs/{stamp}-{command}.json"
    write_json_atomic(path, {
        "command": command,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category_count": len(records),
        "status_counts": dict(Counter(str(item["status"]) for item in records)),
        "records": records,
    })
    return path


def build_metadata(
    library_root: Path,
    categories: list[dict[str, object]],
    *,
    refresh: bool,
) -> int:
    records = []
    for number, category in enumerate(categories, start=1):
        name = str(category["name"])
        root = category_root(library_root, category)
        if not refresh and metadata_complete(root, int(category["page_count"])):
            status, detail = "skipped_complete", "existing metadata matches frozen page count"
        else:
            print(f"metadata {number}/{len(categories)}: {name}", file=sys.stderr, flush=True)
            args = ["metadata", "--no-render"]
            if refresh:
                args.append("--refresh")
            result = run_builder(library_root, category, args)
            if result.returncode == 0:
                render = run_builder(library_root, category, ["render"])
                result = render if render.returncode else result
            status = "completed" if result.returncode == 0 else "failed"
            detail = f"exit={result.returncode}"
        records.append({"name": name, "status": status, "detail": detail})
    report = write_run_report(library_root, "metadata", records)
    print(json.dumps({"report": str(report), "status_counts": dict(Counter(
        item["status"] for item in records
    ))}, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] == "failed" for item in records) else 0


def manifest_records(library_root: Path, categories: Iterable[dict[str, object]]):
    for category in categories:
        root = category_root(library_root, category)
        manifest = read_json(root / "metadata/score_manifest.json", [])
        if isinstance(manifest, list):
            yield category, root, manifest


def expected_remaining_bytes(
    library_root: Path, categories: Iterable[dict[str, object]]
) -> int:
    total = 0
    for _, root, manifest in manifest_records(library_root, categories):
        for record in manifest:
            path = root / record["relative_path"]
            expected = record.get("download_expected_size") or record.get("expected_size") or 0
            if not path.is_file():
                total += int(expected)
    return total


def download_scores(
    library_root: Path,
    categories: list[dict[str, object]],
    *,
    workers: int,
    retry_failures: bool,
) -> int:
    remaining = expected_remaining_bytes(library_root, categories)
    free = shutil.disk_usage(library_root).free
    required = int(remaining * 1.20) + 1024 * 1024 * 1024
    if free < required:
        raise RuntimeError(
            f"Insufficient free space: remaining={remaining}, required_with_headroom={required}, free={free}"
        )
    records = []
    for number, category in enumerate(categories, start=1):
        name = str(category["name"])
        root = category_root(library_root, category)
        if not (root / "metadata/score_manifest.json").is_file():
            records.append({"name": name, "status": "missing_metadata", "detail": "run metadata first"})
            continue
        print(f"download {number}/{len(categories)}: {name}", file=sys.stderr, flush=True)
        args = ["download", "--workers", str(max(1, workers)), "--no-render"]
        if retry_failures:
            args.append("--retry-failures")
        result = run_builder(library_root, category, args)
        status = "completed" if result.returncode == 0 else "failed"
        records.append({"name": name, "status": status, "detail": f"exit={result.returncode}"})
    report = write_run_report(library_root, "download", records)
    print(json.dumps({
        "report": str(report),
        "expected_remaining_bytes_at_start": remaining,
        "free_bytes_at_start": free,
        "status_counts": dict(Counter(item["status"] for item in records)),
    }, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] in {"failed", "missing_metadata"} for item in records) else 0


def merge_seed_mapping(paths: Iterable[Path]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in sorted(paths, key=lambda item: item.as_posix().casefold()):
        payload = read_json(path, {})
        if not isinstance(payload, dict):
            continue
        for source, translated in payload.items():
            if isinstance(source, str) and isinstance(translated, str) and translated.strip():
                merged.setdefault(source, translated.strip())
    return merged


def translate_catalogs(
    library_root: Path,
    categories: list[dict[str, object]],
    *,
    workers: int,
) -> int:
    metadata_root = library_root / "metadata"
    composer_global = metadata_root / "mixed_composer_translations_zh.json"
    title_global = metadata_root / "mixed_title_translations_zh.json"
    composer_paths = list(library_root.glob("For */metadata/composer_translations_zh.json"))
    title_paths = list(library_root.glob("For */metadata/translations_zh.json"))
    composers = merge_seed_mapping([composer_global, *composer_paths])
    titles = merge_seed_mapping([title_global, *title_paths])

    catalogs: dict[str, list[dict[str, object]]] = {}
    for category in categories:
        root = category_root(library_root, category)
        catalog = read_json(root / "metadata/catalog.json", [])
        if isinstance(catalog, list):
            catalogs[str(category["name"])] = catalog
    wanted_composers = sorted({
        str(work["composer"]) for catalog in catalogs.values() for work in catalog
    }, key=str.casefold)
    wanted_titles = sorted({
        str(work["title_en"]) for catalog in catalogs.values() for work in catalog
    }, key=str.casefold)
    jobs = [
        ("composer", value) for value in wanted_composers if not composers.get(value, "").strip()
    ] + [
        ("title", value) for value in wanted_titles if not titles.get(value, "").strip()
    ]
    batches = [jobs[offset:offset + 20] for offset in range(0, len(jobs), 20)]
    completed: dict[tuple[str, str], str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(translate_batch, [value for _, value in batch]): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            for identity, result in zip(batch, future.result(), strict=True):
                completed[identity] = result.strip()
            if len(completed) % 200 < 20:
                print(f"translations: {len(completed)}/{len(jobs)}", file=sys.stderr, flush=True)
    for (kind, source), translated in completed.items():
        if not translated:
            continue
        if kind == "composer":
            composers[source] = translated
        else:
            titles[source] = translated if translated.startswith("《") else f"《{translated}》"
    write_json_atomic(composer_global, composers)
    write_json_atomic(title_global, titles)

    render_failures = []
    for category in categories:
        name = str(category["name"])
        root = category_root(library_root, category)
        catalog = catalogs.get(name)
        if catalog is None:
            continue
        category_composers = sorted({str(work["composer"]) for work in catalog})
        category_titles = sorted({str(work["title_en"]) for work in catalog})
        write_json_atomic(
            root / "metadata/composer_translations_zh.json",
            {key: composers[key] for key in category_composers if key in composers},
        )
        write_json_atomic(
            root / "metadata/translations_zh.json",
            {key: titles[key] for key in category_titles if key in titles},
        )
        result = run_builder(library_root, category, ["render"])
        if result.returncode:
            render_failures.append(name)
    summary = {
        "requested": len(jobs),
        "translated": sum(bool(value) for value in completed.values()),
        "failed": sum(not value for value in completed.values()),
        "composer_mapping_count": len(composers),
        "title_mapping_count": len(titles),
        "render_failures": render_failures,
    }
    write_json_atomic(metadata_root / "mixed_translation_coverage.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] or render_failures else 0


def render_master(library_root: Path) -> int:
    result = subprocess.run(
        [sys.executable, str(MASTER_RENDERER), str(library_root)],
        cwd=PROJECT_ROOT,
        text=True,
        check=False,
    )
    return result.returncode


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_library(library_root: Path, categories: list[dict[str, object]]) -> int:
    category_reports = []
    totals = Counter()
    for category in categories:
        name = str(category["name"])
        root = category_root(library_root, category)
        catalog = read_json(root / "metadata/catalog.json")
        manifest = read_json(root / "metadata/score_manifest.json")
        errors = []
        if not isinstance(catalog, list) or len(catalog) != int(category["page_count"]):
            errors.append("catalog_missing_or_page_count_mismatch")
            catalog = catalog if isinstance(catalog, list) else []
        if not isinstance(manifest, list):
            errors.append("manifest_missing")
            manifest = []
        composer_map = read_json(root / "metadata/composer_translations_zh.json", {})
        title_map = read_json(root / "metadata/translations_zh.json", {})
        if any(not composer_map.get(work.get("composer"), "").strip() for work in catalog):
            errors.append("missing_composer_translation")
        if any(not title_map.get(work.get("title_en"), "").strip() for work in catalog):
            errors.append("missing_title_translation")
        missing = invalid = sha_mismatch = 0
        for record in manifest:
            path = root / record["relative_path"]
            expected_size = record.get("download_expected_size") or record.get("expected_size")
            expected_sha1 = record.get("download_sha1") or record.get("sha1_imslp")
            if not path.is_file():
                missing += 1
                continue
            if path.stat().st_size < 5 or path.open("rb").read(5) != b"%PDF-":
                invalid += 1
            elif expected_size and path.stat().st_size != int(expected_size):
                invalid += 1
            elif expected_sha1 and file_sha1(path) != expected_sha1:
                sha_mismatch += 1
        if missing:
            errors.append(f"missing_pdfs:{missing}")
        if invalid:
            errors.append(f"invalid_pdfs:{invalid}")
        if sha_mismatch:
            errors.append(f"sha1_mismatches:{sha_mismatch}")
        if not (root / "README.md").is_file() or not (root / "index.html").is_file():
            errors.append("catalog_outputs_missing")
        totals["categories"] += 1
        totals["works"] += len(catalog)
        totals["pdf_records"] += len(manifest)
        totals["downloaded"] += len(manifest) - missing - invalid - sha_mismatch
        category_reports.append({
            "name": name,
            "works": len(catalog),
            "pdf_records": len(manifest),
            "valid_pdfs": len(manifest) - missing - invalid - sha_mismatch,
            "errors": errors,
        })
    root_index = library_root / "index.html"
    if not root_index.is_file():
        totals["root_index_missing"] = 1
    failed = [item for item in category_reports if item["errors"]]
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "totals": dict(totals),
        "failed_category_count": len(failed),
        "categories": category_reports,
    }
    path = library_root / "metadata/mixed_verification.json"
    write_json_atomic(path, payload)
    print(json.dumps({
        "report": str(path),
        "totals": dict(totals),
        "failed_category_count": len(failed),
        "first_failures": failed[:20],
    }, ensure_ascii=False, indent=2))
    return 1 if failed or totals.get("root_index_missing") else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("metadata", "download", "translate", "render", "verify", "all")
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--limit-categories", type=int)
    parser.add_argument("--kind", choices=("original", "arrangement"))
    parser.add_argument("--zero-manifest-only", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    library_root = args.root.expanduser().resolve()
    config = load_config(args.config.expanduser().resolve())
    categories = choose_categories(
        config, args.category, args.limit_categories, args.kind,
        library_root, args.zero_manifest_only,
    )
    if args.command == "metadata":
        return build_metadata(library_root, categories, refresh=args.refresh)
    if args.command == "download":
        return download_scores(
            library_root, categories, workers=args.workers,
            retry_failures=args.retry_failures,
        )
    if args.command == "translate":
        return translate_catalogs(library_root, categories, workers=args.workers)
    if args.command == "render":
        for category in categories:
            if (category_root(library_root, category) / "metadata/catalog.json").is_file():
                result = run_builder(library_root, category, ["render"])
                if result.returncode:
                    return result.returncode
        return render_master(library_root)
    if args.command == "verify":
        return verify_library(library_root, categories)

    for action in (
        lambda: build_metadata(library_root, categories, refresh=args.refresh),
        lambda: translate_catalogs(library_root, categories, workers=args.workers),
        lambda: download_scores(
            library_root, categories, workers=args.workers,
            retry_failures=args.retry_failures,
        ),
        lambda: translate_catalogs(library_root, categories, workers=args.workers),
        lambda: render_master(library_root),
        lambda: verify_library(library_root, categories),
    ):
        code = action()
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
