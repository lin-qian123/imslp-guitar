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


MIXED_CATEGORY_LOOKUP: dict[str, dict] = {}
MIXED_FAMILY_ORDER = {
    "strings": 100,
    "woodwinds": 110,
    "brass": 120,
    "keyboard_reed": 130,
    "plucked": 140,
    "percussion": 150,
    "mixed_chamber": 160,
}


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
    mixed = MIXED_CATEGORY_LOOKUP.get(name)
    if mixed:
        family_key = mixed["display_group"]
        return {
            "zh": mixed["name_zh"],
            "family": mixed["family_zh"],
            "sort": (
                MIXED_FAMILY_ORDER.get(family_key, 199),
                name.casefold().endswith("(arr)"),
                name.casefold(),
            ),
        }
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
    global MIXED_CATEGORY_LOOKUP
    pure_path = root / "config/categories.json"
    pure_names = {
        item["name"] for item in read_json(pure_path).get("categories", [])
    } if pure_path.is_file() else set()
    mixed_path = root / "config/mixed_categories.json"
    if mixed_path.is_file():
        payload = read_json(mixed_path)
        groups = payload.get("display_groups", {})
        MIXED_CATEGORY_LOOKUP = {
            item["name"]: {**item, "family_zh": groups[item["display_group"]]}
            for item in payload.get("categories", [])
        }
    allowed_names = pure_names | set(MIXED_CATEGORY_LOOKUP)
    categories = []
    for directory in sorted(root.iterdir(), key=lambda path: category_sort_key(path.name)):
        if not directory.is_dir() or not directory.name.startswith("For "):
            continue
        if allowed_names and directory.name not in allowed_names:
            continue
        catalog_path = directory / "metadata/catalog.json"
        manifest_path = directory / "metadata/score_manifest.json"
        if not catalog_path.is_file() or not manifest_path.is_file():
            continue
        works = read_json(catalog_path)
        manifest = read_json(manifest_path)
        valid_paths = set()
        for record in manifest:
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
                "valid_paths": valid_paths,
                "downloaded": len(valid_paths),
            }
        )
    return categories


