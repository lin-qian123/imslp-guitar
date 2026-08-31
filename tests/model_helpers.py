from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from imslp_library.enums import (
    AttemptPhase,
    DownloadStatus,
    IssueSeverity,
    ReviewStatus,
    RunStatus,
    SelectionReason,
    StorageMethod,
)
from imslp_library.models import (
    CategorySnapshot,
    DownloadAttempt,
    DownloadBatchResult,
    DownloadResult,
    DownloadTarget,
    ExtractionDecision,
    ExtractionResult,
    ExtractionReview,
    FrozenPage,
    MaterializationResult,
    Membership,
    RunSnapshot,
    RunState,
    ScoreFile,
    SelectionEvidence,
    SourceHashReview,
    StoredObject,
    VerificationIssue,
    VerificationReport,
    Work,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
SHA1 = "a" * 40
SHA256 = "b" * 64
CATEGORY = "For 3 guitars (arr)"
SOURCE_ID = "source:f301@r202"
MEMBERSHIP_ID = f"membership:{hashlib.sha256(CATEGORY.encode('utf-8')).hexdigest()[:10]}:{SOURCE_ID}"


def _build(cls, defaults: dict[str, object], overrides: dict[str, object]):
    return cls(**(defaults | overrides))


def make_frozen_page(**overrides):
    return _build(FrozenPage, {"page_id": 101, "revision_id": 202, "page_title": "Fixture (Composer, Test)", "category_names": (CATEGORY,), "wikitext_path": "metadata/.cache/pages/101/202.wiki", "wikitext_sha256": SHA256}, overrides)


def make_work(**overrides):
    return _build(Work, {"work_id": "work:p101@r202", "page_id": 101, "revision_id": 202, "page_title": "Fixture (Composer, Test)", "title_en": "Fixture", "composer_en": "Composer, Test", "imslp_url": "https://imslp.org/wiki/Fixture_(Composer,_Test)"}, overrides)


def make_score(file_id: str = "301", **overrides):
    return _build(ScoreFile, {"source_id": f"source:f{file_id}@r202", "file_id": file_id, "page_id": 101, "page_revision_id": 202, "filename": "score.pdf", "source_url": "https://imslp.org/files/score.pdf", "expected_size": 123, "sha1_imslp": SHA1, "source_hash_missing": False, "mime": "application/pdf", "copyright_label": "Public Domain", "sha256": None, "object_path": None}, overrides)


def make_evidence(**overrides):
    return _build(SelectionEvidence, {"heading_raw": "For 3 Guitars", "heading_normalized": "for 3 guitars", "heading_ancestry": ("Arrangements and Transcriptions", "For 3 Guitars"), "heading_ancestry_normalized": ("arrangements and transcriptions", "for 3 guitars"), "instrumentation_raw": "orchestra", "instrumentation_normalized": "orchestra", "branch": "Arrangements and Transcriptions", "reason_detail": "exact heading"}, overrides)


def make_membership(category: str = CATEGORY, filename: str = "score.pdf", **overrides):
    category_sha10 = hashlib.sha256(category.encode("utf-8")).hexdigest()[:10]
    return _build(Membership, {"membership_id": f"membership:{category_sha10}:{SOURCE_ID}", "category_name": category, "work_id": "work:p101@r202", "source_id": SOURCE_ID, "selection_reason": SelectionReason.EXACT_ARRANGEMENT_HEADING, "evidence": make_evidence(), "planned_local_path": f"{category}/scores/Composer, Test/Fixture/{filename}", "local_path": None, "storage_method": None, "active": True}, overrides)


def make_download_result(**overrides):
    return _build(DownloadResult, {"source_id": SOURCE_ID, "status": DownloadStatus.DOWNLOADED_VERIFIED, "review_status": ReviewStatus.NOT_REQUIRED, "review_evidence_path": None, "attempted_at": NOW, "retry_after": None, "http_status": 200, "evidence_path": None, "detail": "verified", "size": 123, "sha1": SHA1, "sha256": SHA256, "source_hash_missing": False}, overrides)


def make_stored_object(**overrides):
    return _build(StoredObject, {"sha256": SHA256, "size": 123, "object_path": f"objects/{SHA256[:2]}/{SHA256}.pdf", "sha1_imslp": SHA1, "verified_at": NOW}, overrides)


def make_extraction_decision(**overrides):
    return _build(ExtractionDecision, {"source_id": SOURCE_ID, "filename": "score.pdf", "disposition": "selected", "reason_code": "exact_arrangement_heading", "selection_reason": SelectionReason.EXACT_ARRANGEMENT_HEADING, "evidence": make_evidence()}, overrides)


def make_extraction_result(**overrides):
    return _build(ExtractionResult, {"page_id": 101, "revision_id": 202, "category_name": CATEGORY, "decisions": (make_extraction_decision(),)}, overrides)


def make_extraction_review(**overrides):
    return _build(ExtractionReview, {"run_id": "run-20260830T120000Z", "category_name": CATEGORY, "page_id": 101, "revision_id": 202, "source_id": SOURCE_ID, "decision": "exclude", "reason": "mixed instrumentation confirmed", "extraction_decision_sha256": SHA256, "reviewed_at": NOW}, overrides)


def make_category_snapshot(**overrides):
    return _build(CategorySnapshot, {"name": CATEGORY, "member_page_ids": (101,), "member_count": 1}, overrides)


def make_run_snapshot(**overrides):
    return _build(RunSnapshot, {"schema_version": 1, "run_id": "run-20260830T120000Z", "config_version": "2026-08-30.1", "config_sha256": SHA256, "snapshot_started_at": NOW, "snapshot_completed_at": NOW, "status": RunStatus.SNAPSHOT_COMPLETE, "categories": (make_category_snapshot(),), "pages": (make_frozen_page(),), "score_files": (make_score(),)}, overrides)


def make_run_state(**overrides):
    return _build(RunState, {"schema_version": 1, "run_id": "run-20260830T120000Z", "snapshot_path": "metadata/runs/run-20260830T120000Z.json", "snapshot_sha256": SHA256, "start_drift_report_path": None, "start_drift_report_sha256": None, "end_drift_report_path": None, "end_drift_report_sha256": None, "download_attempt_manifest_path": None, "updated_at": NOW}, overrides)


def make_download_target(**overrides):
    return _build(DownloadTarget, {"category_name": CATEGORY, "membership_id": MEMBERSHIP_ID, "score": make_score()}, overrides)


def make_download_attempt(**overrides):
    return _build(DownloadAttempt, {"run_id": "run-20260830T120000Z", "attempt_number": 1, "category_names": (CATEGORY,), "membership_ids": (MEMBERSHIP_ID,), "source_id": SOURCE_ID, "phase": AttemptPhase.FINISHED, "status": DownloadStatus.DOWNLOADED_VERIFIED, "review_status": ReviewStatus.NOT_REQUIRED, "started_at": NOW, "completed_at": NOW, "http_status": 200, "evidence_path": None, "evidence_sha256": None, "part_path": None, "bytes_written": 123, "retry_after": None, "detail_code": "verified"}, overrides)


def make_download_batch_result(**overrides):
    return _build(DownloadBatchResult, {"source_id": SOURCE_ID, "category_names": (CATEGORY,), "membership_ids": (MEMBERSHIP_ID,), "result": make_download_result()}, overrides)


def make_source_hash_review(**overrides):
    return _build(SourceHashReview, {"source_id": SOURCE_ID, "page_revision_id": 202, "object_sha256": SHA256, "decision": "accept_structural_without_source_hash", "reviewer_note": "PDF header, size, parseability and SHA-256 reviewed", "reviewed_at": NOW}, overrides)


def make_materialization_result(**overrides):
    return _build(MaterializationResult, {"membership_id": MEMBERSHIP_ID, "local_path": f"{CATEGORY}/scores/Composer, Test/Fixture/score.pdf", "storage_method": StorageMethod.HARDLINK, "sha256": SHA256}, overrides)


def make_verification_issue(**overrides):
    return _build(VerificationIssue, {"code": "example_warning", "severity": IssueSeverity.WARNING, "stable_ids": (SOURCE_ID,), "evidence": {"detail": "fixture"}}, overrides)


def make_verification_report(**overrides):
    return _build(VerificationReport, {"schema_version": 1, "run_id": "run-20260830T120000Z", "scope": "all_approved", "scope_key": "all", "verified_at": NOW, "facts": {"checked_memberships": 1}, "issues": (make_verification_issue(),), "complete": False, "report_sha256": SHA256}, overrides)
