from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from imslp_library.config import ConfigError, load_allowlist
from tests.basic_helpers import make_file_template, make_wikitext, write_json, write_minimal_pdf

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "categories.json"
EXPECTED_CATEGORY_NAMES = {
    "For guitar", "For guitar (arr)", "For 2 guitars", "For 2 guitars (arr)",
    "For 3 guitars", "For 3 guitars (arr)", "For 4 guitars", "For 4 guitars (arr)",
    "For 5 guitars", "For 5 guitars (arr)", "For 6 guitars", "For 6 guitars (arr)",
    "For 7 guitars", "For 8 guitars", "For 8 guitars (arr)", "For 9 guitars",
    "For 12 guitars", "For 12 guitars (arr)", "For 16 guitars",
    "For 6 string guitar (arr)", "For 7 string guitar (arr)", "For 7-string guitar (arr)",
    "For 8 string guitar (arr)", "For 8-string guitar (arr)", "For 10 string guitar (arr)",
    "For 10-string guitar (arr)", "For 2 and 3 guitars (arr)",
    "For guitar ensemble (arr)", "For guitar orchestra (arr)",
}
EXPECTED_ANNOTATION_REJECT_TOKENS = {
    "accordion", "bass", "bassoon", "brass", "cello", "clarinet", "contrabass", "double bass",
    "drum", "drums", "electric", "flute", "guitar", "harp", "harpsichord", "horn", "keyboard",
    "lute", "mandolin", "oboe", "organ", "percussion", "piano", "recorder", "saxophone", "strings",
    "trombone", "trumpet", "tuba", "ukulele", "viola", "violin", "voice", "voices", "vocal", "woodwind",
}


def real_payload() -> dict[str, object]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def mutated(tmp_path: Path, mutate) -> Path:
    payload = copy.deepcopy(real_payload())
    mutate(payload)
    target = tmp_path / "categories.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_config_has_exact_approved_categories_and_stable_hash(tmp_path: Path) -> None:
    config = load_allowlist(CONFIG_PATH)
    assert (config.schema_version, config.version, config.approved_at) == (1, "2026-08-30.1", "2026-08-30")
    assert len(config.categories) == 29
    assert all(category.name.startswith("For ") for category in config.categories)
    assert {category.name for category in config.categories} == EXPECTED_CATEGORY_NAMES
    reordered = json.dumps(real_payload(), indent=7, sort_keys=True)
    alternate = tmp_path / "reordered.json"
    alternate.write_text("\n" + reordered + "\n", encoding="utf-8")
    assert load_allowlist(alternate).config_hash == config.config_hash


def test_every_category_has_the_complete_annotation_reject_list() -> None:
    config = load_allowlist(CONFIG_PATH)
    assert len(config.categories) == 29
    assert all(category.annotation_reject_tokens == EXPECTED_ANNOTATION_REJECT_TOKENS for category in config.categories)


def test_basic_helpers_match_the_fixture_contract(tmp_path: Path) -> None:
    json_path = tmp_path / "data" / "value.json"
    pdf_path = tmp_path / "data" / "value.pdf"
    assert write_json(json_path, {"é": 1}) == json_path
    assert json_path.read_text(encoding="utf-8") == '{\n  "é": 1\n}\n'
    assert make_file_template("score.pdf", "7") == "{{#fte:imslpfile\n|File Name 1=score.pdf\n|File ID=7\n}}"
    assert make_wikitext("body", "guitar") == "| *****FILES*****\nbody\n| *****WORK INFO*****\n|Instrumentation=guitar"
    assert write_minimal_pdf(pdf_path) == pdf_path
    assert pdf_path.read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda p: p.__setitem__("schema_version", True), "invalid_metadata"), (lambda p: p.__setitem__("version", " "), "invalid_version"), (lambda p: p.__setitem__("version", 7), "invalid_version"),
    (lambda p: p.__setitem__("approved_at", 7), "invalid_approved_at"), (lambda p: p.__setitem__("approved_at", "not-a-date"), "invalid_approved_at"), (lambda p: p.__setitem__("categories", []), "invalid_categories"),
    (lambda p: p["categories"][0].__setitem__("name", "   "), "invalid_name"), (lambda p: p["categories"][0].__setitem__("display_group", "   "), "invalid_display_group"), (lambda p: p["categories"][0].__setitem__("guitar_count", True), "invalid_guitar_count"),
    (lambda p: p["categories"][19].__setitem__("extended_strings", None), "invalid_extended_solo"), (lambda p: p["categories"][19].__setitem__("guitar_count", 2), "invalid_extended_solo"), (lambda p: p["categories"][0].__setitem__("extended_strings", 6), "unexpected_extended_strings"),
    (lambda p: p["categories"][0].__setitem__("annotation_reject_tokens", [7]), "invalid_annotation_reject_tokens"), (lambda p: p["categories"][0].__setitem__("annotation_reject_tokens", ["   "]), "invalid_annotation_reject_tokens"), (lambda p: p["categories"][0].__setitem__("annotation_reject_tokens", ["piano", " PIANO "]), "duplicate_annotation_token"),
    (lambda p: p["categories"][0].__setitem__("kind", []), "invalid_kind"), (lambda p: p["categories"][0].__setitem__("instrumentation_patterns", [7]), "invalid_pattern"), (lambda p: p["categories"][1].__setitem__("heading_patterns", [7]), "invalid_pattern"),
])
def test_structure_mutations_are_rejected(tmp_path: Path, mutate, code: str) -> None:
    with pytest.raises(ConfigError, match=code):
        load_allowlist(mutated(tmp_path, mutate))


