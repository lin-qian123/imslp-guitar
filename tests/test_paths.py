from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from imslp_library.paths import (
    PathSource,
    build_path_map,
    normalize_component,
    quote_path_components,
    write_path_map,
)


def test_normalization_is_nfc_and_cleans_unsafe_characters():
    assert normalize_component("Cafe\u0301") == unicodedata.normalize("NFC", "Cafe\u0301")
    assert normalize_component("A/B\0C\nD. ") == "A_B_C_D"
    assert normalize_component(" . ") == "Untitled"


def test_component_cap_preserves_utf8_boundaries():
    mapped = normalize_component("曲" * 100)
    assert len(mapped.encode("utf-8")) <= 180
    assert mapped.encode("utf-8").decode("utf-8") == mapped


def test_ordinary_names_are_not_suffixed():
    source = PathSource(kind="work", original="普通作品", stable_id="p7")
    assert build_path_map([source])[source].mapped == "普通作品"


def test_long_ordinary_pdf_preserves_extension_within_byte_cap():
    source = PathSource(kind="file", original="曲" * 100 + ".pdf", stable_id="301")
    mapped = build_path_map([source])[source].mapped
    assert mapped.endswith(".pdf")
    assert len(mapped.encode("utf-8")) <= 180


def test_collision_suffix_is_independent_of_input_order():
    one = PathSource(kind="work", original="A/B", stable_id="p7")
    two = PathSource(kind="work", original="A_B", stable_id="p8")
    first = build_path_map([one, two])
    second = build_path_map([two, one])
    assert first == second
    assert first[one].mapped.endswith("__p7")
    assert first[two].mapped.endswith("__p8")
    assert {item.collision_reason for item in first.values()} == {"clean_or_casefold_collision"}


def test_casefold_collision_suffixes_every_member():
    upper = PathSource(kind="work", original="Etude", stable_id="p7")
    lower = PathSource(kind="work", original="etude", stable_id="p8")
    mapped = build_path_map([upper, lower])
    assert mapped[upper].mapped.endswith("__p7")
    assert mapped[lower].mapped.endswith("__p8")


def test_equal_components_in_different_kinds_are_not_a_collision():
    composer = PathSource(kind="composer", original="Same", stable_id="attribution")
    work = PathSource(kind="work", original="Same", stable_id="p7")
    mapped = build_path_map([composer, work])
    assert mapped[composer].mapped == "Same"
    assert mapped[work].mapped == "Same"


def test_collision_created_by_truncation_gets_stable_suffixes():
    one = PathSource(kind="work", original="曲" * 80 + "甲", stable_id="p7")
    two = PathSource(kind="work", original="曲" * 80 + "乙", stable_id="p8")
    mapped = build_path_map([one, two])
    assert mapped[one].mapped != mapped[two].mapped
    assert mapped[one].mapped.endswith("__p7")
    assert mapped[two].mapped.endswith("__p8")
    assert {item.collision_reason for item in mapped.values()} == {"truncation_collision"}
    assert all(len(item.mapped.encode("utf-8")) <= 180 for item in mapped.values())


def test_file_suffix_precedes_pdf_extension_and_composer_suffix_is_hashed():
    one = PathSource(kind="file", original="A/B.pdf", stable_id="301")
    two = PathSource(kind="file", original="A_B.pdf", stable_id="302")
    composer_one = PathSource(kind="composer", original="Name/Here", stable_id="ignored-1")
    composer_two = PathSource(kind="composer", original="Name_Here", stable_id="ignored-2")
    mapped = build_path_map([one, two, composer_one, composer_two])
    assert mapped[one].mapped.endswith("__f301.pdf")
    assert mapped[two].mapped.endswith("__f302.pdf")
    assert "__c" in mapped[composer_one].mapped
    assert len(mapped[composer_one].mapped.rsplit("__c", 1)[1]) == 10


def test_duplicate_source_identity_is_rejected():
    source = PathSource(kind="work", original="A", stable_id="p7")
    with pytest.raises(ValueError, match="duplicate PathSource"):
        build_path_map([source, source])


@pytest.mark.parametrize("kind,stable_id", [("work", "bad"), ("file", "f")])
def test_noncanonical_numeric_stable_ids_are_rejected(kind, stable_id):
    first = PathSource(kind=kind, original="A/B.pdf", stable_id=stable_id)
    second = PathSource(kind=kind, original="A_B.pdf", stable_id="2")
    with pytest.raises(ValueError, match="numeric"):
        build_path_map([first, second])


def test_per_component_url_quoting_preserves_separators():
    assert quote_path_components(Path("For guitar/曲 #%' one.pdf")) == (
        "For%20guitar/%E6%9B%B2%20%23%25%27%20one.pdf"
    )


def test_path_map_persists_complete_deterministic_records(tmp_path):
    sources = [
        PathSource(kind="work", original="A/B", stable_id="p7"),
        PathSource(kind="work", original="A_B", stable_id="p8"),
    ]
    mappings = build_path_map(sources)
    path = write_path_map(tmp_path, mappings)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path == tmp_path / "metadata/path_map.json"
    assert payload["schema_version"] == 1
    assert payload["model_type"] == "PathMapManifest"
    assert payload["items"] == sorted(payload["items"], key=lambda item: (item["kind"], item["stable_id"], item["original"]))
    assert {(row["original"], row["mapped"], row["kind"], row["stable_id"], row["collision_reason"]) for row in payload["items"]} == {
        (source.original, mappings[source].mapped, source.kind, source.stable_id, mappings[source].collision_reason)
        for source in sources
    }
