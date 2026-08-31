from __future__ import annotations

import hashlib
import re

from imslp_library.config import load_allowlist
from imslp_library.extractor import extract_memberships
from tests.basic_helpers import ROOT, load_text_fixture
from tests.model_helpers import make_frozen_page

CONFIG = load_allowlist(ROOT / "config/categories.json")
KEEP_INSTRUMENTATION = object()
REMOVE_INSTRUMENTATION = object()

def extract_fixture(filename, category_name, *, category_membership=True,
                    instrumentation_override=KEEP_INSTRUMENTATION):
    text = load_text_fixture(f"wikitext/{filename}")
    if instrumentation_override is REMOVE_INSTRUMENTATION:
        text = re.sub(r"(?m)^\|Instrumentation=.*\n?", "", text)
    elif instrumentation_override is not KEEP_INSTRUMENTATION:
        text = re.sub(r"(?m)^\|Instrumentation=.*$",
                      f"|Instrumentation={instrumentation_override}", text)
    page = make_frozen_page(
        category_names=(category_name,) if category_membership else (),
        wikitext_path=f"tests/fixtures/wikitext/{filename}",
        wikitext_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    rule = next(rule for rule in CONFIG.categories if rule.name == category_name)
    return extract_memberships(page, text, rule)


import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from imslp_library.extractor import (
    apply_extraction_reviews,
    extraction_decision_sha256,
    review_filename,
)
from imslp_library.models import ExtractionReview
from tests.basic_helpers import make_file_template, make_wikitext


ACCEPTED_EXAMPLES = {
    "For guitar": ("guitar", None),
    "For guitar (arr)": ("guitar", "For Guitar"),
    "For 2 guitars": ("2 guitars", None),
    "For 2 guitars (arr)": ("2 guitars", "For 2 Guitars"),
    "For 3 guitars": ("3 guitars", None),
    "For 3 guitars (arr)": ("3 guitars", "For 3 Guitars"),
    "For 4 guitars": ("4 guitars", None),
    "For 4 guitars (arr)": ("4 guitars", "For 4 Guitars"),
    "For 5 guitars": ("5 guitars", None),
    "For 5 guitars (arr)": ("5 guitars", "For 5 Guitars"),
    "For 6 guitars": ("6 guitars", None),
    "For 6 guitars (arr)": ("6 guitars", "For 6 Guitars"),
    "For 7 guitars": ("7 guitars", None),
    "For 8 guitars": ("8 guitars", None),
    "For 8 guitars (arr)": ("8 guitars", "For 8 Guitars"),
    "For 9 guitars": ("9 guitars", None),
    "For 12 guitars": ("12 guitars", None),
    "For 12 guitars (arr)": ("12 guitars", "For 12 Guitars"),
    "For 16 guitars": ("16 guitars", None),
    "For 6 string guitar (arr)": ("6 string guitar", "For 6 String Guitar"),
    "For 7 string guitar (arr)": ("7 string guitar", "For 7 String Guitar"),
    "For 7-string guitar (arr)": ("7-string guitar", "For 7-String Guitar"),
    "For 8 string guitar (arr)": ("8 string guitar", "For 8 String Guitar"),
    "For 8-string guitar (arr)": ("8-string guitar", "For 8-String Guitar"),
    "For 10 string guitar (arr)": ("10 string guitar", "For 10 String Guitar"),
    "For 10-string guitar (arr)": ("10-string guitar", "For 10-String Guitar"),
    "For 2 and 3 guitars (arr)": ("2 and 3 guitars", "For 2 and 3 Guitars"),
    "For guitar ensemble (arr)": ("guitar ensemble", "For Guitar Ensemble"),
    "For guitar orchestra (arr)": ("guitar orchestra", "For Guitar Orchestra"),
}

assert set(ACCEPTED_EXAMPLES) == {rule.name for rule in CONFIG.categories}


def _extract_text(text: str, category_name: str, *, member: bool = True):
    page = make_frozen_page(
        category_names=(category_name,) if member else (),
        wikitext_path="generated.wiki",
        wikitext_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    rule = next(rule for rule in CONFIG.categories if rule.name == category_name)
    return extract_memberships(page, text, rule)


def _arrangement_text(heading: str, *, filename: str = "score.pdf", file_id: str = "700") -> str:
    body = "===Arrangements and Transcriptions===\n====" + heading + "====\n" + make_file_template(filename, file_id)
    return make_wikitext(body, "orchestra")


def test_exact_arrangement_is_selected_but_mixed_prefix_is_rejected():
    exact = extract_fixture("exact_three_guitars.wiki", "For 3 guitars (arr)")
    mixed = extract_fixture("mixed_bass.wiki", "For 3 guitars (arr)")
    assert [item.filename for item in exact.selected] == ["three-guitars.pdf"]
    assert mixed.selected == []
    assert mixed.excluded[0].reason_code == "mixed_instrument_heading"


def test_work_level_exception_requires_all_four_evidence_conditions():
    accepted = extract_fixture("work_level_exact.wiki", "For 3 guitars (arr)")
    rejected = extract_fixture("work_level_mixed.wiki", "For 3 guitars (arr)")
    assert accepted.selected[0].selection_reason.value == "work_level_exact_instrumentation"
    assert rejected.selected == []
    assert rejected.manual_review


@pytest.mark.parametrize("category_name, example", ACCEPTED_EXAMPLES.items())
def test_every_approved_rule_family_accepts_its_literal_example(category_name, example):
    instrumentation, heading = example
    if heading is None:
        text = make_wikitext("===Scores and Parts===\n" + make_file_template("score.pdf", "701"), instrumentation)
        result = _extract_text(text, category_name)
        assert result.selected[0].selection_reason.value == "exact_original_instrumentation"
    else:
        for annotation in ("", " (Smith, John)", " (Höger, Anton)"):
            result = _extract_text(_arrangement_text(heading + annotation), category_name)
            assert [item.filename for item in result.selected] == ["score.pdf"]


@pytest.mark.parametrize(
    "category_name,accepted,rejected",
    [
        ("For 7 string guitar (arr)", "For 7 String Guitar", "For 7-String Guitar"),
        ("For 7-string guitar (arr)", "For 7-String Guitar", "For 7 String Guitar"),
        ("For 8 string guitar (arr)", "For 8 String Guitar", "For 8-String Guitar"),
        ("For 8-string guitar (arr)", "For 8-String Guitar", "For 8 String Guitar"),
        ("For 10 string guitar (arr)", "For 10 String Guitar", "For 10-String Guitar"),
        ("For 10-string guitar (arr)", "For 10-String Guitar", "For 10 String Guitar"),
    ],
)
def test_extended_string_rules_do_not_cross_accept_spacing(category_name, accepted, rejected):
    assert _extract_text(_arrangement_text(accepted), category_name).selected
    result = _extract_text(_arrangement_text(rejected), category_name)
    assert result.selected == []
    assert result.excluded[0].reason_code == "heading_not_exact"


@pytest.mark.parametrize(
    "instrumentation,expected_reason",
    [
        ("3 guitars and double bass", "work_level_instrumentation_not_exact"),
        ("", "work_level_instrumentation_missing"),
        (REMOVE_INSTRUMENTATION, "work_level_instrumentation_missing"),
    ],
)
def test_work_level_requires_exact_present_instrumentation(instrumentation, expected_reason):
    result = extract_fixture(
        "work_level_exact.wiki",
        "For 3 guitars (arr)",
        instrumentation_override=instrumentation,
    )
    assert result.selected == []
    assert result.manual_review[0].reason_code == expected_reason


def test_work_level_source_is_blocked_when_target_arrangement_heading_exists():
    base = load_text_fixture("wikitext/work_level_exact.wiki")
    addition = "===Arrangements and Transcriptions===\n====For 3 Guitars====\n" + make_file_template("arranged.pdf", "503") + "\n"
    text = base.replace("| *****WORK INFO*****", addition + "| *****WORK INFO*****")
    result = _extract_text(text, "For 3 guitars (arr)")
    assert [item.filename for item in result.selected] == ["arranged.pdf"]
    work_level = next(item for item in result.excluded if item.filename == "work-level.pdf")
    assert work_level.reason_code == "work_level_target_heading_present"


@pytest.mark.parametrize("branch", ["Source Files", "Synthesized/MIDI", "Audio", "Commercial"])
def test_original_or_work_level_files_from_other_branches_are_not_allowed(branch):
    base = load_text_fixture("wikitext/work_level_exact.wiki")
    text = base.replace("===Scores and Parts===", f"==={branch}===")
    result = _extract_text(text, "For 3 guitars (arr)")
    assert result.selected == []
    assert result.excluded[0].reason_code == "branch_not_allowed"


def test_non_pdf_is_explicitly_excluded_from_an_otherwise_exact_branch():
    result = extract_fixture("exact_three_guitars.wiki", "For 3 guitars (arr)")
    source = next(item for item in result.excluded if item.filename == "source.mscz")
    assert source.reason_code == "not_pdf"


def test_page_membership_and_category_kind_are_independent_gates():
    not_member = extract_fixture("work_level_exact.wiki", "For 3 guitars (arr)", category_membership=False)
    assert not_member.selected == []
    assert not_member.excluded[0].reason_code == "page_not_in_category"

    original_rule_on_arrangement = extract_fixture("exact_three_guitars.wiki", "For 3 guitars")
    assert original_rule_on_arrangement.selected == []
    assert {item.reason_code for item in original_rule_on_arrangement.excluded} == {"branch_not_allowed"}

    arrangement_rule_on_original = extract_fixture("original_guitar.wiki", "For guitar (arr)")
    assert arrangement_rule_on_original.selected == []
    assert any(
        item.reason_code == "work_level_arrangement_branch_present"
        for item in arrangement_rule_on_original.excluded
    )


@pytest.mark.parametrize("annotation", ["Smith, John", "Höger, Anton"])
def test_human_arranger_annotations_are_accepted(annotation):
    assert _extract_text(_arrangement_text(f"For 3 Guitars ({annotation})"), "For 3 guitars (arr)").selected


@pytest.mark.parametrize("annotation", ["Smith, piano", "organ", "recorder", "ukulele", "drums"])
def test_instrument_annotations_are_rejected(annotation):
    result = _extract_text(_arrangement_text(f"For 3 Guitars ({annotation})"), "For 3 guitars (arr)")
    assert result.selected == []
    assert result.excluded[0].reason_code == "annotation_contains_instrument"


def test_mixed_descendant_is_rejected_before_its_file_is_parsed_for_selection():
    result = extract_fixture("nested_other_instrument.wiki", "For 3 guitars (arr)")
    assert result.selected == []
    assert result.excluded[0].reason_code == "mixed_instrument_heading"
    assert result.excluded[0].evidence.heading_ancestry[-1] == "With Bass Guitar"


@pytest.mark.parametrize(
    "category_name,instrumentation",
    [
        ("For 3 guitars (arr)", "3 guitars"),
        ("For 3 guitars", "3 guitars"),
        ("For guitar", "guitar"),
    ],
)
def test_mixed_descendant_gate_applies_to_work_level_and_original_score_paths(
    category_name, instrumentation
):
    text = make_wikitext(
        "===Scores and Parts===\n====With Bass Guitar====\n"
        + make_file_template("mixed-descendant.pdf", "699"),
        instrumentation,
    )
    result = _extract_text(text, category_name)
    assert result.selected == []
    assert [item.reason_code for item in result.excluded] == ["mixed_instrument_heading"]


def test_fullmatch_rejects_unconfigured_or_and_preserves_structured_evidence():
    flexible = extract_fixture("flexible_2_and_3.wiki", "For 2 and 3 guitars (arr)")
    selected = flexible.selected[0]
    assert selected.evidence.heading_raw == "For 2 and 3 Guitars (Doe, Jane)"
    assert selected.evidence.heading_normalized == "for 2 and 3 guitars (doe, jane)"
    assert selected.evidence.heading_ancestry == (
        "Arrangements and Transcriptions",
        "For 2 and 3 Guitars (Doe, Jane)",
    )
    assert selected.evidence.heading_ancestry_normalized == (
        "arrangements and transcriptions",
        "for 2 and 3 guitars (doe, jane)",
    )
    assert selected.evidence.instrumentation_raw == "orchestra"
    assert selected.evidence.instrumentation_normalized == "orchestra"
    assert selected.evidence.branch == "Arrangements and Transcriptions"

    wrong = _extract_text(_arrangement_text("For 2 or 3 Guitars"), "For 2 and 3 guitars (arr)")
    assert wrong.selected == []


def test_file_id_deduplication_is_revision_local_and_never_filename_based():
    body = "===Arrangements and Transcriptions===\n====For 3 Guitars====\n"
    body += make_file_template("same.pdf", "801") + "\n"
    body += make_file_template("same.pdf", "801") + "\n"
    body += make_file_template("same.pdf", "802")
    result = _extract_text(make_wikitext(body, "orchestra"), "For 3 guitars (arr)")
    assert [item.source_id for item in result.selected] == ["source:f801@r202", "source:f802@r202"]


def _review_directory(tmp_path: Path, run_id: str) -> Path:
    directory = tmp_path / "metadata" / "overrides" / "extraction_reviews" / run_id
    directory.mkdir(parents=True)
    return directory


def _review_for(decision, **overrides) -> ExtractionReview:
    defaults = {
        "run_id": "run-1",
        "category_name": "For 3 guitars (arr)",
        "page_id": 101,
        "revision_id": 202,
        "source_id": decision.source_id,
        "decision": "exclude",
        "reason": "reviewed exact frozen evidence",
        "extraction_decision_sha256": extraction_decision_sha256(decision),
        "reviewed_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
    }
    return ExtractionReview(**(defaults | overrides))


def _write_review(directory: Path, review: ExtractionReview, *, filename: str | None = None, payload=None) -> Path:
    target = directory / (filename or review_filename(review.category_name, review.page_id, review.source_id))
    target.write_text(json.dumps(payload or review.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return target


def test_valid_exclusion_review_replay_cannot_select_and_keeps_evidence_reference(tmp_path):
    result = extract_fixture("work_level_mixed.wiki", "For 3 guitars (arr)")
    decision = result.manual_review[0]
    directory = _review_directory(tmp_path, "run-1")
    review = _review_for(decision)
    review_path = _write_review(directory, review)

    replayed = apply_extraction_reviews(result, run_id="run-1", review_directory=directory)
    assert replayed.manual_review == []
    assert replayed.selected == []
    excluded = replayed.excluded[0]
    assert excluded.source_id == decision.source_id
    assert excluded.evidence.heading_ancestry == decision.evidence.heading_ancestry
    assert (
        excluded.evidence.heading_ancestry_normalized
        == decision.evidence.heading_ancestry_normalized
    )
    assert review_path.name in excluded.evidence.reason_detail
    assert excluded.reason_code == "extraction_review_exclude"


@pytest.mark.parametrize("mutation", ["run", "revision", "digest", "source", "include", "naive_time"])
def test_stale_or_scope_broadening_extraction_reviews_are_rejected(tmp_path, mutation):
    result = extract_fixture("work_level_mixed.wiki", "For 3 guitars (arr)")
    decision = result.manual_review[0]
    directory = _review_directory(tmp_path, "run-1")
    review = _review_for(decision)
    payload = review.to_dict()
    if mutation == "run":
        payload["run_id"] = "other-run"
    elif mutation == "revision":
        payload["revision_id"] = 201
        payload["source_id"] = "source:f502@r201"
    elif mutation == "digest":
        payload["extraction_decision_sha256"] = "0" * 64
    elif mutation == "source":
        payload["source_id"] = "source:f999@r202"
    elif mutation == "include":
        payload["decision"] = "include"
    else:
        payload["reviewed_at"] = "2026-08-30T00:00:00"
    _write_review(directory, review, payload=payload)

    with pytest.raises((TypeError, ValueError)):
        apply_extraction_reviews(result, run_id="run-1", review_directory=directory)


def test_extra_review_source_and_wrong_directory_layout_are_rejected(tmp_path):
    result = extract_fixture("work_level_mixed.wiki", "For 3 guitars (arr)")
    decision = result.manual_review[0]
    directory = _review_directory(tmp_path, "run-1")
    _write_review(directory, _review_for(decision))
    (directory / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected extraction review"):
        apply_extraction_reviews(result, run_id="run-1", review_directory=directory)

    wrong = tmp_path / "run-1"
    wrong.mkdir()
    with pytest.raises(ValueError, match="directory"):
        apply_extraction_reviews(result, run_id="run-1", review_directory=wrong)


def test_review_replay_never_changes_already_excluded_mixed_instrument_decision(tmp_path):
    result = extract_fixture("mixed_bass.wiki", "For 3 guitars (arr)")
    directory = _review_directory(tmp_path, "run-1")
    assert apply_extraction_reviews(result, run_id="run-1", review_directory=directory) == result
