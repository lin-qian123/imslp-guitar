from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from tests.basic_helpers import ROOT


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def public_exporter() -> dict[str, object]:
    return runpy.run_path(str(ROOT / "scripts/export_public_site.py"))


def public_validator() -> dict[str, object]:
    return runpy.run_path(str(ROOT / "scripts/validate_public_site.py"))


def make_library(root: Path) -> None:
    write_json(
        root / "config/categories.json",
        {
            "schema_version": 1,
            "version": "2026-09-04.1",
            "categories": [
                {
                    "name": "For guitar",
                    "url": "https://imslp.org/wiki/Category:For_guitar",
                    "kind": "original",
                    "display_group": "solo",
                }
            ],
        },
    )
    write_json(
        root / "config/mixed_categories.json",
        {
            "schema_version": 1,
            "version": "2026-09-04.1",
            "display_groups": {"strings": "吉他与弦乐"},
            "categories": [
                {
                    "name": "For guitar, violin (arr)",
                    "url": "https://imslp.org/wiki/Category:For_guitar,_violin_(arr)",
                    "kind": "arrangement",
                    "display_group": "strings",
                    "name_zh": "吉他、小提琴·改编",
                }
            ],
        },
    )
    work = {
        "work_id": "42",
        "title_en": "Danses populaires roumaines, Sz.56",
        "title_zh": "《罗马尼亚民间舞曲，Sz.56》",
        "composer": "Bartók, Béla",
        "composer_zh": "贝拉·巴托克",
        "imslp_url": (
            "https://imslp.org/wiki/Romanian_Folk_Dances,_Sz.56_(Bart%C3%B3k,_B%C3%A9la)"
        ),
        "relative_directory": "scores/private/path",
        "score_file_count": 3,
    }
    write_json(root / "For guitar/metadata/catalog.json", [work])
    write_json(root / "For guitar, violin (arr)/metadata/catalog.json", [work])
    write_json(
        root / "metadata/translations/title_overrides_reviewed_zh.json",
        {"schema_version": 1, "reviewed_at": "2026-09-04", "entries": []},
    )


def test_public_catalog_deduplicates_works_and_excludes_local_paths(
    tmp_path: Path,
) -> None:
    make_library(tmp_path)
    namespace = public_exporter()

    payload = namespace["build_public_catalog"](tmp_path)

    assert payload["summary"] == {
        "category_count": 2,
        "category_record_count": 2,
        "unique_work_count": 1,
        "reviewed_at": "2026-09-04",
    }
    assert [category["id"] for category in payload["categories"]] == [0, 1]
    work = payload["works"][0]
    assert work == {
        "id": "42",
        "title_en": "Danses populaires roumaines, Sz.56",
        "title_zh": "《罗马尼亚民间舞曲，Sz.56》",
        "composer_en": "Bartók, Béla",
        "composer_zh": "贝拉·巴托克",
        "imslp_url": (
            "https://imslp.org/wiki/Romanian_Folk_Dances,_Sz.56_(Bart%C3%B3k,_B%C3%A9la)"
        ),
        "category_ids": [0, 1],
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "relative_directory" not in serialized
    assert "scores/private/path" not in serialized
    assert ".pdf" not in serialized.casefold()
    assert "file://" not in serialized.casefold()
    assert "/Volumes/" not in serialized


def test_public_catalog_fails_closed_on_cross_category_identity_drift(
    tmp_path: Path,
) -> None:
    make_library(tmp_path)
    catalog_path = tmp_path / "For guitar, violin (arr)/metadata/catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog[0]["title_zh"] = "《错误的另一个译名》"
    write_json(catalog_path, catalog)
    namespace = public_exporter()

    with pytest.raises(namespace["PublicExportError"], match="identity drift"):
        namespace["build_public_catalog"](tmp_path)


def test_public_catalog_rejects_non_imslp_work_links(tmp_path: Path) -> None:
    make_library(tmp_path)
    catalog_path = tmp_path / "For guitar/metadata/catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog[0]["imslp_url"] = "https://example.com/not-imslp"
    write_json(catalog_path, catalog)
    namespace = public_exporter()

    with pytest.raises(namespace["PublicExportError"], match="IMSLP URL"):
        namespace["build_public_catalog"](tmp_path)


def test_public_frontend_uses_imslp_links_without_pdf_links() -> None:
    index = (ROOT / "public_site/index.html").read_text(encoding="utf-8")
    script = (ROOT / "public_site/assets/app.js").read_text(encoding="utf-8")
    styles = (ROOT / "public_site/assets/site.css").read_text(encoding="utf-8")

    assert "data/catalog.json" in script
    assert "item.imslp_url" in script
    assert "category_ids" in script
    assert "normalize(\"NFKD\")" in script
    assert "URLSearchParams" in script
    assert "family-shortcut" in script
    assert 'aria-pressed' in script
    assert "innerHTML" not in script
    assert "file://" not in index + script
    assert ".pdf" not in (index + script).casefold()
    assert "本地" not in index
    assert "--ink" in styles
    assert 'src="assets/archive-hero.webp"' in index
    hero = (ROOT / "public_site/assets/archive-hero.webp").read_bytes()
    assert hero.startswith(b"RIFF") and hero[8:12] == b"WEBP"
    favicon = (ROOT / "public_site/assets/favicon.png").read_bytes()
    assert favicon.startswith(b"\x89PNG\r\n\x1a\n")


def test_public_validator_checks_counts_memberships_and_forbidden_fields(
    tmp_path: Path,
) -> None:
    make_library(tmp_path)
    payload = public_exporter()["build_public_catalog"](tmp_path)
    validator = public_validator()

    report = validator["validate_payload"](payload)

    assert report == {
        "categories": 2,
        "category_records": 2,
        "unique_works": 1,
        "source_links": 3,
        "score_file_links": 0,
    }

    payload["works"][0]["download_url"] = "https://example.test/score.pdf"
    with pytest.raises(validator["PublicSiteValidationError"], match="forbidden"):
        validator["validate_payload"](payload)
