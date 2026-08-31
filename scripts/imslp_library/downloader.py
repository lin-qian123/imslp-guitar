from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .client import Clock, PROJECT_USER_AGENT, Transport
from .enums import AttemptPhase, DownloadStatus, ReviewStatus
from .jsonio import atomic_write_json, atomic_write_models, read_json, read_models
from .models import (
    DownloadAttempt,
    DownloadBatchResult,
    DownloadResult,
    DownloadTarget,
    RunState,
    ScoreFile,
    SourceHashReview,
)
from .paths import _assert_safe_read_target, _assert_safe_write_target
from .snapshot import SnapshotError, load_complete_snapshot
from .storage import _validate_pdf, hash_file_sha256, object_path_for_hash, store_verified_pdf


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}
_TERMINAL = {
    DownloadStatus.DOWNLOADED_VERIFIED,
    DownloadStatus.SOURCE_OVERRIDE_VERIFIED,
    DownloadStatus.LOGIN_REQUIRED,
    DownloadStatus.COPYRIGHT_RESTRICTED,
    DownloadStatus.REGION_RESTRICTED,
    DownloadStatus.MEMBERSHIP_REQUIRED,
    DownloadStatus.COMMERCIAL_ONLY,
    DownloadStatus.DELETED,
}
_PART_MANIFEST_KEYS = {
    "source_id",
    "part_path",
    "quarantine_path",
    "size",
    "sha256",
    "reason",
    "quarantined_at",
}
_HTML_EVIDENCE_DETAILS = {
    "human_verification_required",
    "login_required",
    "membership_wait_pending",
    "membership_required",
    "copyright_restricted",
    "region_restricted",
    "commercial_only",
    "deleted",
    "not_a_pdf",
}
_DETAILS_BY_STATUS = {
    DownloadStatus.RETRYABLE: {
        "transport_retryable",
        "http_retryable",
        "stream_interrupted",
        "disk_full",
        "permission_denied",
        "target_unavailable",
        "process_interrupted",
        "retry_exhausted",
    },
    DownloadStatus.HUMAN_VERIFICATION_REQUIRED: {"human_verification_required"},
    DownloadStatus.MEMBERSHIP_WAIT_PENDING: {"membership_wait_pending"},
    DownloadStatus.COPYRIGHT_RESTRICTED: {"copyright_restricted"},
    DownloadStatus.REGION_RESTRICTED: {"region_restricted"},
    DownloadStatus.MEMBERSHIP_REQUIRED: {"membership_required"},
    DownloadStatus.LOGIN_REQUIRED: {"login_required"},
    DownloadStatus.COMMERCIAL_ONLY: {"commercial_only"},
    DownloadStatus.DELETED: {"deleted"},
    DownloadStatus.MANUAL_REVIEW: {
        "unapproved_source",
        "unapproved_redirect",
        "not_a_pdf",
        "http_unexpected",
        "size_mismatch",
        "sha1_mismatch",
        "sha256_mismatch",
        "pdf_invalid",
        "source_metadata_conflict",
        "source_override_invalid",
    },
    DownloadStatus.DOWNLOADED_VERIFIED: {
        "verified",
        "source_hash_review_pending",
        "source_hash_review_resolved",
    },
    DownloadStatus.SOURCE_OVERRIDE_VERIFIED: {"source_override_verified"},
}


class DownloadError(RuntimeError):
    """A downloader trust, persistence, or replay invariant failed."""


