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


def test_suffix_collision_closes_iteratively_and_is_order_independent():
    cleaned_one = PathSource(kind="work", original="A/B", stable_id="p7")
    cleaned_two = PathSource(kind="work", original="A_B", stable_id="p8")
    suffix_occupant = PathSource(kind="work", original="A_B__p7", stable_id="p9")
    sources = [cleaned_one, cleaned_two, suffix_occupant]
    forward = build_path_map(sources)
    reverse = build_path_map(list(reversed(sources)))
    assert forward == reverse
    assert len({item.mapped.casefold() for item in forward.values()}) == 3
    assert forward[cleaned_one].mapped.endswith("__p7")
    assert forward[cleaned_two].mapped.endswith("__p8")
    assert forward[suffix_occupant].mapped.endswith("__p9")
    assert forward[suffix_occupant].collision_reason == "suffix_collision"
    assert all(len(item.mapped.encode("utf-8")) <= 180 for item in forward.values())


def test_unresolvable_duplicate_stable_suffix_is_rejected():
    one = PathSource(kind="work", original="A/B", stable_id="p7")
    two = PathSource(kind="work", original="A_B", stable_id="7")
    with pytest.raises(ValueError, match="duplicate stable suffix"):
        build_path_map([one, two])


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


@pytest.mark.parametrize("kind,stable_id", [("work", "p007"), ("work", "0"), ("file", "f00"), ("file", "00")])
def test_stable_numeric_ids_must_be_canonical_positive_decimals(kind, stable_id):
    first = PathSource(kind=kind, original="A/B.pdf", stable_id=stable_id)
    second = PathSource(kind=kind, original="A_B.pdf", stable_id="2")
    with pytest.raises(ValueError, match="canonical positive"):
        build_path_map([first, second])


@pytest.mark.parametrize("kind,stable_id", [("work", "p007"), ("file", "f00")])
def test_ordinary_unsuffixed_sources_still_require_canonical_ids(kind, stable_id):
    with pytest.raises(ValueError, match="canonical positive"):
        build_path_map([PathSource(kind=kind, original="Unique.pdf", stable_id=stable_id)])


def test_per_component_url_quoting_preserves_separators():
    assert quote_path_components(Path("For guitar/曲 #%' one.pdf")) == (
        "For%20guitar/%E6%9B%B2%20%23%25%27%20one.pdf"
    )


@pytest.mark.parametrize("unsafe", ["", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "a/"])
def test_url_quoting_rejects_ambiguous_or_unsafe_raw_paths(unsafe):
    with pytest.raises(ValueError, match="relative POSIX"):
        quote_path_components(unsafe)


def test_nfc_equivalent_composer_aliases_share_one_unsuffixed_component():
    composed = PathSource(kind="composer", original="Caf\u00e9", stable_id="alias-1")
    decomposed = PathSource(kind="composer", original="Cafe\u0301", stable_id="alias-2")
    forward = build_path_map([composed, decomposed])
    reverse = build_path_map([decomposed, composed])
    assert forward == reverse
    assert forward[composed].mapped == "Caf\u00e9"
    assert forward[decomposed].mapped == "Caf\u00e9"
    assert forward[composed].collision_reason is None
    assert forward[decomposed].collision_reason is None


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


def test_path_map_rejects_metadata_symlink_before_external_write(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-path-map"
    outside.mkdir()
    (tmp_path / "metadata").symlink_to(outside, target_is_directory=True)
    source = PathSource(kind="work", original="Safe", stable_id="p7")
    with pytest.raises(ValueError, match="symlink ancestor"):
        write_path_map(tmp_path, build_path_map([source]))
    assert list(outside.iterdir()) == []


def test_path_map_rejects_symlink_library_root(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-root-path-map"
    outside.mkdir()
    root = tmp_path / "library"
    root.symlink_to(outside, target_is_directory=True)
    source = PathSource(kind="work", original="Safe", stable_id="p7")
    with pytest.raises(ValueError, match="library root is a symlink"):
        write_path_map(root, build_path_map([source]))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload | {"model_type": "WrongManifest"},
        lambda payload: payload | {"schema_version": 2},
        lambda payload: payload | {"items": [{"kind": "work"}]},
    ],
)
def test_path_map_rejects_incompatible_existing_envelope_without_changing_bytes(tmp_path, mutation):
    source = PathSource(kind="work", original="Safe", stable_id="p7")
    mappings = build_path_map([source])
    path = write_path_map(tmp_path, mappings)
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(mutation(payload)), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises((TypeError, ValueError), match="PathMapManifest"):
        write_path_map(tmp_path, mappings)
    assert path.read_bytes() == before
