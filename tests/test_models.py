from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime

import pytest

from imslp_library.enums import (
    AttemptPhase,
    CategoryKind,
    DownloadStatus,
    IssueSeverity,
    ReviewStatus,
    RunStatus,
    SelectionReason,
    StorageMethod,
)
from imslp_library.models import ExtractionResult, RunSnapshot, VerificationReport
from tests import model_helpers as h


ENUM_VALUES = {
    CategoryKind: {"original", "arrangement"},
    StorageMethod: {"hardlink", "relative_symlink"},
    SelectionReason: {"exact_original_instrumentation", "exact_arrangement_heading", "work_level_exact_instrumentation"},
    RunStatus: {"snapshot_incomplete", "snapshot_complete", "in_progress", "paused", "complete", "failed"},
    AttemptPhase: {"started", "streaming", "finished"},
    ReviewStatus: {"not_required", "pending", "resolved"},
    IssueSeverity: {"info", "warning", "error"},
    DownloadStatus: {"not_started", "retryable", "human_verification_required", "membership_wait_pending", "copyright_restricted", "region_restricted", "membership_required", "login_required", "commercial_only", "deleted", "manual_review", "downloaded_verified", "source_override_verified"},
}

FACTORIES = [h.make_frozen_page, h.make_work, h.make_score, h.make_evidence, h.make_membership, h.make_download_result, h.make_stored_object, h.make_extraction_decision, h.make_extraction_result, h.make_extraction_review, h.make_category_snapshot, h.make_run_snapshot, h.make_run_state, h.make_download_target, h.make_download_attempt, h.make_download_batch_result, h.make_source_hash_review, h.make_materialization_result, h.make_verification_issue, h.make_verification_report]


@pytest.mark.parametrize(("enum_type", "values"), ENUM_VALUES.items())
def test_enums_have_exact_string_vocabulary(enum_type, values) -> None:
    assert issubclass(enum_type, str)
    assert {member.value for member in enum_type} == values
    with pytest.raises(ValueError):
        enum_type("unknown")


@pytest.mark.parametrize("factory", FACTORIES)
def test_factories_return_frozen_slotted_round_trippable_models(factory) -> None:
    model = factory()
    assert model.__slots__ == tuple(field.name for field in fields(model))
    with pytest.raises(FrozenInstanceError):
        setattr(model, fields(model)[0].name, "changed")
    payload = model.to_dict()
    assert payload["model_type"] == type(model).__name__
    assert type(model).from_dict(payload) == model
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert canonical(type(model).from_dict(payload).to_dict()) == canonical(payload)


def test_extraction_result_properties_preserve_decision_order() -> None:
    selected = h.make_extraction_decision(filename="a.pdf")
    excluded = h.make_extraction_decision(filename="b.pdf", disposition="excluded", selection_reason=None)
    manual = h.make_extraction_decision(filename="c.pdf", disposition="manual_review", selection_reason=None)
    result = h.make_extraction_result(decisions=(manual, selected, excluded, selected))
    assert result.manual_review == (manual,)
    assert result.selected == (selected, selected)
    assert result.excluded == (excluded,)


@pytest.mark.parametrize(("factory", "changes"), [
    (h.make_work, {"work_id": ""}), (h.make_work, {"page_id": -1}),
    (h.make_score, {"expected_size": -1}), (h.make_score, {"sha1_imslp": "A" * 40}),
    (h.make_stored_object, {"sha256": "x" * 64}), (h.make_stored_object, {"size": -1}),
    (h.make_extraction_review, {"decision": "include"}), (h.make_extraction_review, {"reason": ""}),
    (h.make_source_hash_review, {"decision": "accept"}), (h.make_source_hash_review, {"reviewer_note": ""}),
    (h.make_verification_report, {"report_sha256": "b" * 63}),
])
def test_invalid_scalar_invariants_are_rejected(factory, changes) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory(**changes)


def test_stable_ids_are_validated_without_rewriting() -> None:
    with pytest.raises(ValueError, match="work_id"):
        h.make_work(work_id="work:p101@r999")
    with pytest.raises(ValueError, match="source_id"):
        h.make_score(source_id="source:f999@r202")
    with pytest.raises(ValueError, match="membership_id"):
        h.make_membership(membership_id="membership:0000000000:source:f301@r202")


def test_paired_optional_fields_and_source_hash_state_are_consistent() -> None:
    for factory, changes in [
        (h.make_score, {"source_hash_missing": True}),
        (h.make_score, {"sha256": h.SHA256}),
        (h.make_membership, {"local_path": "x.pdf"}),
        (h.make_run_state, {"start_drift_report_path": "start.json"}),
        (h.make_download_result, {"source_hash_missing": True}),
        (h.make_download_result, {"review_status": ReviewStatus.RESOLVED}),
    ]:
        with pytest.raises(ValueError):
            factory(**changes)
    h.make_score(sha1_imslp=None, source_hash_missing=True)
    h.make_score(sha256=h.SHA256, object_path="objects/bb/value.pdf")
    h.make_membership(local_path="view.pdf", storage_method=StorageMethod.HARDLINK)