def _safe_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id must be a safe canonical path component")
    return run_id


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise DownloadError("download path escapes library root") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(root: Path, target: Path, body: bytes, label: str) -> None:
    _assert_safe_write_target(root, target, label)
    if target.is_symlink():
        raise DownloadError(f"{label} cannot be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_write_target(root, target, label)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _manifest_path(root: Path, run_id: str) -> Path:
    return root / "metadata/runs" / f"{run_id}-download-attempts.json"


def _state_path(root: Path, run_id: str) -> Path:
    return root / "metadata/runs" / f"{run_id}-state.json"


def _part_path(root: Path, run_id: str, source_id: str) -> Path:
    match = re.fullmatch(r"source:f([1-9][0-9]*)@r(0|[1-9][0-9]*)", source_id)
    if match is None:
        raise DownloadError("source_id is not canonical")
    return root / "objects/.parts" / run_id / f"f{match.group(1)}-r{match.group(2)}.part"


def _source_revision(source_id: str) -> int:
    match = re.fullmatch(r"source:f[1-9][0-9]*@r(0|[1-9][0-9]*)", source_id)
    if match is None:
        raise DownloadError("source_id is not canonical")
    return int(match.group(1))


def _success_evidence_path(root: Path, run_id: str, source_id: str, attempt_number: int) -> Path:
    return root / "metadata/runs" / run_id / "evidence" / f"{source_id}-{attempt_number}-success.json"


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_object_bytes(
    root: Path,
    *,
    size: int,
    sha1: str | None,
    sha256: str,
    object_path: str,
) -> Path:
    expected = object_path_for_hash(root, sha256)
    if object_path != _relative(root, expected):
        raise DownloadError("success evidence object path is not canonical")
    try:
        _assert_safe_read_target(root, expected, "verified download object")
    except ValueError as exc:
        raise DownloadError("verified download object is missing, unsafe or not regular") from exc
    if expected.is_symlink() or not expected.exists() or not stat.S_ISREG(expected.lstat().st_mode) or expected.stat().st_nlink < 1:
        raise DownloadError("verified download object is missing, unsafe or not regular")
    if expected.stat().st_size != size or hash_file_sha256(expected) != sha256:
        raise DownloadError("verified download object size or SHA-256 mismatch")
    if sha1 is not None and _sha1_file(expected) != sha1:
        raise DownloadError("verified download object SHA-1 mismatch")
    try:
        _validate_pdf(expected)
    except (OSError, ValueError) as exc:
        raise DownloadError("verified download object is not a valid PDF") from exc
    return expected


def _write_success_evidence(
    root: Path,
    run_id: str,
    score: ScoreFile,
    attempt_number: int,
    *,
    size: int,
    sha256: str,
    verified_at: datetime,
) -> tuple[str, str]:
    path = _success_evidence_path(root, run_id, score.source_id, attempt_number)
    payload = {
        "schema_version": 1,
        "model_type": "DownloadSuccessEvidence",
        "run_id": run_id,
        "source_id": score.source_id,
        "page_revision_id": score.page_revision_id,
        "size": size,
        "sha1": score.sha1_imslp,
        "sha256": sha256,
        "object_path": _relative(root, object_path_for_hash(root, sha256)),
        "source_hash_missing": score.source_hash_missing,
        "verified_at": verified_at.isoformat(),
    }
    atomic_write_json(path, payload)
    return _relative(root, path), hash_file_sha256(path)


def _read_success_evidence(
    root: Path,
    run_id: str,
    attempt: DownloadAttempt,
    frozen_score: ScoreFile | None,
) -> dict[str, object]:
    expected_path = _success_evidence_path(root, run_id, attempt.source_id, attempt.attempt_number)
    expected_relative = _relative(root, expected_path)
    if attempt.evidence_path != expected_relative or attempt.evidence_sha256 is None:
        raise DownloadError("downloaded success requires canonical object evidence")
    try:
        _assert_safe_read_target(root, expected_path, "download success evidence")
    except ValueError as exc:
        raise DownloadError("download success evidence path is unsafe") from exc
    if expected_path.is_symlink():
        raise DownloadError("download success evidence cannot be a symlink")
    try:
        payload = read_json(expected_path)
        verified_at = datetime.fromisoformat(payload["verified_at"]) if isinstance(payload.get("verified_at"), str) else None
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DownloadError("invalid download success evidence") from exc
    keys = {
        "schema_version",
        "model_type",
        "run_id",
        "source_id",
        "page_revision_id",
        "size",
        "sha1",
        "sha256",
        "object_path",
        "source_hash_missing",
        "verified_at",
    }
    valid = (
        set(payload) == keys
        and payload["schema_version"] == 1
        and payload["model_type"] == "DownloadSuccessEvidence"
        and payload["run_id"] == run_id
        and payload["source_id"] == attempt.source_id
        and payload["page_revision_id"] == _source_revision(attempt.source_id)
        and frozen_score is not None
        and payload["source_id"] == frozen_score.source_id
        and payload["page_revision_id"] == frozen_score.page_revision_id
        and type(payload["size"]) is int
        and payload["size"] >= 0
        and (frozen_score.expected_size is None or payload["size"] == frozen_score.expected_size)
        and payload["size"] == attempt.bytes_written
        and (payload["sha1"] is None or isinstance(payload["sha1"], str) and _SHA1_RE.fullmatch(payload["sha1"]) is not None)
        and payload["sha1"] == frozen_score.sha1_imslp
        and isinstance(payload["sha256"], str)
        and _SHA256_RE.fullmatch(payload["sha256"]) is not None
        and isinstance(payload["object_path"], str)
        and type(payload["source_hash_missing"]) is bool
        and payload["source_hash_missing"] == (payload["sha1"] is None)
        and payload["source_hash_missing"] == frozen_score.source_hash_missing
        and verified_at is not None
        and verified_at.tzinfo is not None
        and verified_at.utcoffset() is not None
        and attempt.completed_at is not None
        and attempt.started_at <= verified_at <= attempt.completed_at
        and hash_file_sha256(expected_path) == attempt.evidence_sha256
    )
    if not valid:
        raise DownloadError("download success evidence identity or digest mismatch")
    _validate_object_bytes(
        root,
        size=payload["size"],
        sha1=payload["sha1"],
        sha256=payload["sha256"],
        object_path=payload["object_path"],
    )
    return payload


def _strict_override_for_attempt(root: Path, attempt: DownloadAttempt) -> dict[str, object]:
    path = root / "metadata/overrides/download_sources" / f"{attempt.source_id}.json"
    if attempt.evidence_path != _relative(root, path) or attempt.evidence_sha256 is None:
        raise DownloadError("override-backed attempt requires canonical override evidence")
    try:
        _assert_safe_read_target(root, path, "download source override")
    except ValueError as exc:
        raise DownloadError("override-backed attempt evidence path is unsafe") from exc
    if path.is_symlink() or not path.is_file() or hash_file_sha256(path) != attempt.evidence_sha256:
        raise DownloadError("override-backed attempt evidence is missing or changed")
    payload, error = _strict_override_payload(path, attempt.source_id, _source_revision(attempt.source_id))
    if error is not None or payload is None:
        raise DownloadError("override-backed attempt evidence is invalid")
    return payload


def _validate_resolved_source_review(root: Path, attempt: DownloadAttempt, authority: dict[str, object]) -> str:
    path = root / "metadata/overrides/source_hash_reviews" / f"{attempt.source_id}.json"
    try:
        _assert_safe_read_target(root, path, "source hash review")
    except ValueError as exc:
        raise DownloadError("resolved source hash review path is unsafe") from exc
    if path.is_symlink() or not path.is_file():
        raise DownloadError("resolved source hash review evidence is missing or unsafe")
    try:
        review = SourceHashReview.from_dict(read_json(path))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DownloadError("resolved source hash review evidence is invalid") from exc
    if (
        review.source_id != attempt.source_id
        or review.page_revision_id != authority["page_revision_id"]
        or review.object_sha256 != authority["sha256"]
        or review.decision != "accept_structural_without_source_hash"
        or not review.reviewer_note.strip()
    ):
        raise DownloadError("resolved source hash review evidence replay detected")
    return _relative(root, path)


def _read_attempts(root: Path, run_id: str) -> tuple[DownloadAttempt, ...]:
    path = _manifest_path(root, run_id)
    _assert_safe_read_target(root, path, "download attempt manifest")
    if path.is_symlink():
        raise DownloadError("download attempt manifest cannot be a symlink")
    try:
        values = read_models(path, "DownloadAttemptManifest")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DownloadError("invalid download attempt manifest") from exc
    attempts = tuple(values)
    if any(not isinstance(item, DownloadAttempt) or item.run_id != run_id for item in attempts):
        raise DownloadError("download attempt manifest run identity mismatch")
    frozen_scores = _frozen_score_map(root, run_id)
    seen: set[tuple[str, int]] = set()
    previous: dict[str, int] = {}
    previous_attempt: dict[str, DownloadAttempt] = {}
    terminal: set[str] = set()
    for item in attempts:
        identity = item.source_id, item.attempt_number
        if identity in seen or item.attempt_number != previous.get(item.source_id, 0) + 1:
            raise DownloadError("download attempt numbers must be unique and contiguous per source")
        if item.source_id in terminal:
            raise DownloadError("download attempt follows an irreversible terminal status")
        predecessor = previous_attempt.get(item.source_id)
        if predecessor is not None:
            if predecessor.phase is not AttemptPhase.FINISHED:
                raise DownloadError("download history continues after an unfinished attempt")
            if (
                item.started_at < predecessor.started_at
                or predecessor.completed_at is None
                or item.started_at < predecessor.completed_at
            ):
                raise DownloadError("download attempt history is not chronological")
            if predecessor.status is DownloadStatus.MANUAL_REVIEW and predecessor.review_status is not ReviewStatus.RESOLVED:
                raise DownloadError("download history retries an unresolved manual review")
            if predecessor.status is DownloadStatus.MEMBERSHIP_WAIT_PENDING:
                if predecessor.retry_after is None or item.started_at < predecessor.retry_after:
                    raise DownloadError("membership wait history retries before retry_after")
        canonical_part = _relative(root, _part_path(root, run_id, item.source_id))
        if item.part_path is not None and item.part_path != canonical_part:
            raise DownloadError("download attempt part path is not canonical for its run and source")
        if item.phase in {AttemptPhase.STARTED, AttemptPhase.STREAMING} and item.part_path != canonical_part:
            raise DownloadError("unfinished download attempt lacks its canonical part path")
        expected_evidence: str | None = None
        evidence_kind: str | None = None
        if item.phase in {AttemptPhase.STARTED, AttemptPhase.STREAMING}:
            if item.review_status is not ReviewStatus.NOT_REQUIRED or item.retry_after is not None or item.detail_code not in {"request_started", "streaming"}:
                raise DownloadError("unfinished attempt has invalid status semantics")
        else:
            allowed_details = _DETAILS_BY_STATUS.get(item.status)
            if allowed_details is None or item.detail_code not in allowed_details:
                raise DownloadError("finished attempt status/detail semantics are invalid")
            if item.status is DownloadStatus.MEMBERSHIP_WAIT_PENDING:
                if item.retry_after is None:
                    raise DownloadError("membership wait attempt requires retry_after")
            elif item.retry_after is not None:
                raise DownloadError("only membership wait attempts may carry retry_after")
            if item.status is DownloadStatus.MANUAL_REVIEW:
                if item.review_status not in {ReviewStatus.PENDING, ReviewStatus.RESOLVED}:
                    raise DownloadError("manual review attempt has invalid review status")
            elif item.status is DownloadStatus.DOWNLOADED_VERIFIED:
                if item.review_status not in {ReviewStatus.NOT_REQUIRED, ReviewStatus.PENDING, ReviewStatus.RESOLVED}:
                    raise DownloadError("downloaded attempt has invalid review status")
            elif item.status is DownloadStatus.SOURCE_OVERRIDE_VERIFIED:
                if item.review_status is not ReviewStatus.RESOLVED:
                    raise DownloadError("source override success must be resolved")
            elif item.review_status is not ReviewStatus.NOT_REQUIRED:
                raise DownloadError("non-review attempt cannot carry review status")
        if item.status is DownloadStatus.MANUAL_REVIEW and item.review_status is ReviewStatus.RESOLVED:
            expected_evidence = f"metadata/overrides/download_sources/{item.source_id}.json"
            evidence_kind = "override"
        elif item.status is DownloadStatus.DOWNLOADED_VERIFIED:
            expected_evidence = _relative(
                root,
                _success_evidence_path(root, run_id, item.source_id, item.attempt_number),
            )
            evidence_kind = "success"
        elif item.status is DownloadStatus.SOURCE_OVERRIDE_VERIFIED:
            expected_evidence = f"metadata/overrides/download_sources/{item.source_id}.json"
            evidence_kind = "override"
        elif item.detail_code in _HTML_EVIDENCE_DETAILS:
            expected_evidence = _relative(
                root,
                _evidence_path(root, run_id, item.source_id, item.attempt_number, "html"),
            )
            evidence_kind = "html"
        elif item.detail_code == "http_unexpected":
            expected_evidence = _relative(
                root,
                _evidence_path(root, run_id, item.source_id, item.attempt_number, "bin"),
            )
            evidence_kind = "bin"
        elif item.status is DownloadStatus.MANUAL_REVIEW and item.detail_code in {
            "size_mismatch",
            "sha1_mismatch",
            "sha256_mismatch",
            "pdf_invalid",
        }:
            expected_evidence = _relative(
                root,
                _invalid_path(root, run_id, item.source_id, item.attempt_number),
            )
            evidence_kind = "invalid"
        if (item.evidence_path is None) != (expected_evidence is None):
            raise DownloadError("download attempt evidence is required or forbidden by its status")
        if expected_evidence is not None and item.evidence_path != expected_evidence:
            raise DownloadError("download attempt evidence path does not match its status identity")
        if item.evidence_path is not None:
            evidence = Path(item.evidence_path)
            if evidence.is_absolute() or ".." in evidence.parts:
                raise DownloadError("download attempt evidence path is unsafe")
            evidence_path = root / evidence
            try:
                _assert_safe_read_target(root, evidence_path, "download attempt evidence")
            except ValueError as exc:
                raise DownloadError("download attempt evidence path is unsafe") from exc
            if evidence_path.is_symlink() or not evidence_path.is_file() or hash_file_sha256(evidence_path) != item.evidence_sha256:
                raise DownloadError("download attempt evidence bytes do not match their checkpoint")
            if evidence_kind == "html":
                body = evidence_path.read_bytes()
                classified_status, classified_detail, wait_seconds = _classify_html(body)
                if classified_status is not item.status or classified_detail != item.detail_code:
                    raise DownloadError("HTML evidence content does not match terminal status")
                if item.status is DownloadStatus.MEMBERSHIP_WAIT_PENDING:
                    if (
                        wait_seconds is None
                        or item.completed_at is None
                        or item.retry_after != item.completed_at + timedelta(seconds=wait_seconds)
                    ):
                        raise DownloadError("membership wait evidence does not match retry_after")
        if evidence_kind == "bin" and (
            item.http_status is None or 200 <= item.http_status < 300
        ):
            raise DownloadError("unexpected HTTP evidence requires a non-success status")
        if item.status in {
            DownloadStatus.DOWNLOADED_VERIFIED,
            DownloadStatus.SOURCE_OVERRIDE_VERIFIED,
        } and (item.http_status is None or not 200 <= item.http_status < 300):
            raise DownloadError("verified success requires a successful HTTP status")
        if item.detail_code == "http_retryable" and item.http_status not in _RETRYABLE_HTTP:
            raise DownloadError("HTTP retry attempt has an invalid status")
        if item.detail_code == "transport_retryable" and item.http_status is not None:
            raise DownloadError("transport retry attempt cannot carry an HTTP status")
        if item.status is DownloadStatus.DOWNLOADED_VERIFIED:
            authority = _read_success_evidence(root, run_id, item, frozen_scores.get(item.source_id))
            if item.review_status is ReviewStatus.NOT_REQUIRED and authority["source_hash_missing"]:
                raise DownloadError("missing source hash success cannot skip review")
            if item.review_status in {ReviewStatus.PENDING, ReviewStatus.RESOLVED} and not authority["source_hash_missing"]:
                raise DownloadError("known source hash success cannot require review")
            if item.review_status is ReviewStatus.RESOLVED:
                _validate_resolved_source_review(root, item, authority)
        elif item.status is DownloadStatus.SOURCE_OVERRIDE_VERIFIED:
            override = _strict_override_for_attempt(root, item)
            _validate_object_bytes(
                root,
                size=override["expected_size"],
                sha1=override["sha1"],
                sha256=override["sha256"],
                object_path=_relative(root, object_path_for_hash(root, override["sha256"])),
            )
        elif item.status is DownloadStatus.MANUAL_REVIEW and item.review_status is ReviewStatus.RESOLVED:
            _strict_override_for_attempt(root, item)
        seen.add(identity)
        previous[item.source_id] = item.attempt_number
        previous_attempt[item.source_id] = item
        if item.phase is AttemptPhase.FINISHED and item.status in _TERMINAL:
            terminal.add(item.source_id)
    return attempts


def _write_attempts(root: Path, run_id: str, attempts: Iterable[DownloadAttempt]) -> None:
    path = _manifest_path(root, run_id)
    _assert_safe_write_target(root, path, "download attempt manifest")
    if path.is_symlink():
        raise DownloadError("download attempt manifest cannot be a symlink")
    atomic_write_models(path, "DownloadAttemptManifest", tuple(attempts))


def _load_state(root: Path, run_id: str) -> RunState:
    path = _state_path(root, run_id)
    _assert_safe_read_target(root, path, "run state")
    if path.is_symlink():
        raise DownloadError("run state cannot be a symlink")
    try:
        state = RunState.from_dict(read_json(path))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DownloadError("invalid RunState") from exc
    if state.run_id != run_id or state.snapshot_path != f"metadata/runs/{run_id}.json" or state.snapshot_sha256 is None:
        raise DownloadError("RunState is not bound to a complete canonical snapshot")
    return state


def _ensure_manifest(
    root: Path,
    run_id: str,
    clock: Clock,
    transition_hook: Callable[[str], None] | None,
) -> tuple[RunState, tuple[DownloadAttempt, ...]]:
    try:
        load_complete_snapshot(root, run_id)
    except (SnapshotError, ValueError) as exc:
        raise DownloadError("downloads require a valid complete frozen snapshot") from exc
    state = _load_state(root, run_id)
    path = _manifest_path(root, run_id)
    expected = _relative(root, path)
    if state.download_attempt_manifest_path not in {None, expected}:
        raise DownloadError("RunState points to a noncanonical download attempt manifest")
    if path.exists() or path.is_symlink():
        attempts = _read_attempts(root, run_id)
    else:
        if state.download_attempt_manifest_path is not None:
            raise DownloadError("RunState download manifest pointer is dangling")
        _write_attempts(root, run_id, ())
        attempts = ()
        if transition_hook is not None:
            transition_hook("manifest_created")
    if state.download_attempt_manifest_path is None:
        updated = replace(state, download_attempt_manifest_path=expected, updated_at=clock.now())
        atomic_write_json(_state_path(root, run_id), updated.to_dict())
        state = updated
        if transition_hook is not None:
            transition_hook("state_updated")
    return state, attempts


def _associations(targets: tuple[DownloadTarget, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pairs = tuple(sorted({(item.category_name, item.membership_id) for item in targets}))
    return tuple(item[0] for item in pairs), tuple(item[1] for item in pairs)


def _empty_result(source_id: str, status: DownloadStatus, now: datetime, detail: str, *, retry_after: datetime | None = None, http_status: int | None = None, evidence_path: str | None = None, review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED) -> DownloadResult:
    return DownloadResult(
        source_id=source_id,
        status=status,
        review_status=review_status,
        review_evidence_path=None,
        attempted_at=now,
        retry_after=retry_after,
        http_status=http_status,
        evidence_path=evidence_path,
        detail=detail,
        size=None,
        sha1=None,
        sha256=None,
        source_hash_missing=True,
    )


def _batch(targets: tuple[DownloadTarget, ...], result: DownloadResult) -> DownloadBatchResult:
    categories, memberships = _associations(targets)
    return DownloadBatchResult(result.source_id, categories, memberships, result)


def _group_targets(targets: Iterable[DownloadTarget]) -> tuple[tuple[DownloadTarget, ...], ...]:
    groups: dict[str, list[DownloadTarget]] = {}
    for target in targets:
        if not isinstance(target, DownloadTarget):
            raise TypeError("targets must contain DownloadTarget models")
        groups.setdefault(target.score.source_id, []).append(target)
    return tuple(tuple(sorted(values, key=lambda item: (item.category_name, item.membership_id))) for _, values in sorted(groups.items()))


def _restore_associations(
    targets: tuple[DownloadTarget, ...],
    latest: DownloadAttempt | None,
) -> tuple[DownloadTarget, ...]:
    if latest is None:
        return targets
    score = targets[0].score
    values = {(item.category_name, item.membership_id): item for item in targets}
    for category_name, membership_id in zip(latest.category_names, latest.membership_ids, strict=True):
        values.setdefault(
            (category_name, membership_id),
            DownloadTarget(category_name=category_name, membership_id=membership_id, score=score),
        )
    return tuple(values[key] for key in sorted(values))


def _score_matches(left: ScoreFile, right: ScoreFile) -> bool:
    return left.to_dict() == right.to_dict()


def _frozen_score_map(root: Path, run_id: str) -> dict[str, ScoreFile]:
    try:
        snapshot = load_complete_snapshot(root, run_id)
    except (SnapshotError, ValueError) as exc:
        raise DownloadError("download history requires a valid complete frozen snapshot") from exc
    scores: dict[str, ScoreFile] = {}
    for score in snapshot.score_files:
        if score.source_id in scores:
            raise DownloadError("frozen snapshot contains duplicate source IDs")
        scores[score.source_id] = score
    return scores


def _snapshot_score(root: Path, run_id: str, source_id: str) -> ScoreFile | None:
    return _frozen_score_map(root, run_id).get(source_id)


def _append_started(
    root: Path,
    run_id: str,
    attempts: tuple[DownloadAttempt, ...],
    targets: tuple[DownloadTarget, ...],
    now: datetime,
    part_path: Path,
) -> tuple[tuple[DownloadAttempt, ...], DownloadAttempt]:
    source_id = targets[0].score.source_id
    categories, memberships = _associations(targets)
    number = max((item.attempt_number for item in attempts if item.source_id == source_id), default=0) + 1
    item = DownloadAttempt(
        run_id=run_id,
        attempt_number=number,
        category_names=categories,
        membership_ids=memberships,
        source_id=source_id,
        phase=AttemptPhase.STARTED,
        status=DownloadStatus.NOT_STARTED,
        review_status=ReviewStatus.NOT_REQUIRED,
        started_at=now,
        completed_at=None,
        http_status=None,
        evidence_path=None,
        evidence_sha256=None,
        part_path=_relative(root, part_path),
        bytes_written=0,
        retry_after=None,
        detail_code="request_started",
    )
    updated = attempts + (item,)
    _write_attempts(root, run_id, updated)
    return updated, item


def _replace_attempt(root: Path, run_id: str, attempts: tuple[DownloadAttempt, ...], item: DownloadAttempt) -> tuple[DownloadAttempt, ...]:
    matches = [index for index, value in enumerate(attempts) if (value.source_id, value.attempt_number) == (item.source_id, item.attempt_number)]
    if len(matches) != 1:
        raise DownloadError("attempt checkpoint identity is ambiguous")
    values = list(attempts)
    values[matches[0]] = item
    updated = tuple(values)
    _write_attempts(root, run_id, updated)
    return updated


def _reconcile_unfinished_attempts(
    root: Path,
    run_id: str,
    attempts: tuple[DownloadAttempt, ...],
    clock: Clock,
) -> tuple[DownloadAttempt, ...]:
    updated = attempts
    latest_by_source: dict[str, DownloadAttempt] = {}
    for item in attempts:
        latest_by_source[item.source_id] = item
    for item in latest_by_source.values():
        if item.phase is AttemptPhase.FINISHED:
            continue
        part = _part_path(root, run_id, item.source_id)
        if part.is_symlink():
            raise DownloadError("interrupted download part cannot be a symlink")
        if part.exists():
            details = part.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size < item.bytes_written:
                raise DownloadError("interrupted download part does not match its checkpoint")
            bytes_written = details.st_size
        else:
            if item.bytes_written != 0:
                raise DownloadError("interrupted download checkpoint lost its persisted part")
            bytes_written = 0
        recovered = replace(
            item,
            phase=AttemptPhase.FINISHED,
            status=DownloadStatus.RETRYABLE,
            review_status=ReviewStatus.NOT_REQUIRED,
            completed_at=clock.now(),
            bytes_written=bytes_written,
            detail_code="process_interrupted",
        )
        updated = _replace_attempt(root, run_id, updated, recovered)
    return updated


def _reconcile_success_part_cleanup(
    root: Path,
    run_id: str,
    attempts: tuple[DownloadAttempt, ...],
) -> None:
    latest_by_source: dict[str, DownloadAttempt] = {}
    for item in attempts:
        latest_by_source[item.source_id] = item
    for item in latest_by_source.values():
        if item.status not in {DownloadStatus.DOWNLOADED_VERIFIED, DownloadStatus.SOURCE_OVERRIDE_VERIFIED}:
            continue
        part = _part_path(root, run_id, item.source_id)
        if not part.exists() and not part.is_symlink():
            continue
        if part.is_symlink() or not stat.S_ISREG(part.lstat().st_mode) or part.stat().st_nlink != 1 or part.stat().st_size != item.bytes_written:
            raise DownloadError("successful download left an invalid cleanup part")
        digest = hash_file_sha256(part)
        stored = object_path_for_hash(root, digest)
        if stored.is_symlink() or not stored.is_file() or stored.stat().st_size != item.bytes_written or hash_file_sha256(stored) != digest:
            raise DownloadError("successful download cleanup part has no matching stored object")
        part.unlink()
        _fsync_directory(part.parent)


def _finish_attempt(
    root: Path,
    run_id: str,
    attempts: tuple[DownloadAttempt, ...],
    item: DownloadAttempt,
    clock: Clock,
    status: DownloadStatus,
    detail: str,
    *,
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED,
    http_status: int | None = None,
    evidence_path: str | None = None,
    evidence_sha256: str | None = None,
    retry_after: datetime | None = None,
    bytes_written: int | None = None,
    completed_at: datetime | None = None,
) -> tuple[tuple[DownloadAttempt, ...], DownloadAttempt]:
    finished = replace(
        item,
        phase=AttemptPhase.FINISHED,
        status=status,
        review_status=review_status,
        completed_at=clock.now() if completed_at is None else completed_at,
        http_status=http_status,
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha256,
        bytes_written=item.bytes_written if bytes_written is None else bytes_written,
        retry_after=retry_after,
        detail_code=detail,
    )
    return _replace_attempt(root, run_id, attempts, finished), finished


def _approved_imslp_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    return (
        parsed.scheme.casefold() == "https"
        and parsed.username is None
        and parsed.password is None
        and (host == "imslp.org" or host.endswith(".imslp.org"))
    )


def _strict_override_payload(
    path: Path,
    source_id: str,
    page_revision_id: int,
) -> tuple[dict[str, object] | None, str | None]:
    try:
        payload = read_json(path)
        approved = datetime.fromisoformat(payload["approved_at"]) if isinstance(payload.get("approved_at"), str) else None
        parsed_url = urllib.parse.urlsplit(payload["url"]) if isinstance(payload.get("url"), str) else None
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, "source_override_invalid"
    keys = {"schema_version", "source_id", "page_revision_id", "url", "legal_source_note", "expected_size", "sha1", "sha256", "approved_at"}
    valid = (
        set(payload) == keys
        and payload["schema_version"] == 1
        and payload["source_id"] == source_id
        and payload["page_revision_id"] == page_revision_id
        and isinstance(payload["url"], str)
        and parsed_url is not None
        and parsed_url.scheme.casefold() == "https"
        and parsed_url.hostname is not None
        and parsed_url.username is None
        and parsed_url.password is None
        and isinstance(payload["legal_source_note"], str)
        and bool(payload["legal_source_note"].strip())
        and type(payload["expected_size"]) is int
        and payload["expected_size"] > 0
        and isinstance(payload["sha1"], str)
        and _SHA1_RE.fullmatch(payload["sha1"]) is not None
        and isinstance(payload["sha256"], str)
        and _SHA256_RE.fullmatch(payload["sha256"]) is not None
        and approved is not None
        and approved.tzinfo is not None
        and approved.utcoffset() is not None
    )
    if not valid:
        return None, "source_override_invalid"
    return payload, None


def _read_override(root: Path, target: DownloadTarget) -> tuple[dict[str, object] | None, str | None]:
    path = root / "metadata/overrides/download_sources" / f"{target.score.source_id}.json"
    _assert_safe_read_target(root, path, "download source override")
    if not path.exists() and not path.is_symlink():
        return None, None
    if path.is_symlink():
        return None, "source_override_invalid"
    payload, error = _strict_override_payload(path, target.score.source_id, target.score.page_revision_id)
    if error is not None or payload is None:
        return None, "source_override_invalid"
    return payload, _relative(root, path)


def _classify_html(body: bytes) -> tuple[DownloadStatus, str, int | None]:
    text = body.decode("utf-8", errors="replace").casefold()
    if "verify you are human" in text or "captcha" in text or "bot check" in text:
        return DownloadStatus.HUMAN_VERIFICATION_REQUIRED, "human_verification_required", None
    if "please log in" in text or "login required" in text:
        return DownloadStatus.LOGIN_REQUIRED, "login_required", None
    wait = re.search(r"(?:continue|download).*?([0-9]+)\s*seconds", text, re.DOTALL)
    if wait:
        return DownloadStatus.MEMBERSHIP_WAIT_PENDING, "membership_wait_pending", int(wait.group(1))
    if "members only" in text or "membership required" in text:
        return DownloadStatus.MEMBERSHIP_REQUIRED, "membership_required", None
    if "copyright restricted" in text or "not public domain" in text:
        return DownloadStatus.COPYRIGHT_RESTRICTED, "copyright_restricted", None
    if "not available in your country" in text or "regional restriction" in text:
        return DownloadStatus.REGION_RESTRICTED, "region_restricted", None
    if "available for purchase" in text or "commercial edition" in text:
        return DownloadStatus.COMMERCIAL_ONLY, "commercial_only", None
    if "file has been deleted" in text or "requested file has been deleted" in text:
        return DownloadStatus.DELETED, "deleted", None
    return DownloadStatus.MANUAL_REVIEW, "not_a_pdf", None


def _evidence_path(root: Path, run_id: str, source_id: str, attempt_number: int, suffix: str) -> Path:
    return root / "metadata/runs" / run_id / "evidence" / f"{source_id}-{attempt_number}.{suffix}"


def _invalid_path(root: Path, run_id: str, source_id: str, attempt_number: int) -> Path:
    safe = source_id.replace(":", "_").replace("@", "_")
    return root / "quarantine/download-invalid" / run_id / f"{safe}-{attempt_number}.bin"


def _attempt_digest(attempt: DownloadAttempt) -> str:
    content = json.dumps(
        attempt.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _invalid_intent_path(root: Path, run_id: str, source_id: str, attempt_number: int) -> Path:
    token = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
    return root / "quarantine/transactions" / f"download-invalid-{run_id}-{token}-{attempt_number}.json"


def _reconcile_invalid_intent(
    root: Path,
    run_id: str,
    intent: Path,
    attempts: tuple[DownloadAttempt, ...],
    transition_hook: Callable[[str], None] | None = None,
) -> tuple[DownloadAttempt, ...]:
    try:
        _assert_safe_read_target(root, intent, "invalid download quarantine transaction")
    except ValueError as exc:
        raise DownloadError("invalid download quarantine transaction path is unsafe") from exc
    if intent.is_symlink() or not intent.is_file():
        raise DownloadError("invalid download quarantine transaction is missing or unsafe")
    try:
        payload = read_json(intent)
        predecessor = DownloadAttempt.from_dict(payload["predecessor_attempt"])
        target_attempt = DownloadAttempt.from_dict(payload["target_attempt"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DownloadError("invalid DownloadInvalidQuarantineIntent") from exc
    keys = {
        "schema_version",
        "model_type",
        "run_id",
        "source_id",
        "attempt_number",
        "detail_code",
        "source_path",
        "quarantine_path",
        "size",
        "sha256",
        "predecessor_attempt",
        "predecessor_sha256",
        "target_attempt",
        "target_sha256",
    }
    source_id = payload.get("source_id")
    attempt_number = payload.get("attempt_number")
    detail = payload.get("detail_code")
    if not isinstance(source_id, str) or type(attempt_number) is not int:
        raise DownloadError("invalid DownloadInvalidQuarantineIntent identity")
    source = _part_path(root, run_id, source_id)
    quarantine = _invalid_path(root, run_id, source_id, attempt_number)
    expected_target = replace(
        predecessor,
        phase=AttemptPhase.FINISHED,
        status=DownloadStatus.MANUAL_REVIEW,
        review_status=ReviewStatus.PENDING,
        completed_at=target_attempt.completed_at,
        http_status=target_attempt.http_status,
        evidence_path=_relative(root, quarantine),
        evidence_sha256=payload.get("sha256"),
        bytes_written=payload.get("size"),
        detail_code=detail,
    )
    valid = (
        set(payload) == keys
        and payload["schema_version"] == 1
        and payload["model_type"] == "DownloadInvalidQuarantineIntent"
        and payload["run_id"] == run_id
        and source_id == predecessor.source_id == target_attempt.source_id
        and attempt_number == predecessor.attempt_number == target_attempt.attempt_number
        and detail in {"size_mismatch", "sha1_mismatch", "sha256_mismatch", "pdf_invalid"}
        and payload["source_path"] == _relative(root, source)
        and payload["quarantine_path"] == _relative(root, quarantine)
        and type(payload["size"]) is int
        and payload["size"] >= 0
        and isinstance(payload["sha256"], str)
        and _SHA256_RE.fullmatch(payload["sha256"]) is not None
        and predecessor.run_id == run_id
        and predecessor.phase in {AttemptPhase.STARTED, AttemptPhase.STREAMING}
        and predecessor.status is DownloadStatus.NOT_STARTED
        and predecessor.part_path == _relative(root, source)
        and predecessor.bytes_written == payload["size"]
        and target_attempt == expected_target
        and payload["predecessor_sha256"] == _attempt_digest(predecessor)
        and payload["target_sha256"] == _attempt_digest(target_attempt)
        and intent == _invalid_intent_path(root, run_id, source_id, attempt_number)
    )
    if not valid:
        raise DownloadError("DownloadInvalidQuarantineIntent binding mismatch")
    current = [
        item
        for item in attempts
        if (item.source_id, item.attempt_number) == (source_id, attempt_number)
    ]
    if len(current) != 1 or current[0] not in {predecessor, target_attempt}:
        raise DownloadError("invalid quarantine transaction attempt checkpoint mismatch")
    for path, label in ((source, "invalid download source"), (quarantine, "invalid download quarantine")):
        try:
            _assert_safe_write_target(root, path, label)
        except ValueError as exc:
            raise DownloadError(f"{label} path is unsafe") from exc
    source_present = source.exists() or source.is_symlink()
    quarantine_present = quarantine.exists() or quarantine.is_symlink()
    if source_present == quarantine_present:
        raise DownloadError("invalid download quarantine transaction requires exactly one retained copy")
    if quarantine_present:
        if (
            quarantine.is_symlink()
            or not quarantine.is_file()
            or quarantine.stat().st_nlink != 1
            or quarantine.stat().st_size != payload["size"]
            or hash_file_sha256(quarantine) != payload["sha256"]
        ):
            raise DownloadError("invalid download quarantine destination mismatch")
    else:
        if (
            source.is_symlink()
            or not source.is_file()
            or source.stat().st_nlink != 1
            or source.stat().st_size != payload["size"]
            or hash_file_sha256(source) != payload["sha256"]
        ):
            raise DownloadError("invalid download quarantine source mismatch")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, quarantine)
        _fsync_directory(source.parent)
        _fsync_directory(quarantine.parent)
        if transition_hook is not None:
            transition_hook("invalid_moved")
    updated = attempts
    if current[0] == predecessor:
        updated = _replace_attempt(root, run_id, attempts, target_attempt)
        if transition_hook is not None:
            transition_hook("invalid_attempt_checkpointed")
    if transition_hook is not None:
        transition_hook("invalid_before_cleanup")
    intent.unlink()
    _fsync_directory(intent.parent)
    return updated


def _reconcile_invalid_intents(
    root: Path,
    run_id: str,
    attempts: tuple[DownloadAttempt, ...],
) -> tuple[DownloadAttempt, ...]:
    transactions = root / "quarantine/transactions"
    if not transactions.exists():
        return attempts
    if transactions.is_symlink() or not transactions.is_dir():
        raise DownloadError("quarantine transaction directory is unsafe")
    updated = attempts
    for intent in sorted(transactions.glob(f"download-invalid-{run_id}-*.json")):
        updated = _reconcile_invalid_intent(root, run_id, intent, updated)
    return updated


def _quarantine_invalid_transaction(
    root: Path,
    run_id: str,
    attempts: tuple[DownloadAttempt, ...],
    predecessor: DownloadAttempt,
    clock: Clock,
    detail: str,
    http_status: int | None,
    transition_hook: Callable[[str], None] | None,
) -> tuple[tuple[DownloadAttempt, ...], DownloadAttempt]:
    source = _part_path(root, run_id, predecessor.source_id)
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise DownloadError("invalid PDF part is missing or unsafe")
    size = source.stat().st_size
    digest = hash_file_sha256(source)
    quarantine = _invalid_path(root, run_id, predecessor.source_id, predecessor.attempt_number)
    intent = _invalid_intent_path(root, run_id, predecessor.source_id, predecessor.attempt_number)
    _assert_safe_write_target(root, intent, "invalid download quarantine transaction")
    if intent.exists() or intent.is_symlink() or quarantine.exists() or quarantine.is_symlink():
        raise DownloadError("invalid download quarantine transaction target already exists")
    target = replace(
        predecessor,
        phase=AttemptPhase.FINISHED,
        status=DownloadStatus.MANUAL_REVIEW,
        review_status=ReviewStatus.PENDING,
        completed_at=clock.now(),
        http_status=http_status,
        evidence_path=_relative(root, quarantine),
        evidence_sha256=digest,
        bytes_written=size,
        detail_code=detail,
    )
    atomic_write_json(
        intent,
        {
            "schema_version": 1,
            "model_type": "DownloadInvalidQuarantineIntent",
            "run_id": run_id,
            "source_id": predecessor.source_id,
            "attempt_number": predecessor.attempt_number,
            "detail_code": detail,
            "source_path": _relative(root, source),
            "quarantine_path": _relative(root, quarantine),
            "size": size,
            "sha256": digest,
            "predecessor_attempt": predecessor.to_dict(),
            "predecessor_sha256": _attempt_digest(predecessor),
            "target_attempt": target.to_dict(),
            "target_sha256": _attempt_digest(target),
        },
    )
    if transition_hook is not None:
        transition_hook("invalid_intent_written")
    updated = _reconcile_invalid_intent(
        root,
        run_id,
        intent,
        attempts,
        transition_hook,
    )
    return updated, target


def _part_manifest_path(root: Path, run_id: str) -> Path:
    return root / "quarantine/manifests" / f"download-parts-{run_id}.json"


def _read_part_items(root: Path, run_id: str, *, validate_tree: bool = True) -> list[dict[str, object]]:
    path = _part_manifest_path(root, run_id)
    _assert_safe_read_target(root, path, "download part quarantine manifest")
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        raise DownloadError("download part quarantine manifest cannot be a symlink")
    payload = read_json(path)
    if set(payload) != {"schema_version", "model_type", "items"} or payload["schema_version"] != 1 or payload["model_type"] != "DownloadPartQuarantineManifest" or not isinstance(payload["items"], list):
        raise DownloadError("invalid DownloadPartQuarantineManifest envelope")
    items: list[dict[str, object]] = []
    for raw in payload["items"]:
        if not isinstance(raw, dict) or set(raw) != _PART_MANIFEST_KEYS:
            raise DownloadError("invalid DownloadPartQuarantineManifest item")
        try:
            when = datetime.fromisoformat(raw["quarantined_at"]) if isinstance(raw["quarantined_at"], str) else None
        except ValueError:
            when = None
        valid = (
            isinstance(raw["source_id"], str)
            and isinstance(raw["part_path"], str)
            and isinstance(raw["quarantine_path"], str)
            and not Path(raw["part_path"]).is_absolute()
            and not Path(raw["quarantine_path"]).is_absolute()
            and ".." not in Path(raw["part_path"]).parts
            and ".." not in Path(raw["quarantine_path"]).parts
            and type(raw["size"]) is int
            and raw["size"] >= 0
            and isinstance(raw["sha256"], str)
            and _SHA256_RE.fullmatch(raw["sha256"]) is not None
            and raw["reason"] == "restart_from_zero"
            and when is not None
            and when.tzinfo is not None
        )
        if not valid:
            raise DownloadError("invalid DownloadPartQuarantineManifest item")
        quarantine = root / raw["quarantine_path"]
        if quarantine.is_symlink() or not quarantine.is_file() or quarantine.stat().st_size != raw["size"] or hash_file_sha256(quarantine) != raw["sha256"]:
            raise DownloadError("download part quarantine manifest does not match its tree")
        items.append(raw)
    if validate_tree:
        tree_root = root / "quarantine/download-parts" / run_id
        actual = {_relative(root, item) for item in tree_root.rglob("*") if item.is_file()} if tree_root.exists() else set()
        expected = {str(item["quarantine_path"]) for item in items}
        if actual != expected:
            raise DownloadError("download part quarantine tree contains unmanifested files")
    return items


def _part_intent_path(root: Path, run_id: str, source_id: str) -> Path:
    token = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
    return root / "quarantine/transactions" / f"download-part-{run_id}-{token}.json"


def _reconcile_part_intent(
    root: Path,
    run_id: str,
    source_id: str,
    transition_hook: Callable[[str], None] | None = None,
) -> bool:
    intent = _part_intent_path(root, run_id, source_id)
    _assert_safe_read_target(root, intent, "download part quarantine transaction")
    if not intent.exists() and not intent.is_symlink():
        return False
    if intent.is_symlink():
        raise DownloadError("download part quarantine transaction cannot be a symlink")
    payload = read_json(intent)
    expected_keys = {"schema_version", "model_type", "run_id"} | _PART_MANIFEST_KEYS
    if (
        set(payload) != expected_keys
        or payload["schema_version"] != 1
        or payload["model_type"] != "DownloadPartQuarantineIntent"
        or payload["run_id"] != run_id
        or payload["source_id"] != source_id
    ):
        raise DownloadError("invalid DownloadPartQuarantineIntent identity")
    item = {key: payload[key] for key in _PART_MANIFEST_KEYS}
    original = root / str(item["part_path"])
    quarantine = root / str(item["quarantine_path"])
    _assert_safe_write_target(root, original, "download part transaction source")
    _assert_safe_write_target(root, quarantine, "download part transaction destination")
    size = item["size"]
    digest = item["sha256"]
    canonical_part = _part_path(root, run_id, source_id)
    expected_parent = root / "quarantine/download-parts" / run_id
    try:
        quarantined_at = datetime.fromisoformat(str(item["quarantined_at"]))
    except ValueError:
        quarantined_at = None
    if (
        type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or item["part_path"] != _relative(root, canonical_part)
        or quarantine.parent != expected_parent
        or re.fullmatch(rf"{re.escape(canonical_part.stem)}-attempt-[1-9][0-9]*\.part", quarantine.name) is None
        or item["reason"] != "restart_from_zero"
        or quarantined_at is None
        or quarantined_at.tzinfo is None
        or quarantined_at.utcoffset() is None
    ):
        raise DownloadError("invalid DownloadPartQuarantineIntent content identity")
    if quarantine.exists() or quarantine.is_symlink():
        if quarantine.is_symlink() or not quarantine.is_file() or quarantine.stat().st_size != size or hash_file_sha256(quarantine) != digest:
            raise DownloadError("download part quarantine transaction destination mismatch")
    else:
        if original.is_symlink() or not original.is_file() or original.stat().st_size != size or hash_file_sha256(original) != digest:
            raise DownloadError("download part quarantine transaction source mismatch")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(original, quarantine)
        _fsync_directory(original.parent)
        _fsync_directory(quarantine.parent)
        if transition_hook is not None:
            transition_hook("part_moved")
    items = _read_part_items(root, run_id, validate_tree=False)
    if item not in items:
        if any(existing["quarantine_path"] == item["quarantine_path"] for existing in items):
            raise DownloadError("download part quarantine manifest path conflict")
        items.append(item)
        atomic_write_json(
            _part_manifest_path(root, run_id),
            {"schema_version": 1, "model_type": "DownloadPartQuarantineManifest", "items": items},
        )
        if transition_hook is not None:
            transition_hook("part_manifest_updated")
    intent.unlink()
    _fsync_directory(intent.parent)
    _read_part_items(root, run_id)
    return True


def _quarantine_stale_part(
    root: Path,
    run_id: str,
    source_id: str,
    part: Path,
    attempts: tuple[DownloadAttempt, ...],
    clock: Clock,
    transition_hook: Callable[[str], None] | None,
) -> None:
    _reconcile_part_intent(root, run_id, source_id)
    _read_part_items(root, run_id)
    if not part.exists() and not part.is_symlink():
        return
    if part.is_symlink() or not stat.S_ISREG(part.lstat().st_mode) or part.stat().st_nlink != 1:
        raise DownloadError("download part is not an owned regular file")
    relative = _relative(root, part)
    latest = max((item for item in attempts if item.source_id == source_id), key=lambda item: item.attempt_number, default=None)
    if latest is None or latest.part_path != relative or latest.bytes_written != part.stat().st_size or latest.phase is not AttemptPhase.FINISHED or latest.status is not DownloadStatus.RETRYABLE:
        raise DownloadError("download part lacks an exact retryable checkpoint")
    digest = hash_file_sha256(part)
    quarantine = root / "quarantine/download-parts" / run_id / f"{part.stem}-attempt-{latest.attempt_number}.part"
    _assert_safe_write_target(root, quarantine, "download part quarantine")
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    if quarantine.exists() or quarantine.is_symlink():
        raise DownloadError("download part quarantine target already exists")
    item = {
        "source_id": source_id,
        "part_path": relative,
        "quarantine_path": _relative(root, quarantine),
        "size": part.stat().st_size,
        "sha256": digest,
        "reason": "restart_from_zero",
        "quarantined_at": clock.now().isoformat(),
    }
    intent = _part_intent_path(root, run_id, source_id)
    _assert_safe_write_target(root, intent, "download part quarantine transaction")
    atomic_write_json(
        intent,
        {
            "schema_version": 1,
            "model_type": "DownloadPartQuarantineIntent",
            "run_id": run_id,
            **item,
        },
    )
    if transition_hook is not None:
        transition_hook("part_intent_written")
    _reconcile_part_intent(root, run_id, source_id, transition_hook)


def _open_part(path: Path):
    return path.open("xb")


def _io_detail(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "disk_full"
    if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM}:
        return "permission_denied"
    if isinstance(exc, OSError) and exc.errno in {errno.ENOENT, errno.ENODEV, errno.EROFS}:
        return "target_unavailable"
    return "stream_interrupted"


def _result_from_attempt(root: Path, attempt: DownloadAttempt) -> DownloadResult:
    if attempt.status is DownloadStatus.DOWNLOADED_VERIFIED:
        evidence = _read_success_evidence(
            root,
            attempt.run_id,
            attempt,
            _snapshot_score(root, attempt.run_id, attempt.source_id),
        )
        review_path = (
            _validate_resolved_source_review(root, attempt, evidence)
            if attempt.review_status is ReviewStatus.RESOLVED
            else None
        )
        return DownloadResult(
            source_id=attempt.source_id,
            status=attempt.status,
            review_status=attempt.review_status,
            review_evidence_path=review_path,
            attempted_at=attempt.started_at,
            retry_after=None,
            http_status=attempt.http_status,
            evidence_path=attempt.evidence_path,
            detail=attempt.detail_code,
            size=evidence["size"],
            sha1=evidence["sha1"],
            sha256=evidence["sha256"],
            source_hash_missing=evidence["source_hash_missing"],
        )
    if attempt.status is DownloadStatus.SOURCE_OVERRIDE_VERIFIED:
        override = _strict_override_for_attempt(root, attempt)
        _validate_object_bytes(
            root,
            size=override["expected_size"],
            sha1=override["sha1"],
            sha256=override["sha256"],
            object_path=_relative(root, object_path_for_hash(root, override["sha256"])),
        )
        return DownloadResult(
            source_id=attempt.source_id,
            status=attempt.status,
            review_status=ReviewStatus.RESOLVED,
            review_evidence_path=attempt.evidence_path,
            attempted_at=attempt.started_at,
            retry_after=None,
            http_status=attempt.http_status,
            evidence_path=None,
            detail=attempt.detail_code,
            size=override["expected_size"],
            sha1=override["sha1"],
            sha256=override["sha256"],
            source_hash_missing=False,
        )
    return _empty_result(
        attempt.source_id,
        attempt.status if attempt.phase is AttemptPhase.FINISHED else DownloadStatus.NOT_STARTED,
        attempt.started_at,
        attempt.detail_code,
        retry_after=attempt.retry_after,
        http_status=attempt.http_status,
        evidence_path=attempt.evidence_path,
        review_status=attempt.review_status,
    )


def _download_once(
    root: Path,
    run_id: str,
    targets: tuple[DownloadTarget, ...],
    transport: Transport,
    clock: Clock,
    attempts: tuple[DownloadAttempt, ...],
    override: dict[str, object] | None,
    override_path: str | None,
    transition_hook: Callable[[str], None] | None,
) -> tuple[DownloadBatchResult, tuple[DownloadAttempt, ...], bool]:
    target = targets[0]
    score = target.score
    part = _part_path(root, run_id, score.source_id)
    _quarantine_stale_part(root, run_id, score.source_id, part, attempts, clock, transition_hook)
    attempts, started = _append_started(root, run_id, attempts, targets, clock.now(), part)
    if transition_hook is not None:
        transition_hook("attempt_started")
    url = str(override["url"]) if override is not None else score.source_url
    if override is None and not _approved_imslp_url(url):
        attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.MANUAL_REVIEW, "unapproved_source", review_status=ReviewStatus.PENDING)
        return _batch(targets, _empty_result(score.source_id, DownloadStatus.MANUAL_REVIEW, started.started_at, "unapproved_source", review_status=ReviewStatus.PENDING)), attempts, False
    try:
        response = transport.request(
            "GET",
            url,
            headers={"Accept": "application/pdf,text/html;q=0.8", "Accept-Encoding": "identity", "User-Agent": PROJECT_USER_AGENT},
            timeout=(15.0, 60.0),
        )
    except (TimeoutError, ConnectionError, OSError):
        detail = "transport_retryable"
        attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.RETRYABLE, detail)
        return _batch(targets, _empty_result(score.source_id, DownloadStatus.RETRYABLE, started.started_at, detail)), attempts, True
    http_status = int(response.status)
    if http_status in _RETRYABLE_HTTP:
        attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.RETRYABLE, "http_retryable", http_status=http_status)
        return _batch(targets, _empty_result(score.source_id, DownloadStatus.RETRYABLE, started.started_at, "http_retryable", http_status=http_status)), attempts, True
    final_url = str(response.final_url)
    approved_final = final_url == str(override["url"]) if override is not None else _approved_imslp_url(final_url)
    if not approved_final:
        attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.MANUAL_REVIEW, "unapproved_redirect", review_status=ReviewStatus.PENDING, http_status=http_status)
        return _batch(targets, _empty_result(score.source_id, DownloadStatus.MANUAL_REVIEW, started.started_at, "unapproved_redirect", http_status=http_status, review_status=ReviewStatus.PENDING)), attempts, False
    content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
    if content_type in {"text/html", "application/xhtml+xml"}:
        try:
            body = b"".join(response.iter_bytes())
        except (TimeoutError, ConnectionError, OSError):
            attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.RETRYABLE, "stream_interrupted", http_status=http_status)
            return _batch(targets, _empty_result(score.source_id, DownloadStatus.RETRYABLE, started.started_at, "stream_interrupted", http_status=http_status)), attempts, False
        status, detail, wait_seconds = _classify_html(body)
        evidence = _evidence_path(root, run_id, score.source_id, started.attempt_number, "html")
        _atomic_write_bytes(root, evidence, body, "download HTML evidence")
        digest = hashlib.sha256(body).hexdigest()
        classified_at = clock.now()
        retry_after = classified_at + timedelta(seconds=wait_seconds) if wait_seconds is not None else None
        review = ReviewStatus.PENDING if status is DownloadStatus.MANUAL_REVIEW else ReviewStatus.NOT_REQUIRED
        attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, status, detail, review_status=review, http_status=http_status, evidence_path=_relative(root, evidence), evidence_sha256=digest, retry_after=retry_after, completed_at=classified_at)
        result = _empty_result(score.source_id, status, started.started_at, detail, retry_after=retry_after, http_status=http_status, evidence_path=_relative(root, evidence), review_status=review)
        return _batch(targets, result), attempts, False
    if not 200 <= http_status < 300:
        try:
            body = b"".join(response.iter_bytes())
        except (TimeoutError, ConnectionError, OSError):
            attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.RETRYABLE, "stream_interrupted", http_status=http_status)
            return _batch(targets, _empty_result(score.source_id, DownloadStatus.RETRYABLE, started.started_at, "stream_interrupted", http_status=http_status)), attempts, False
        evidence = _evidence_path(root, run_id, score.source_id, started.attempt_number, "bin")
        _atomic_write_bytes(root, evidence, body, "unexpected HTTP response evidence")
        evidence_digest = hashlib.sha256(body).hexdigest()
        relative_evidence = _relative(root, evidence)
        attempts, _ = _finish_attempt(
            root,
            run_id,
            attempts,
            started,
            clock,
            DownloadStatus.MANUAL_REVIEW,
            "http_unexpected",
            review_status=ReviewStatus.PENDING,
            http_status=http_status,
            evidence_path=relative_evidence,
            evidence_sha256=evidence_digest,
        )
        return _batch(
            targets,
            _empty_result(
                score.source_id,
                DownloadStatus.MANUAL_REVIEW,
                started.started_at,
                "http_unexpected",
                http_status=http_status,
                evidence_path=relative_evidence,
                review_status=ReviewStatus.PENDING,
            ),
        ), attempts, False
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_write_target(root, part, "download part")
        with _open_part(part) as output:
            total = 0
            for chunk in response.iter_bytes():
                if not isinstance(chunk, bytes):
                    raise TypeError("response stream yielded a non-bytes chunk")
                output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                total += len(chunk)
                streaming = replace(started, phase=AttemptPhase.STREAMING, bytes_written=total, detail_code="streaming")
                attempts = _replace_attempt(root, run_id, attempts, streaming)
                started = streaming
                if transition_hook is not None:
                    transition_hook("stream_checkpointed")
    except (TimeoutError, ConnectionError, OSError, TypeError) as exc:
        bytes_written = part.stat().st_size if part.exists() and not part.is_symlink() else 0
        detail = _io_detail(exc)
        attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.RETRYABLE, detail, http_status=http_status, bytes_written=bytes_written)
        return _batch(targets, _empty_result(score.source_id, DownloadStatus.RETRYABLE, started.started_at, detail, http_status=http_status)), attempts, False
    size = part.stat().st_size
    digest = hash_file_sha256(part)
    expected_size = int(override["expected_size"]) if override is not None else score.expected_size
    expected_sha1 = str(override["sha1"]) if override is not None else score.sha1_imslp
    expected_sha256 = str(override["sha256"]) if override is not None else None
    detail: str | None = None
    if expected_size is not None and size != expected_size:
        detail = "size_mismatch"
    elif expected_sha1 is not None and hashlib.sha1(part.read_bytes()).hexdigest() != expected_sha1:
        detail = "sha1_mismatch"
    elif expected_sha256 is not None and digest != expected_sha256:
        detail = "sha256_mismatch"
    else:
        try:
            with part.open("rb") as source:
                if source.read(5) != b"%PDF-":
                    raise ValueError("missing PDF header")
            _validate_pdf(part)
        except (OSError, ValueError):
            detail = "pdf_invalid"
    if detail is not None:
        attempts, finished = _quarantine_invalid_transaction(
            root,
            run_id,
            attempts,
            started,
            clock,
            detail,
            http_status,
            transition_hook,
        )
        evidence_path = finished.evidence_path
        result = _empty_result(score.source_id, DownloadStatus.MANUAL_REVIEW, started.started_at, detail, http_status=http_status, evidence_path=evidence_path, review_status=ReviewStatus.PENDING)
        return _batch(targets, result), attempts, False
    validation_score = replace(score, expected_size=expected_size, sha1_imslp=expected_sha1, source_hash_missing=expected_sha1 is None)
    try:
        stored = store_verified_pdf(root, part, validation_score, run_id)
    except OSError as exc:
        detail = _io_detail(exc)
        attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.RETRYABLE, detail, http_status=http_status, bytes_written=size)
        return _batch(targets, _empty_result(score.source_id, DownloadStatus.RETRYABLE, started.started_at, detail, http_status=http_status)), attempts, False
    if transition_hook is not None:
        transition_hook("object_stored")
    if override is not None:
        status = DownloadStatus.SOURCE_OVERRIDE_VERIFIED
        review = ReviewStatus.RESOLVED
        evidence_path = override_path
        evidence_digest = hash_file_sha256(root / override_path) if override_path else None
        result = DownloadResult(score.source_id, status, review, override_path, started.started_at, None, http_status, None, "source_override_verified", stored.size, expected_sha1, stored.sha256, False)
    elif score.source_hash_missing:
        status = DownloadStatus.DOWNLOADED_VERIFIED
        review = ReviewStatus.PENDING
        evidence_path, evidence_digest = _write_success_evidence(
            root,
            run_id,
            score,
            started.attempt_number,
            size=stored.size,
            sha256=stored.sha256,
            verified_at=clock.now(),
        )
        result = DownloadResult(score.source_id, status, review, None, started.started_at, None, http_status, evidence_path, "source_hash_review_pending", stored.size, None, stored.sha256, True)
    else:
        status = DownloadStatus.DOWNLOADED_VERIFIED
        review = ReviewStatus.NOT_REQUIRED
        evidence_path, evidence_digest = _write_success_evidence(
            root,
            run_id,
            score,
            started.attempt_number,
            size=stored.size,
            sha256=stored.sha256,
            verified_at=clock.now(),
        )
        result = DownloadResult(score.source_id, status, review, None, started.started_at, None, http_status, evidence_path, "verified", stored.size, score.sha1_imslp, stored.sha256, False)
    attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, status, result.detail, review_status=review, http_status=http_status, evidence_path=evidence_path, evidence_sha256=evidence_digest, bytes_written=size)
    if transition_hook is not None:
        transition_hook("success_checkpointed")
    part.unlink()
    _fsync_directory(part.parent)
    return _batch(targets, result), attempts, False