def test_annotation_tokens_are_normalized(tmp_path: Path) -> None:
    config = load_allowlist(mutated(tmp_path, lambda p: p["categories"][0].__setitem__("annotation_reject_tokens", [" Voice "])))
    assert config.categories[0].annotation_reject_tokens == frozenset({"voice"})


def test_future_approved_metadata_is_accepted(tmp_path: Path) -> None:
    path = mutated(tmp_path, lambda p: (p.__setitem__("version", "2030.1"), p.__setitem__("approved_at", "2030-01-02")))
    config = load_allowlist(path)
    assert (config.version, config.approved_at) == ("2030.1", "2030-01-02")


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p: p["categories"].append(copy.deepcopy(p["categories"][0])), "duplicate_name"),
        (lambda p: p["categories"][0].__setitem__("instrumentation_patterns", ["guitar$"]), "invalid_pattern"),
        (lambda p: p["categories"][0].__setitem__("instrumentation_patterns", ["^guitar"]), "invalid_pattern"),
        (lambda p: p["categories"][0].__setitem__("extra", True), "unknown_key"),
        (lambda p: p["categories"][0].__setitem__("instrumentation_patterns", []), "missing_instrumentation_patterns"),
        (lambda p: p["categories"][1].__setitem__("heading_patterns", []), "missing_heading_patterns"),
        (lambda p: p["categories"][0].__setitem__("guitar_count", 0), "invalid_guitar_count"),
        (lambda p: p["categories"][0].__setitem__("instrumentation_patterns", ["^(broken$"]), "invalid_regex"),
    ],
)
def test_invalid_mutations_are_rejected(tmp_path: Path, mutate, code: str) -> None:
    with pytest.raises(ConfigError, match=code):
        load_allowlist(mutated(tmp_path, mutate))


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p: p["categories"][0].__setitem__("url", "https://imslp.org/wiki/Category:Wrong"), "url_name_mismatch"),
        (lambda p: p["categories"][0].__setitem__("name", "For electric guitar"), "excluded_instrument"),
        (lambda p: p["categories"][0].__setitem__("name", "For bass guitar"), "excluded_instrument"),
        (lambda p: p["categories"][0].__setitem__("kind", "bad"), "invalid_kind"),
    ],
)
def test_other_invalid_mutations_are_rejected(tmp_path: Path, mutate, code: str) -> None:
    with pytest.raises(ConfigError, match=code):
        load_allowlist(mutated(tmp_path, mutate))


def test_fullmatch_and_annotation_rejection_rules() -> None:
    category = next(c for c in load_allowlist(CONFIG_PATH).categories if c.name == "For guitar (arr)")
    assert category.matches_instrumentation("guitar")
    assert not category.matches_instrumentation("guitar and piano")
    assert category.matches_heading("For guitar (Smith, John)")
    assert category.matches_heading("For guitar (Höger, Anton)")
    for rejected in ("Smith, piano", "organ", "recorder", "ukulele", "drums"):
        assert not category.matches_heading(f"For guitar ({rejected})")


def test_ignore_policy(tmp_path: Path) -> None:
    ignored = [".pytest_cache/x", "x.egg-info/a", "index.html", "catalog.csv", "score_manifest.csv", "x.pdf", "x.part", "x.tmp", "For guitar/x", "for3guitars/x", "objects/x", "backups/x", "quarantine/x", "_migration/x", "metadata/.cache/x", "metadata/runs/x", "metadata/migrations/x", "metadata/verification/x", "metadata/operations/x", "metadata/catalog.json", "metadata/score_manifest.json", "metadata/path_map.json", "metadata/category_drift_report.json", "metadata/translation_coverage.json"]
    tracked = ["config/categories.json", "metadata/translations/composers_zh.json", "metadata/overrides/extraction_reviews/run/x.json", "tests/fixtures/sample.pdf", "tests/index.html", "docs/index.html", "docs/catalog.csv", "scripts/index.html", "README.md", "TODO.md", "AGENTS.md", "scripts/code.py"]
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    for relative in ignored + tracked:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    for relative in ignored:
        assert subprocess.run(["git", "check-ignore", "-q", "--no-index", str(repo / relative)], cwd=repo).returncode == 0, relative
    for relative in tracked:
        assert subprocess.run(["git", "check-ignore", "-q", "--no-index", str(repo / relative)], cwd=repo).returncode == 1, relative