def test_datetime_and_completion_invariants_are_enforced() -> None:
    naive = datetime(2026, 8, 30, 12, 0)
    for factory, changes in [(h.make_run_snapshot, {"snapshot_started_at": naive}), (h.make_extraction_review, {"reviewed_at": naive}), (h.make_download_result, {"attempted_at": naive}), (h.make_stored_object, {"verified_at": naive})]:
        with pytest.raises(ValueError):
            factory(**changes)
    with pytest.raises(ValueError):
        h.make_download_attempt(completed_at=datetime(2026, 8, 30, 11, 0, tzinfo=h.NOW.tzinfo))
    with pytest.raises(ValueError):
        h.make_run_snapshot(snapshot_completed_at=None)


def test_download_attempt_phase_and_association_contract() -> None:
    h.make_download_attempt(phase=AttemptPhase.STARTED, status=DownloadStatus.NOT_STARTED, completed_at=None, http_status=None, bytes_written=0, detail_code="started")
    for changes in [
        {"attempt_number": 0}, {"bytes_written": -1}, {"category_names": ()},
        {"membership_ids": ()}, {"phase": AttemptPhase.STARTED},
        {"phase": AttemptPhase.FINISHED, "status": DownloadStatus.NOT_STARTED},
        {"category_names": ("z", "a"), "membership_ids": (h.MEMBERSHIP_ID, h.MEMBERSHIP_ID)},
    ]:
        with pytest.raises(ValueError):
            h.make_download_attempt(**changes)


def test_download_batch_source_and_pairs_must_match() -> None:
    with pytest.raises(ValueError):
        h.make_download_batch_result(source_id="source:f999@r202")
    with pytest.raises(ValueError):
        h.make_download_batch_result(category_names=("a", "b"), membership_ids=(h.MEMBERSHIP_ID,))


def test_membership_associations_validate_category_digest() -> None:
    with pytest.raises(ValueError, match="category"):
        h.make_download_target(category_name="Wrong category")
    with pytest.raises(ValueError, match="category"):
        h.make_download_attempt(category_names=("Wrong category",))


def test_review_source_revision_must_match_reviewed_revision() -> None:
    with pytest.raises(ValueError, match="revision"):
        h.make_extraction_review(revision_id=999)
    with pytest.raises(ValueError, match="revision"):
        h.make_source_hash_review(page_revision_id=999)


def test_verified_download_states_distinguish_source_hash_and_override_review() -> None:
    with pytest.raises(ValueError):
        h.make_download_result(sha1=None, source_hash_missing=True)
    with pytest.raises(ValueError):
        h.make_download_result(status=DownloadStatus.SOURCE_OVERRIDE_VERIFIED)
    h.make_download_result(status=DownloadStatus.SOURCE_OVERRIDE_VERIFIED, review_status=ReviewStatus.RESOLVED, review_evidence_path="metadata/overrides/source_hash_review.json", sha1=None, source_hash_missing=True)


def test_strict_deserialization_rejects_bad_shape_and_nested_types() -> None:
    payload = h.make_download_target().to_dict()
    for mutation in [lambda p: p.pop("model_type"), lambda p: p.__setitem__("extra", 1), lambda p: p.__setitem__("model_type", "Unknown"), lambda p: p.__setitem__("score", h.make_work().to_dict())]:
        changed = json.loads(json.dumps(payload))
        mutation(changed)
        with pytest.raises((TypeError, ValueError)):
            type(h.make_download_target()).from_dict(changed)


def test_aggregate_models_sort_only_set_like_collections() -> None:
    category_a = h.make_category_snapshot(name="A", member_page_ids=(3, 1, 2), member_count=3)
    category_z = h.make_category_snapshot(name="Z")
    page1 = h.make_frozen_page(page_id=1, revision_id=9, wikitext_path="x", category_names=("z", "a"))
    page2 = h.make_frozen_page(page_id=1, revision_id=2, wikitext_path="y")
    score1 = h.make_score("2")
    score2 = h.make_score("1")
    snapshot = h.make_run_snapshot(categories=(category_z, category_a), pages=(page1, page2), score_files=(score1, score2))
    assert tuple(item.name for item in snapshot.categories) == ("A", "Z")
    assert tuple((item.page_id, item.revision_id) for item in snapshot.pages) == ((1, 2), (1, 9))
    assert tuple(item.source_id for item in snapshot.score_files) == tuple(sorted((score1.source_id, score2.source_id)))
    assert snapshot.categories[0].member_page_ids == (1, 2, 3)
    assert page1.category_names == ("z", "a")


def test_verification_json_values_and_issue_sorting_are_strict() -> None:
    info = h.make_verification_issue(code="z", severity=IssueSeverity.INFO, stable_ids=("b",))
    warning = h.make_verification_issue(code="a", severity=IssueSeverity.WARNING, stable_ids=("a",))
    report = h.make_verification_report(issues=(warning, info))
    assert report.issues == (info, warning)
    for facts in [{1: "bad"}, {"bad": object()}]:
        with pytest.raises((TypeError, ValueError)):
            h.make_verification_report(facts=facts)
    with pytest.raises(TypeError):
        h.make_verification_issue(evidence={"bad": {1: "nested"}})