def download_batch(
    root: Path,
    run_id: str,
    targets: tuple[DownloadTarget, ...],
    transport: Transport,
    clock: Clock,
    *,
    max_attempts: int = 3,
    batch_limit: int | None = None,
    category_names: tuple[str, ...] | None = None,
    source_ids: tuple[str, ...] | None = None,
    statuses: tuple[DownloadStatus, ...] | None = None,
    capacity_guard: Callable[[Path, tuple[DownloadTarget, ...]], bool | None] | None = None,
    transition_hook: Callable[[str], None] | None = None,
) -> tuple[DownloadBatchResult, ...]:
    root = Path(root)
    _safe_run_id(run_id)
    groups = _group_targets(targets)
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return tuple(_batch(group, _empty_result(group[0].score.source_id, DownloadStatus.RETRYABLE, clock.now(), "target_unavailable")) for group in groups)
    if type(max_attempts) is not int or max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    if batch_limit is not None and (type(batch_limit) is not int or batch_limit <= 0):
        raise ValueError("batch_limit must be positive")
    if statuses is not None and (
        not isinstance(statuses, tuple)
        or any(not isinstance(item, DownloadStatus) for item in statuses)
        or len(set(statuses)) != len(statuses)
    ):
        raise TypeError("statuses must be a unique tuple of DownloadStatus values")
    try:
        _, attempts = _ensure_manifest(root, run_id, clock, transition_hook)
    except DownloadError:
        raise
    selector_status: dict[str, DownloadStatus] = {}
    for item in attempts:
        selector_status[item.source_id] = (
            item.status if item.phase is AttemptPhase.FINISHED else DownloadStatus.NOT_STARTED
        )
    attempts = _reconcile_invalid_intents(root, run_id, attempts)
    attempts = _reconcile_unfinished_attempts(root, run_id, attempts, clock)
    _reconcile_success_part_cleanup(root, run_id, attempts)
    if capacity_guard is not None:
        try:
            passed = capacity_guard(root, targets)
        except OSError:
            passed = False
        if passed is False:
            return tuple(_batch(group, _empty_result(group[0].score.source_id, DownloadStatus.RETRYABLE, clock.now(), "insufficient_capacity")) for group in groups)
    restored_groups: list[tuple[DownloadTarget, ...]] = []
    for group in groups:
        latest = max(
            (item for item in attempts if item.source_id == group[0].score.source_id),
            key=lambda item: item.attempt_number,
            default=None,
        )
        restored_groups.append(_restore_associations(group, latest))
    groups = tuple(restored_groups)
    categories_filter = set(category_names) if category_names is not None else None
    sources_filter = set(source_ids) if source_ids is not None else None
    statuses_filter = set(statuses) if statuses is not None else None
    selected = [
        group
        for group in groups
        if (categories_filter is None or any(item.category_name in categories_filter for item in group))
        and (sources_filter is None or group[0].score.source_id in sources_filter)
        and (
            statuses_filter is None
            or selector_status.get(group[0].score.source_id, DownloadStatus.NOT_STARTED)
            in statuses_filter
        )
    ]
    results: list[DownloadBatchResult] = []
    stopped = False
    started_groups = 0
    for group in selected:
        source_id = group[0].score.source_id
        latest = max((item for item in attempts if item.source_id == source_id), key=lambda item: item.attempt_number, default=None)
        if stopped:
            results.append(_batch(group, _empty_result(source_id, DownloadStatus.NOT_STARTED, clock.now(), "batch_stopped")))
            continue
        if any(not _score_matches(group[0].score, item.score) for item in group[1:]):
            if latest is not None and (latest.status in _TERMINAL or (latest.status is DownloadStatus.MANUAL_REVIEW and latest.review_status is ReviewStatus.PENDING)):
                results.append(
                    _batch(
                        _restore_associations(group, latest),
                        _empty_result(
                            source_id,
                            DownloadStatus.MANUAL_REVIEW,
                            clock.now(),
                            "source_metadata_conflict",
                            review_status=ReviewStatus.PENDING,
                        ),
                    )
                )
                continue
            if batch_limit is not None and started_groups >= batch_limit:
                continue
            started_groups += 1
            attempts, started = _append_started(root, run_id, attempts, group, clock.now(), _part_path(root, run_id, source_id))
            attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.MANUAL_REVIEW, "source_metadata_conflict", review_status=ReviewStatus.PENDING)
            results.append(_batch(group, _empty_result(source_id, DownloadStatus.MANUAL_REVIEW, clock.now(), "source_metadata_conflict", review_status=ReviewStatus.PENDING)))
            continue
        frozen = _snapshot_score(root, run_id, source_id)
        if frozen is None or not _score_matches(frozen, group[0].score):
            if latest is not None and (latest.status in _TERMINAL or (latest.status is DownloadStatus.MANUAL_REVIEW and latest.review_status is ReviewStatus.PENDING)):
                results.append(
                    _batch(
                        _restore_associations(group, latest),
                        _empty_result(
                            source_id,
                            DownloadStatus.MANUAL_REVIEW,
                            clock.now(),
                            "source_metadata_conflict",
                            review_status=ReviewStatus.PENDING,
                        ),
                    )
                )
                continue
            if batch_limit is not None and started_groups >= batch_limit:
                continue
            started_groups += 1
            attempts, started = _append_started(root, run_id, attempts, group, clock.now(), _part_path(root, run_id, source_id))
            attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.MANUAL_REVIEW, "source_metadata_conflict", review_status=ReviewStatus.PENDING)
            results.append(_batch(group, _empty_result(source_id, DownloadStatus.MANUAL_REVIEW, clock.now(), "source_metadata_conflict", review_status=ReviewStatus.PENDING)))
            continue
        group = _restore_associations(group, latest)
        override, override_path = _read_override(root, group[0])
        override_exists = (root / "metadata/overrides/download_sources" / f"{source_id}.json").exists()
        if override_exists and override is None:
            if latest is not None and latest.status in _TERMINAL:
                continue
            if batch_limit is not None and started_groups >= batch_limit:
                continue
            started_groups += 1
            attempts, started = _append_started(root, run_id, attempts, group, clock.now(), _part_path(root, run_id, source_id))
            attempts, _ = _finish_attempt(root, run_id, attempts, started, clock, DownloadStatus.MANUAL_REVIEW, "source_override_invalid", review_status=ReviewStatus.PENDING)
            results.append(_batch(group, _empty_result(source_id, DownloadStatus.MANUAL_REVIEW, clock.now(), "source_override_invalid", review_status=ReviewStatus.PENDING)))
            continue
        if latest is not None and latest.status is DownloadStatus.MANUAL_REVIEW and latest.review_status is ReviewStatus.PENDING and override is not None and override_path is not None:
            resolved_manual = replace(
                latest,
                review_status=ReviewStatus.RESOLVED,
                evidence_path=override_path,
                evidence_sha256=hash_file_sha256(root / override_path),
            )
            attempts = _replace_attempt(root, run_id, attempts, resolved_manual)
            latest = resolved_manual
        if latest is not None:
            if latest.phase is not AttemptPhase.FINISHED:
                raise DownloadError("unfinished attempt requires explicit crash reconciliation")
            if latest.status is DownloadStatus.MEMBERSHIP_WAIT_PENDING and latest.retry_after is not None and clock.now() < latest.retry_after:
                results.append(_batch(group, _result_from_attempt(root, latest)))
                continue
            if latest.status in _TERMINAL:
                if statuses is not None or (
                    latest.status is DownloadStatus.DOWNLOADED_VERIFIED
                    and latest.review_status is ReviewStatus.PENDING
                ):
                    results.append(_batch(group, _result_from_attempt(root, latest)))
                continue
            if latest.status is DownloadStatus.MANUAL_REVIEW and latest.review_status is not ReviewStatus.RESOLVED and override is None:
                results.append(_batch(group, _result_from_attempt(root, latest)))
                continue
        if batch_limit is not None and started_groups >= batch_limit:
            continue
        started_groups += 1
        result: DownloadBatchResult | None = None
        for number in range(max_attempts):
            result, attempts, retry_immediately = _download_once(
                root,
                run_id,
                group,
                transport,
                clock,
                attempts,
                override,
                override_path,
                transition_hook,
            )
            if not retry_immediately:
                break
            if number + 1 == max_attempts:
                last = max((item for item in attempts if item.source_id == source_id), key=lambda item: item.attempt_number)
                attempts, _ = _finish_attempt(root, run_id, attempts, last, clock, DownloadStatus.RETRYABLE, "retry_exhausted", http_status=last.http_status, bytes_written=last.bytes_written)
                result = _batch(group, _empty_result(source_id, DownloadStatus.RETRYABLE, last.started_at, "retry_exhausted", http_status=last.http_status))
        if result is None:
            raise AssertionError("download group produced no result")
        results.append(result)
        if result.result.status is DownloadStatus.HUMAN_VERIFICATION_REQUIRED:
            stopped = True
    return tuple(results)


