from __future__ import annotations

import json
import runpy
from pathlib import Path

from tests.basic_helpers import ROOT


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_master_index_is_compact_and_searches_category_cards(tmp_path: Path) -> None:
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
    write_json(category_root / "metadata/score_manifest.json", [])
    (category_root / "index.html").write_text("category", encoding="utf-8")

    namespace = runpy.run_path(str(ROOT / "scripts/render_master_index.py"))
    result = namespace["render"](tmp_path)
    rendered = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert result["categories"] == 1
    assert rendered.index('id="search"') < rendered.index('class="nav-group"')
    assert '<details' not in rendered
    assert 'class="work"' not in rendered
    assert 'href="For%20guitar/index.html"' in rendered
    assert 'data-search="' in rendered
    assert "ghirlanda di varii fiori" in rendered
    assert "阿巴泰莎" in rendered
    assert "querySelectorAll('.card')" in rendered
    assert "点击分类卡片进入独立乐谱目录" in rendered
