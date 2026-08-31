#!/usr/bin/env python
"""Render the offline master index from completed category directories."""

from __future__ import annotations

import argparse
import html
import json
import re
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def valid_pdf(path: Path, expected_size: int | None = None) -> bool:
    if not path.is_file() or path.stat().st_size < 5:
        return False
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            return False
    return not expected_size or path.stat().st_size == expected_size


def record_expected_size(record: dict) -> int | None:
    return record.get("download_expected_size") or record.get("expected_size")


def category_info(name: str) -> dict:
    arrangement = name.casefold().endswith("(arr)")
    kind = "改编" if arrangement else "原作"
    regular = re.fullmatch(r"For (?:(\d+) guitars?|guitar)(?: \(arr\))?", name, re.IGNORECASE)
    if regular:
        count = int(regular.group(1)) if regular.group(1) else 1
        if count == 1:
            family = "独奏吉他"
        elif count == 2:
            family = "吉他二重奏"
        elif count == 3:
            family = "吉他三重奏"
        elif count == 4:
            family = "吉他四重奏"
        else:
            family = "多把吉他"
        return {"zh": f"{count}把吉他·{kind}", "family": family, "sort": (count, 0, arrangement, name.casefold())}

    extended = re.fullmatch(r"For (\d+)[ -]string guitar(?: \(arr\))?", name, re.IGNORECASE)
    if extended:
        strings = int(extended.group(1))
        return {
            "zh": f"{strings}弦吉他·{kind}",
            "family": "扩展弦制独奏",
            "sort": (1, 1, strings, arrangement, name.casefold()),
        }

    flexible = re.fullmatch(r"For (\d+) and (\d+) guitars(?: \(arr\))?", name, re.IGNORECASE)
    if flexible:
        low, high = map(int, flexible.groups())
        return {
            "zh": f"{low}或{high}把吉他·{kind}",
            "family": "灵活编制",
            "sort": (low, 2, high, arrangement, name.casefold()),
        }

    if re.fullmatch(r"For guitar ensemble(?: \(arr\))?", name, re.IGNORECASE):
        return {"zh": f"吉他合奏·{kind}", "family": "吉他合奏与乐团", "sort": (90, 0, arrangement, name.casefold())}
    if re.fullmatch(r"For guitar orchestra(?: \(arr\))?", name, re.IGNORECASE):
        return {"zh": f"吉他乐团·{kind}", "family": "吉他合奏与乐团", "sort": (91, 0, arrangement, name.casefold())}
    return {"zh": "纯吉他分类", "family": "其他分类", "sort": (99, 0, arrangement, name.casefold())}


def category_sort_key(name: str) -> tuple:
    return category_info(name)["sort"]


def category_zh(name: str) -> str:
    return category_info(name)["zh"]


def load_categories(root: Path) -> list[dict]:
    categories = []
    for directory in sorted(root.iterdir(), key=lambda path: category_sort_key(path.name)):
        if not directory.is_dir() or not directory.name.startswith("For "):
            continue
        catalog_path = directory / "metadata/catalog.json"
        manifest_path = directory / "metadata/score_manifest.json"
        if not catalog_path.is_file() or not manifest_path.is_file():
            continue
        works = read_json(catalog_path)
        manifest = read_json(manifest_path)
        files_by_work: dict[str, list[dict]] = defaultdict(list)
        valid_paths = set()
        for record in manifest:
            files_by_work[str(record["work_id"])].append(record)
            if valid_pdf(directory / record["relative_path"], record_expected_size(record)):
                valid_paths.add(record["relative_path"])
        categories.append(
            {
                "name": directory.name,
                "name_zh": category_zh(directory.name),
                "family": category_info(directory.name)["family"],
                "directory": directory,
                "works": works,
                "manifest": manifest,
                "files_by_work": files_by_work,
                "valid_paths": valid_paths,
                "downloaded": len(valid_paths),
            }
        )
    return categories