def render(root: Path) -> dict:
    root = root.resolve()
    categories = load_categories(root)
    total_works = sum(len(item["works"]) for item in categories)
    unique_work_ids = {
        str(work["work_id"]) for item in categories for work in item["works"]
    }
    total_files = sum(len(item["manifest"]) for item in categories)
    total_downloaded = sum(item["downloaded"] for item in categories)
    search_records = []
    for item in categories:
        category_href = urllib.parse.quote(
            f'{item["name"]}/index.html', safe="/._-~()"
        )
        for work in item["works"]:
            title_en = work.get("title_en", work.get("page_title", "Untitled"))
            title_zh = work.get("title_zh", f"暂无通行中译（原题：{title_en}）")
            composer = work.get("composer", "Unknown")
            composer_zh = work.get("composer_zh", f"暂无中译（原名：{composer}）")
            search_records.append({
                "t": title_en,
                "z": title_zh,
                "c": composer,
                "cz": composer_zh,
                "n": item["name"],
                "nz": item["name_zh"],
                "h": category_href,
                "u": work.get("imslp_url", ""),
                "s": f"{title_en} {title_zh} {composer} {composer_zh}".casefold(),
            })
    search_json = json.dumps(
        search_records, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")

    parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>IMSLP 吉他总乐谱库</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:1280px;margin:0 auto;padding:32px 24px;color:#202124;background:#faf9f6}",
        "h1{margin-bottom:.25rem}.meta{color:#5f6368;margin-bottom:1.2rem}.search-shell{position:sticky;top:8px;z-index:3;background:rgba(250,249,246,.94);backdrop-filter:blur(10px);padding:4px 0 12px}.search-note{color:#6b6f73;font-size:13px;margin:7px 2px 0}.nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;margin:18px 0 24px}",
        ".card{display:block;background:white;border:1px solid #e2e0dc;border-radius:10px;padding:14px;text-decoration:none;color:#202124}.card:hover{border-color:#7998bb}.card b{display:block}.card span{color:#5f6368;font-size:14px}",
        "#search{width:100%;box-sizing:border-box;padding:13px 15px;font-size:16px;border:1px solid #b9bdc2;border-radius:9px;background:white;box-shadow:0 4px 18px rgba(48,52,58,.06)}",
        ".results{margin:18px 0 28px}.results-list{display:grid;gap:10px}.result{background:white;border:1px solid #dedbd5;border-left:4px solid #6f8e6b;border-radius:9px;padding:13px 15px}.result-title{font-size:17px;font-weight:680}.result-zh{color:#3c4043;margin-top:2px}.result-composer,.result-category{color:#62676c;font-size:14px;margin-top:5px}.result-links{font-size:14px;margin-top:8px}.empty{background:white;border:1px dashed #c8c4bd;border-radius:9px;padding:22px;color:#62676c;text-align:center}",
        "a{color:#1457a6;text-decoration:none}a:hover{text-decoration:underline}[hidden]{display:none!important}",
        "</style></head><body>",
        "<h1>IMSLP 吉他总乐谱库</h1>",
        f'<div class="meta">分类 {len(categories)} · 作品分类记录 {total_works} · 不重复作品 {len(unique_work_ids)} · PDF 记录 {total_files} · 已下载 {total_downloaded} · 更新于 {datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")}</div>',
        '<div class="search-shell">',
        '<input id="search" type="search" placeholder="跨分类搜索作曲家、英文名或中文名……" aria-label="搜索总目录">',
        f'<div class="search-note" id="search-status">显示全部 {len(categories)} 个分类；点击分类卡片进入独立乐谱目录。</div>',
        "</div>",
        '<section class="results" id="search-results" aria-live="polite" hidden>',
        '<div class="results-list" id="results-list"></div>',
        "</section>",
        '<main id="categories">',
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
            "</main>",
            f'<script type="application/json" id="search-data">{search_json}</script>',
            "<script>",
            "const q=document.querySelector('#search'),categories=document.querySelector('#categories'),results=document.querySelector('#search-results'),list=document.querySelector('#results-list'),status=document.querySelector('#search-status'),records=JSON.parse(document.querySelector('#search-data').textContent),limit=200;function addText(parent,className,value){const node=document.createElement('div');node.className=className;node.textContent=value;parent.append(node);}function resultNode(item){const article=document.createElement('article');article.className='result';addText(article,'result-title',item.t);addText(article,'result-zh',item.z);addText(article,'result-composer',`${item.c}｜${item.cz}`);addText(article,'result-category',`所属分类：${item.n}｜${item.nz}`);const links=document.createElement('div');links.className='result-links';const category=document.createElement('a');category.href=item.h;category.textContent='打开分类目录';links.append(category);if(item.u){links.append(document.createTextNode(' · '));const source=document.createElement('a');source.href=item.u;source.textContent='IMSLP 原页';links.append(source);}article.append(links);return article;}function rank(item,s,terms){if(!terms.every(term=>item.s.includes(term)))return null;const fields=[item.t,item.z,item.c,item.cz].map(value=>value.toLocaleLowerCase());if(fields.some(value=>value===s))return 0;if(fields.some(value=>value.startsWith(s)))return 1;return 2;}function filter(){const raw=q.value.trim(),s=raw.toLocaleLowerCase(),terms=s.split(/\\s+/).filter(Boolean);if(!s){results.hidden=true;categories.hidden=false;list.replaceChildren();status.textContent=`显示全部 ${document.querySelectorAll('.card').length} 个分类；点击分类卡片进入独立乐谱目录。`;return;}const matches=[];records.forEach(item=>{const score=rank(item,s,terms);if(score!==null)matches.push([score,item]);});matches.sort((a,b)=>a[0]-b[0]||a[1].c.localeCompare(b[1].c)||a[1].t.localeCompare(b[1].t));list.replaceChildren(...matches.slice(0,limit).map(match=>resultNode(match[1])));if(!matches.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='没有找到对应的曲名或作者，请尝试英文名、中文名或作曲家姓氏。';list.append(empty);}categories.hidden=true;results.hidden=false;status.textContent=matches.length>limit?`找到 ${matches.length} 条曲名或作者结果，显示前 ${limit} 条。`:`找到 ${matches.length} 条曲名或作者结果。`;}let timer;q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(filter,70);});",
            "</script></body></html>",
        ]
    )
    (root / "index.html").write_text("\n".join(parts) + "\n", encoding="utf-8")
    return {
        "categories": len(categories),
        "works": total_works,
        "unique_works": len(unique_work_ids),
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
