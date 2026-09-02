from __future__ import annotations

import json
import re
import runpy
from pathlib import Path

from tests.basic_helpers import ROOT


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_master_index_is_compact_and_returns_work_and_composer_results(
    tmp_path: Path,
) -> None:
    write_json(
        tmp_path / "config/categories.json",
        {"categories": [{"name": "For guitar"}]},
    )
    category_root = tmp_path / "For guitar"
    write_json(
        category_root / "metadata/catalog.json",
        [{
            "work_id": "1",
            "page_title": "Ghirlanda di varii fiori",
            "title_en": "Ghirlanda di varii fiori",
            "title_zh": "《各种鲜花的花环》",
            "composer": "Abbatessa, Giovanni Battista",
            "composer_zh": "阿巴泰莎，乔瓦尼·巴蒂斯塔",
        }],
    )
    pdf_bytes = b"%PDF-1.4\n%%EOF\n"
    pdf_relative = "scores/Abbatessa/Ghirlanda/complete.pdf"
    pdf_path = category_root / pdf_relative
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(pdf_bytes)
    write_json(category_root / "metadata/score_manifest.json", [{
        "work_id": "1",
        "relative_path": pdf_relative,
        "filename": "complete.pdf",
        "description": "Complete Score",
        "expected_size": len(pdf_bytes),
    }])
    (category_root / "index.html").write_text("category", encoding="utf-8")

    namespace = runpy.run_path(str(ROOT / "scripts/render_master_index.py"))
    result = namespace["render"](tmp_path)
    rendered = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert result["categories"] == 1
    assert rendered.index('id="search"') < rendered.index('class="nav-group"')
    assert '<details' not in rendered
    assert 'class="work"' not in rendered
    assert 'href="For%20guitar/index.html"' in rendered
    assert 'id="search-results"' in rendered
    match = re.search(
        r'<script type="application/json" id="search-data">(.*?)</script>', rendered
    )
    assert match is not None
    search_data = json.loads(match.group(1))
    assert search_data == [{
        "t": "Ghirlanda di varii fiori",
        "z": "《各种鲜花的花环》",
        "c": "Abbatessa, Giovanni Battista",
        "cz": "阿巴泰莎，乔瓦尼·巴蒂斯塔",
        "n": "For guitar",
        "nz": "1把吉他·原作",
        "h": "For%20guitar/index.html",
        "u": "",
        "p": [{
            "l": "Complete Score",
            "h": "For%20guitar/scores/Abbatessa/Ghirlanda/complete.pdf",
        }],
        "pc": 1,
        "s": (
            "ghirlanda di varii fiori 《各种鲜花的花环》 "
            "abbatessa, giovanni battista 阿巴泰莎，乔瓦尼·巴蒂斯塔"
        ),
    }]
    assert "result-title" in rendered
    assert "result-composer" in rendered
    assert "本地 PDF" in rendered
    assert "暂无有效本地 PDF" in rendered
    assert "item.p.forEach" in rendered
    assert "所属分类" in rendered
    assert "曲名或作者结果" in rendered
    assert "categories.hidden=true" in rendered
    assert "点击分类卡片进入独立乐谱目录" in rendered