def render(root: Path) -> dict:
    root = root.resolve()
    categories = load_categories(root)
    total_works = sum(len(item["works"]) for item in categories)
    total_files = sum(len(item["manifest"]) for item in categories)
    total_downloaded = sum(item["downloaded"] for item in categories)

    parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>IMSLP 纯吉他总乐谱库</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:1280px;margin:0 auto;padding:32px 24px;color:#202124;background:#faf9f6}",
        "h1{margin-bottom:.25rem}.meta{color:#5f6368;margin-bottom:1.2rem}.nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;margin:18px 0 24px}",
        ".card{display:block;background:white;border:1px solid #e2e0dc;border-radius:10px;padding:14px;text-decoration:none;color:#202124}.card:hover{border-color:#7998bb}.card b{display:block}.card span{color:#5f6368;font-size:14px}",
        "#search{width:100%;box-sizing:border-box;padding:12px 14px;font-size:16px;border:1px solid #c8c8c8;border-radius:8px;background:white;position:sticky;top:8px;z-index:2}",
        "details{background:white;border:1px solid #e2e0dc;border-radius:10px;margin:14px 0;padding:8px 14px}summary{font-size:20px;font-weight:650;cursor:pointer;padding:8px 0}",
        ".work{padding:11px 4px;border-top:1px solid #eceae6}.en{font-weight:650}.zh{color:#3c4043;margin-top:3px}.composer{font-size:14px;color:#5f6368;margin-top:3px}",
        ".links{margin-top:6px;font-size:14px}a{color:#1457a6;text-decoration:none}a:hover{text-decoration:underline}.pending{color:#9a6700}",
        "</style></head><body>",
        "<h1>IMSLP 纯吉他总乐谱库</h1>",
        f'<div class="meta">分类 {len(categories)} · 作品记录 {total_works} · PDF 记录 {total_files} · 已下载 {total_downloaded} · 更新于 {datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")}</div>',
    ]
    grouped_categories: dict[str, list[dict]] = defaultdict(list)
    for item in categories:
        grouped_categories[item["family"]].append(item)
    for family, family_items in grouped_categories.items():
        parts.extend([f'<section class="nav-group"><h2>{html.escape(family)}</h2>', '<div class="nav">'])
        for item in family_items:
            href = urllib.parse.quote(f'{item["name"]}/index.html', safe="/._-~()")
            parts.append(
                f'<a class="card" href="{href}"><b>{html.escape(item["name"])}｜{html.escape(item["name_zh"])}</b>'
                f'<span>作品 {len(item["works"])} · PDF {len(item["manifest"])} · 已下载 {item["downloaded"]}</span></a>'
            )
        parts.extend(["</div>", "</section>"])
    parts.extend(
        [
            '<input id="search" type="search" placeholder="跨分类搜索作曲家、英文名或中文名……" aria-label="搜索总目录">',
            '<main id="catalog">',
        ]
    )

    for item in categories:
        category_search = f'{item["name"]} {item["name_zh"]}'.casefold()
        parts.append(
            f'<details open class="category" data-category="{html.escape(category_search)}">'
            f'<summary>{html.escape(item["name"])}｜{html.escape(item["name_zh"])}</summary>'
        )
        for work in item["works"]:
            composer = work.get("composer", "Unknown")
            composer_zh = work.get("composer_zh", f"暂无中译（原名：{composer}）")
            title_en = work.get("title_en", work.get("page_title", "Untitled"))
            title_zh = work.get("title_zh", f"暂无通行中译（原题：{title_en}）")
            links = []
            for record in item["files_by_work"].get(str(work["work_id"]), []):
                if record["relative_path"] in item["valid_paths"]:
                    href = urllib.parse.quote(
                        f'{item["name"]}/{record["relative_path"]}', safe="/._-~()"
                    )
                    label = record.get("description") or record["filename"]
                    links.append(f'<a href="{href}">{html.escape(label)}</a>')
            score_html = "；".join(links) if links else '<span class="pending">待下载或无合格 PDF</span>'
            search = f'{item["name"]} {item["name_zh"]} {composer} {composer_zh} {title_en} {title_zh}'.casefold()
            parts.extend(
                [
                    f'<article class="work" data-search="{html.escape(search)}">',
                    f'<div class="en">{html.escape(title_en)}</div>',
                    f'<div class="zh">{html.escape(title_zh)}</div>',
                    f'<div class="composer">{html.escape(composer)}｜{html.escape(composer_zh)}</div>',
                    f'<div class="links"><a href="{html.escape(work["imslp_url"])}">IMSLP 原页</a> · 乐谱：{score_html}</div>',
                    "</article>",
                ]
            )
        parts.append("</details>")
    parts.extend(
        [
            "</main>",
            "<script>",
            "const q=document.querySelector('#search');q.addEventListener('input',()=>{const s=q.value.trim().toLocaleLowerCase();document.querySelectorAll('.work').forEach(w=>w.hidden=s&&!w.dataset.search.includes(s));document.querySelectorAll('.category').forEach(d=>{const n=[...d.querySelectorAll('.work')].some(w=>!w.hidden);d.hidden=s&&!n&&!d.dataset.category.includes(s);if(s&&!d.hidden)d.open=true;});});",
            "</script></body></html>",
        ]
    )
    (root / "index.html").write_text("\n".join(parts) + "\n", encoding="utf-8")
    return {
        "categories": len(categories),
        "works": total_works,
        "pdf_records": total_files,
        "downloaded": total_downloaded,
        "index": str(root / "index.html"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(render(args.root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