def download_score(root: Path, run_id: str, target: DownloadTarget, transport: Transport, clock: Clock) -> DownloadResult:
    values = download_batch(root, run_id, (target,), transport, clock)
    if len(values) != 1:
        raise DownloadError("single-score download was skipped by an invalid selector")
    return values[0].result


def resolve_source_hash_review(root: Path, run_id: str, target: DownloadTarget, result: DownloadResult) -> DownloadResult:
    root = Path(root)
    _safe_run_id(run_id)
    attempts = _read_attempts(root, run_id)
    latest = max((item for item in attempts if item.source_id == target.score.source_id), key=lambda item: item.attempt_number, default=None)
    if latest is None or latest.status is not DownloadStatus.DOWNLOADED_VERIFIED or latest.review_status is not ReviewStatus.PENDING:
        raise DownloadError("attempt manifest has no matching pending source-hash review")
    frozen = _snapshot_score(root, run_id, target.score.source_id)
    authority = _read_success_evidence(root, run_id, latest, frozen)
    expected_result = _result_from_attempt(root, latest)
    if (
        result != expected_result
        or result.source_id != target.score.source_id
        or result.status is not DownloadStatus.DOWNLOADED_VERIFIED
        or result.review_status is not ReviewStatus.PENDING
        or not result.source_hash_missing
        or result.sha1 is not None
        or result.sha256 is None
        or authority["page_revision_id"] != target.score.page_revision_id
        or not authority["source_hash_missing"]
        or not target.score.source_hash_missing
        or target.score.sha1_imslp is not None
        or frozen is None
        or not _score_matches(frozen, target.score)
    ):
        raise DownloadError("caller result does not exactly match pending object authority")
    path = root / "metadata/overrides/source_hash_reviews" / f"{target.score.source_id}.json"
    try:
        _assert_safe_read_target(root, path, "source hash review")
    except ValueError as exc:
        raise DownloadError("source hash review path is unsafe") from exc
    if path.is_symlink():
        raise DownloadError("source hash review cannot be a symlink")
    try:
        review = SourceHashReview.from_dict(read_json(path))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DownloadError("invalid source hash review") from exc
    valid = (
        review.source_id == target.score.source_id
        and review.page_revision_id == target.score.page_revision_id
        and review.object_sha256 == authority["sha256"]
        and review.decision == "accept_structural_without_source_hash"
        and bool(review.reviewer_note.strip())
    )
    if not valid:
        raise DownloadError("source hash review replay or rejection detected")
    relative = _relative(root, path)
    updated = replace(latest, review_status=ReviewStatus.RESOLVED)
    _replace_attempt(root, run_id, attempts, updated)
    _read_attempts(root, run_id)
    return replace(expected_result, review_status=ReviewStatus.RESOLVED, review_evidence_path=relative)


def download_completion_blockers(root: Path, run_id: str, clock: Clock) -> tuple[str, ...]:
    attempts = _read_attempts(Path(root), _safe_run_id(run_id))
    latest: dict[str, DownloadAttempt] = {}
    for item in attempts:
        latest[item.source_id] = item
    blockers: set[str] = set()
    for item in latest.values():
        if item.phase is not AttemptPhase.FINISHED:
            blockers.add("download_incomplete")
        elif item.status is DownloadStatus.MEMBERSHIP_WAIT_PENDING:
            blockers.add("membership_wait_elapsed_unretried" if item.retry_after is not None and clock.now() >= item.retry_after else "membership_wait_pending")
        elif item.status is DownloadStatus.DOWNLOADED_VERIFIED and item.review_status is ReviewStatus.PENDING:
            blockers.add("source_hash_review_pending")
        elif item.status is DownloadStatus.MANUAL_REVIEW and item.review_status is ReviewStatus.PENDING:
            blockers.add("manual_review_pending")
    return tuple(sorted(blockers))
