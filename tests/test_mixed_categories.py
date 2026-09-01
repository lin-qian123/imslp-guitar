from __future__ import annotations

import runpy

from imslp_library.client import AllCategory
from imslp_library.mixed_categories import category_name_zh, classify_mixed_category
from tests.basic_helpers import ROOT


def category(name: str, pages: int = 1) -> AllCategory:
    return AllCategory(name, pages, pages, 0, 0)


def test_classifies_requested_instrument_families() -> None:
    cases = {
        "For guitar, violin": "strings",
        "For flute, guitar (arr)": "woodwinds",
        "For accordion, guitar": "keyboard_reed",
        "For guitar, mandolin": "plucked",
        "For trumpet, guitar": "brass",
        "For percussion, guitar, piano": "mixed_chamber",
    }
    for name, expected in cases.items():
        selected, reason = classify_mixed_category(category(name))
        assert reason is None
        assert selected is not None
        assert selected.display_group == expected


def test_excludes_out_of_scope_and_pure_categories() -> None:
    cases = {
        "For 2 guitars": "pure_guitar",
        "For voice, guitar": "voice_or_chorus",
        "For electric guitar, violin": "non_classical_guitar",
        "For guitar, orchestra": "large_ensemble",
        "For violin, synthesizer, guitar": "electronic_or_tape",
        "For piano or guitar (arr)": "alternative_solo",
        "For guitar or ukulele (arr)": "alternative_solo",
        "For narrator, flute, guitar": "voice_or_chorus",
    }
    for name, expected in cases.items():
        selected, reason = classify_mixed_category(category(name))
        assert selected is None
        assert reason == expected


def test_rejects_polluted_or_empty_api_categories() -> None:
    selected, reason = classify_mixed_category(category("For guitar?utm_source=x", 1))
    assert selected is None
    assert reason == "malformed_or_unrelated"
    selected, reason = classify_mixed_category(category("For flute, guitar", 0))
    assert selected is None
    assert reason == "empty"


def test_category_chinese_name_keeps_kind_visible() -> None:
    assert category_name_zh("For guitar, violin") == "吉他、小提琴·原作"
    assert category_name_zh("For flute, guitar (arr)") == "长笛、吉他·改编"
    assert category_name_zh("For guitar, mandocello") == "吉他、曼陀大提琴·原作"


def test_mixed_arrangement_heading_accepts_order_and_connector_variants(
    monkeypatch,
) -> None:
    monkeypatch.setenv("IMSLP_ALLOW_MIXED_TARGET", "1")
    monkeypatch.setenv("IMSLP_CATEGORY_NAME", "For harmonica, guitar (arr)")
    monkeypatch.setenv("IMSLP_CATEGORY_KIND", "arrangement")
    monkeypatch.setenv("IMSLP_TARGET_INSTRUMENT", "harmonica, guitar")
    monkeypatch.setenv("IMSLP_GUITAR_COUNT", "1")
    namespace = runpy.run_path(str(ROOT / "scripts/build_category_library.py"))

    matches = namespace["is_target_arrangement_heading"]
    assert matches("For Harmonica and Guitar")
    assert matches("For Guitar, Harmonica (Smith, Jane)")
    assert not matches("For Guitar")
    assert not matches("For Guitar and Harmonica (with voice)")

    matches.__globals__["TARGET_INSTRUMENT"] = "guitar, ukulele"
    assert matches("For 2 Guitars or Guitar and Ukulele (Marieh)")
    matches.__globals__["TARGET_INSTRUMENT"] = "guitar, 2 violins"
    assert matches("For 2 Violins, Guitar and Bass Instrument ad lib. (Blatt)")
    assert not matches("For Guitar and 2 Mandolins")
    matches.__globals__["TARGET_INSTRUMENT"] = "recorder, guitar"
    assert matches("For Csakan and Guitar (Composer)")
    assert not matches("For Mixed Chorus, Recorder, Guitar and Keyboard")


def test_case_only_imslp_filenames_get_unique_stable_paths(monkeypatch) -> None:
    monkeypatch.setenv("IMSLP_ALLOW_MIXED_TARGET", "1")
    namespace = runpy.run_path(str(ROOT / "scripts/build_category_library.py"))
    assign_unique_relative_paths = namespace["assign_unique_relative_paths"]
    filesystem_path_key = namespace["filesystem_path_key"]
    records = [
        {
            "relative_path": "scores/Cottin/Aubade/Chant_D'amour.pdf",
            "filename": "Chant_D'amour.pdf",
            "sha1_imslp": "b7ce919e74801ea907aac55d24bdc0cda64ed0f5",
            "download_url": "https://example.test/upper.pdf",
            "expected_size": 214648,
        },
        {
            "relative_path": "scores/Cottin/Aubade/Chant_d'amour.pdf",
            "filename": "Chant_d'amour.pdf",
            "sha1_imslp": "722970fb6d648176e5f3f56983f376e792a1dd7e",
            "download_url": "https://example.test/lower.pdf",
            "expected_size": 62132,
        },
    ]

    assign_unique_relative_paths(records)

    assert records[0]["relative_path"] == "scores/Cottin/Aubade/Chant_D'amour.pdf"
    assert records[1]["relative_path"].endswith("Chant_d'amour__imslp_722970fb.pdf")
    assert len({filesystem_path_key(record["relative_path"]) for record in records}) == 2
