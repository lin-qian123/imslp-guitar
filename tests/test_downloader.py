from __future__ import annotations

import errno
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from pypdf import PdfWriter

from imslp_library.downloader import (
    DownloadError,
    download_batch,
    download_completion_blockers,
    resolve_source_hash_review,
)
from imslp_library.enums import AttemptPhase, DownloadStatus, ReviewStatus
from imslp_library.jsonio import atomic_write_json, read_json, read_models
from imslp_library.models import DownloadAttempt, RunState, SourceHashReview
from imslp_library.storage import hash_file_sha256

from .basic_helpers import ROOT, write_minimal_pdf
from .model_helpers import make_download_target, make_run_snapshot, make_run_state, make_score
from .network_helpers import FakeClock, FakeTransport, bytes_response, stream_response


def pdf_response(path: Path):
    return bytes_response(
        path.read_bytes(),
        "application/pdf",
        final_url="https://s9.imslp.org/files/score.pdf",
    )


def target_for_path(path: Path, file_id: str = "301", include_sha1: bool = True):
    body = path.read_bytes()
    score = make_score(
        file_id=file_id,
        source_id=f"source:f{file_id}@r202",
        expected_size=len(body),
        sha1_imslp=hashlib.sha1(body).hexdigest() if include_sha1 else None,
        source_hash_missing=not include_sha1,
    )
    category = "For 3 guitars (arr)"
    membership = f"membership:{hashlib.sha256(category.encode()).hexdigest()[:10]}:{score.source_id}"
    return make_download_target(category_name=category, membership_id=membership, score=score)


def fixture_response(relative: str):
    body = (ROOT / "tests/fixtures" / relative).read_bytes()
    return bytes_response(body, "text/html", final_url="https://imslp.org/wiki/Special:ImagefromIndex/301")


def valid_pdf(tmp_path: Path) -> Path:
    return write_minimal_pdf(tmp_path / "valid.pdf")


def two_targets():
    first = make_download_target()
    category = "For 4 guitars (arr)"
    score = make_score(file_id="302", source_id="source:f302@r202")
    membership = f"membership:{hashlib.sha256(category.encode()).hexdigest()[:10]}:{score.source_id}"
    second = make_download_target(category_name=category, membership_id=membership, score=score)
    return first, second


def _initialize_run(root: Path, run_id: str, targets: tuple = ()) -> None:
    scores = tuple(sorted((item.score for item in targets), key=lambda item: item.source_id))
    snapshot = make_run_snapshot(run_id=run_id, score_files=scores)
    snapshot_path = root / f"metadata/runs/{run_id}.json"
    state_path = root / f"metadata/runs/{run_id}-state.json"
    atomic_write_json(snapshot_path, snapshot.to_dict())
    digest = hash_file_sha256(snapshot_path)
    state = make_run_state(
        run_id=run_id,
        snapshot_path=f"metadata/runs/{run_id}.json",
        snapshot_sha256=digest,
    )
    atomic_write_json(state_path, state.to_dict())


def _attempts(root: Path, run_id: str) -> tuple[DownloadAttempt, ...]:
    values = read_models(root / f"metadata/runs/{run_id}-download-attempts.json", "DownloadAttemptManifest")
    assert all(isinstance(item, DownloadAttempt) for item in values)
    return values  # type: ignore[return-value]


def test_valid_pdf_stores_one_object_and_finished_attempt(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))

    results = download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())

    assert results[0].result.status is DownloadStatus.DOWNLOADED_VERIFIED
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert (tmp_path / f"objects/{digest[:2]}/{digest}.pdf").read_bytes() == path.read_bytes()
    assert len(_attempts(tmp_path, "r1")) == 1
    assert _attempts(tmp_path, "r1")[0].phase is AttemptPhase.FINISHED
    state = RunState.from_dict(read_json(tmp_path / "metadata/runs/r1-state.json"))
    assert state.download_attempt_manifest_path == "metadata/runs/r1-download-attempts.json"


@pytest.mark.parametrize("mutation", ["size", "sha1", "pdf"])
def test_pdf_mismatch_is_quarantined_for_manual_review(tmp_path, mutation):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    if mutation == "size":
        target = replace(target, score=replace(target.score, expected_size=target.score.expected_size + 1))
    elif mutation == "sha1":
        target = replace(target, score=replace(target.score, sha1_imslp="0" * 40))
    else:
        target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    response = pdf_response(path) if mutation != "pdf" else bytes_response(b"%PDF-broken", "application/pdf", final_url="https://s9.imslp.org/files/score.pdf")

    result = download_batch(tmp_path, "r1", (target,), FakeTransport([response]), FakeClock.fixed())[0].result

    assert result.status is DownloadStatus.MANUAL_REVIEW
    assert result.review_status is ReviewStatus.PENDING
    assert not list((tmp_path / "objects").rglob("*.pdf"))
    quarantined = list((tmp_path / "quarantine/download-invalid/r1").iterdir())
    assert len(quarantined) == 1


def test_missing_imslp_sha1_requires_exact_separate_review(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=False)
    _initialize_run(tmp_path, "r1", (target,))
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())[0].result
    assert result.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert result.review_status is ReviewStatus.PENDING
    assert result.source_hash_missing is True
    assert result.evidence_path
    assert download_completion_blockers(tmp_path, "r1", FakeClock.fixed()) == ("source_hash_review_pending",)

    review = SourceHashReview(
        source_id=target.score.source_id,
        page_revision_id=target.score.page_revision_id,
        object_sha256=result.sha256,
        decision="accept_structural_without_source_hash",
        reviewer_note="Structure and provenance reviewed without claiming a source hash",
        reviewed_at=FakeClock.fixed().now(),
    )
    review_path = tmp_path / f"metadata/overrides/source_hash_reviews/{target.score.source_id}.json"
    atomic_write_json(review_path, review.to_dict())
    resolved = resolve_source_hash_review(tmp_path, "r1", target, result)
    assert resolved.review_status is ReviewStatus.RESOLVED
    assert resolved.source_hash_missing is True
    assert resolved.sha1 is None
    assert resolved.review_evidence_path == review_path.relative_to(tmp_path).as_posix()
    assert download_completion_blockers(tmp_path, "r1", FakeClock.fixed()) == ()


@pytest.mark.parametrize(
    ("fixture", "status"),
    [
        ("http/bot-check.html", DownloadStatus.HUMAN_VERIFICATION_REQUIRED),
        ("http/login.html", DownloadStatus.LOGIN_REQUIRED),
        ("http/membership-wait.html", DownloadStatus.MEMBERSHIP_WAIT_PENDING),
        ("http/membership-required.html", DownloadStatus.MEMBERSHIP_REQUIRED),
        ("http/copyright.html", DownloadStatus.COPYRIGHT_RESTRICTED),
        ("http/region.html", DownloadStatus.REGION_RESTRICTED),
        ("http/commercial.html", DownloadStatus.COMMERCIAL_ONLY),
        ("http/deleted.html", DownloadStatus.DELETED),
    ],
)
def test_html_access_state_saves_exact_evidence(tmp_path, fixture, status):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    response = fixture_response(fixture)
    expected = (ROOT / "tests/fixtures" / fixture).read_bytes()

    result = download_batch(tmp_path, "r1", (target,), FakeTransport([response]), FakeClock.fixed())[0].result

    assert result.status is status
    assert result.evidence_path is not None
    evidence = tmp_path / result.evidence_path
    assert evidence.read_bytes() == expected
    assert _attempts(tmp_path, "r1")[0].evidence_sha256 == hashlib.sha256(expected).hexdigest()
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_bot_check_stops_batch_without_storing_html(tmp_path):
    first, second = two_targets()
    path = valid_pdf(tmp_path)
    first = replace(first, score=replace(first.score, expected_size=path.stat().st_size, sha1_imslp=hashlib.sha1(path.read_bytes()).hexdigest()))
    second = replace(second, score=replace(second.score, expected_size=path.stat().st_size, sha1_imslp=hashlib.sha1(path.read_bytes()).hexdigest()))
    _initialize_run(tmp_path, "r1", (first, second))
    responses = [fixture_response("http/bot-check.html"), pdf_response(path)]
    results = download_batch(tmp_path, "r1", (first, second), FakeTransport(responses), FakeClock.fixed())
    assert results[0].result.status is DownloadStatus.HUMAN_VERIFICATION_REQUIRED
    assert results[1].result.status is DownloadStatus.NOT_STARTED
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_membership_wait_retries_exactly_when_due_and_preserves_attempts(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    start = FakeClock.fixed()
    first = download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/membership-wait.html")]), start)
    assert first[0].result.retry_after == start.now() + timedelta(seconds=60)

    early_clock = FakeClock.fixed()
    early_clock.current += timedelta(seconds=59)
    early_transport = FakeTransport([])
    early = download_batch(tmp_path, "r1", (target,), early_transport, early_clock)
    assert early[0].result.status is DownloadStatus.MEMBERSHIP_WAIT_PENDING
    assert early_transport.calls == []

    due_clock = FakeClock.fixed()
    due_clock.current += timedelta(seconds=60)
    due_transport = FakeTransport([pdf_response(path)])
    final = download_batch(tmp_path, "r1", (target,), due_transport, due_clock)
    assert final[0].result.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert len(_attempts(tmp_path, "r1")) == 2


def test_elapsed_untried_membership_wait_blocks_completion(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/membership-wait.html")]), FakeClock.fixed())
    clock = FakeClock.fixed()
    clock.current += timedelta(seconds=60)
    assert download_completion_blockers(tmp_path, "r1", clock) == ("membership_wait_elapsed_unretried",)


@pytest.mark.parametrize("responses", [[TimeoutError()] * 3, [ConnectionError()] * 3, [bytes_response(b"", "text/plain", status=429)] * 3, [bytes_response(b"", "text/plain", status=503)] * 3])
def test_retryable_transport_uses_exact_attempt_cap(tmp_path, responses):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    transport = FakeTransport(list(responses))
    result = download_batch(tmp_path, "r1", (target,), transport, FakeClock.fixed(), max_attempts=3)[0].result
    assert result.status is DownloadStatus.RETRYABLE
    assert result.detail == "retry_exhausted"
    assert len(transport.calls) == 3
    assert len(_attempts(tmp_path, "r1")) == 3


@pytest.mark.parametrize(("error", "detail"), [(OSError(errno.ENOSPC, "full"), "disk_full"), (OSError(errno.EACCES, "denied"), "permission_denied")])
def test_stream_io_errors_have_exact_details(tmp_path, monkeypatch, error, detail):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    import imslp_library.downloader as module
    monkeypatch.setattr(module, "_open_part", lambda _path: (_ for _ in ()).throw(error))
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())[0].result
    assert result.status is DownloadStatus.RETRYABLE
    assert result.detail == detail


def test_missing_root_and_capacity_failure_are_retryable(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    missing = tmp_path / "missing"
    first = download_batch(missing, "r1", (target,), FakeTransport([]), FakeClock.fixed())[0].result
    assert first.detail == "target_unavailable"
    root = tmp_path / "library"
    root.mkdir()
    _initialize_run(root, "r1", (target,))
    second = download_batch(root, "r1", (target,), FakeTransport([]), FakeClock.fixed(), capacity_guard=lambda *_: False)[0].result
    assert second.detail == "insufficient_capacity"


def test_unapproved_redirect_never_creates_object(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    response = bytes_response(path.read_bytes(), "application/pdf", final_url="https://evil.example/score.pdf")
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([response]), FakeClock.fixed())[0].result
    assert result.status is DownloadStatus.MANUAL_REVIEW
    assert result.detail == "unapproved_redirect"
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_target_metadata_must_match_frozen_source(tmp_path):
    path = valid_pdf(tmp_path)
    canonical = target_for_path(path)
    forged = replace(canonical, score=replace(canonical.score, expected_size=canonical.score.expected_size + 1))
    _initialize_run(tmp_path, "r1", (canonical,))
    transport = FakeTransport([])
    result = download_batch(tmp_path, "r1", (forged,), transport, FakeClock.fixed())[0].result
    assert result.status is DownloadStatus.MANUAL_REVIEW
    assert result.detail == "source_metadata_conflict"
    assert transport.calls == []


def test_exact_legal_source_override_is_verified_and_replay_safe(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    override_path = tmp_path / f"metadata/overrides/download_sources/{target.score.source_id}.json"
    payload = json.loads((ROOT / "tests/fixtures/http/source-override.json").read_text())
    payload |= {
        "source_id": target.score.source_id,
        "page_revision_id": target.score.page_revision_id,
        "expected_size": path.stat().st_size,
        "sha1": hashlib.sha1(path.read_bytes()).hexdigest(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    atomic_write_json(override_path, payload)
    response = bytes_response(path.read_bytes(), "application/pdf", final_url=payload["url"])
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([response]), FakeClock.fixed())[0].result
    assert result.status is DownloadStatus.SOURCE_OVERRIDE_VERIFIED
    assert result.review_status is ReviewStatus.RESOLVED
    assert result.review_evidence_path == override_path.relative_to(tmp_path).as_posix()

    other = tmp_path / "other"
    other.mkdir()
    _initialize_run(other, "r1", (target,))
    changed = dict(payload, page_revision_id=999)
    atomic_write_json(other / f"metadata/overrides/download_sources/{target.score.source_id}.json", changed)
    rejected = download_batch(other, "r1", (target,), FakeTransport([]), FakeClock.fixed())[0].result
    assert rejected.status is DownloadStatus.MANUAL_REVIEW
    assert rejected.detail == "source_override_invalid"


def test_manifest_pointer_recovers_after_creation_crash(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    with pytest.raises(RuntimeError, match="crash"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed(), transition_hook=lambda phase: (_ for _ in ()).throw(RuntimeError("crash")) if phase == "manifest_created" else None)
    assert (tmp_path / "metadata/runs/r1-download-attempts.json").exists()
    state = RunState.from_dict(read_json(tmp_path / "metadata/runs/r1-state.json"))
    assert state.download_attempt_manifest_path is None
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())
    assert result[0].result.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert RunState.from_dict(read_json(tmp_path / "metadata/runs/r1-state.json")).download_attempt_manifest_path == "metadata/runs/r1-download-attempts.json"


def test_midstream_restart_quarantines_part_and_never_uses_range(tmp_path):
    path = valid_pdf(tmp_path)
    body = path.read_bytes()
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    first_transport = FakeTransport([stream_response((body[:32], ConnectionError("mid-stream")), "application/pdf", final_url="https://s9.imslp.org/files/score.pdf")])
    first = download_batch(tmp_path, "r1", (target,), first_transport, FakeClock.fixed(), max_attempts=1)[0].result
    assert first.status is DownloadStatus.RETRYABLE
    attempt = _attempts(tmp_path, "r1")[-1]
    assert attempt.bytes_written == 32
    assert (tmp_path / attempt.part_path).read_bytes() == body[:32]

    second_transport = FakeTransport([pdf_response(path)])
    second = download_batch(tmp_path, "r1", (target,), second_transport, FakeClock.fixed(), max_attempts=1)[0].result
    assert second.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert all(dict(call.headers).get("Range") is None for call in second_transport.calls)
    manifest = read_json(tmp_path / "quarantine/manifests/download-parts-r1.json")
    item = manifest["items"][0]
    quarantined = tmp_path / item["quarantine_path"]
    assert quarantined.read_bytes() == body[:32]
    assert item["size"] == 32
    assert item["sha256"] == hashlib.sha256(body[:32]).hexdigest()
    assert (tmp_path / f"objects/{hashlib.sha256(body).hexdigest()[:2]}/{hashlib.sha256(body).hexdigest()}.pdf").read_bytes() == body


@pytest.mark.parametrize("phase", ["part_intent_written", "part_moved", "part_manifest_updated"])
def test_part_quarantine_transaction_recovers_every_crash_window(tmp_path, phase):
    path = valid_pdf(tmp_path)
    body = path.read_bytes()
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    interrupted = stream_response(
        (body[:32], ConnectionError("mid-stream")),
        "application/pdf",
        final_url="https://s9.imslp.org/files/score.pdf",
    )
    download_batch(tmp_path, "r1", (target,), FakeTransport([interrupted]), FakeClock.fixed(), max_attempts=1)

    def crash(current):
        if current == phase:
            raise RuntimeError(f"crash at {phase}")

    with pytest.raises(RuntimeError, match=phase):
        download_batch(
            tmp_path,
            "r1",
            (target,),
            FakeTransport([]),
            FakeClock.fixed(),
            max_attempts=1,
            transition_hook=crash,
        )
    result = download_batch(
        tmp_path,
        "r1",
        (target,),
        FakeTransport([pdf_response(path)]),
        FakeClock.fixed(),
        max_attempts=1,
    )[0].result
    assert result.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert not list((tmp_path / "quarantine/transactions").glob("download-part-*.json"))
    manifest = read_json(tmp_path / "quarantine/manifests/download-parts-r1.json")
    assert len(manifest["items"]) == 1


@pytest.mark.parametrize("phase", ["attempt_started", "stream_checkpointed"])
def test_unfinished_attempt_checkpoint_is_recovered_before_retry(tmp_path, phase):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))

    def crash(current):
        if current == phase:
            raise RuntimeError(f"crash at {phase}")

    with pytest.raises(RuntimeError, match=phase):
        download_batch(
            tmp_path,
            "r1",
            (target,),
            FakeTransport([pdf_response(path)]),
            FakeClock.fixed(),
            max_attempts=1,
            transition_hook=crash,
        )
    assert _attempts(tmp_path, "r1")[0].phase in {AttemptPhase.STARTED, AttemptPhase.STREAMING}

    result = download_batch(
        tmp_path,
        "r1",
        (target,),
        FakeTransport([pdf_response(path)]),
        FakeClock.fixed(),
        max_attempts=1,
    )[0].result

    assert result.status is DownloadStatus.DOWNLOADED_VERIFIED
    attempts = _attempts(tmp_path, "r1")
    assert attempts[0].phase is AttemptPhase.FINISHED
    assert attempts[0].status is DownloadStatus.RETRYABLE
    assert attempts[0].detail_code == "process_interrupted"
    assert attempts[1].status is DownloadStatus.DOWNLOADED_VERIFIED


@pytest.mark.parametrize("phase", ["object_stored", "success_checkpointed"])
def test_object_to_success_checkpoint_crashes_reconcile_without_stale_parts(tmp_path, phase):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))

    def crash(current):
        if current == phase:
            raise RuntimeError(f"crash at {phase}")

    with pytest.raises(RuntimeError, match=phase):
        download_batch(
            tmp_path,
            "r1",
            (target,),
            FakeTransport([pdf_response(path)]),
            FakeClock.fixed(),
            max_attempts=1,
            transition_hook=crash,
        )
    transport = FakeTransport([pdf_response(path)])
    result = download_batch(tmp_path, "r1", (target,), transport, FakeClock.fixed(), max_attempts=1)
    if phase == "object_stored":
        assert result[0].result.status is DownloadStatus.DOWNLOADED_VERIFIED
        assert len(transport.calls) == 1
    else:
        assert result == ()
        assert transport.calls == []
    assert not list((tmp_path / "objects/.parts/r1").glob("*.part"))


def test_restart_restores_all_persisted_source_associations(tmp_path):
    path = valid_pdf(tmp_path)
    body = path.read_bytes()
    first = target_for_path(path)
    category = "For 4 guitars (arr)"
    membership = f"membership:{hashlib.sha256(category.encode()).hexdigest()[:10]}:{first.score.source_id}"
    second = replace(first, category_name=category, membership_id=membership)
    _initialize_run(tmp_path, "r1", (first,))
    interrupted = stream_response(
        (body[:32], ConnectionError("mid-stream")),
        "application/pdf",
        final_url="https://s9.imslp.org/files/score.pdf",
    )
    download_batch(tmp_path, "r1", (first, second), FakeTransport([interrupted]), FakeClock.fixed(), max_attempts=1)

    result = download_batch(
        tmp_path,
        "r1",
        (first,),
        FakeTransport([pdf_response(path)]),
        FakeClock.fixed(),
        max_attempts=1,
        category_names=(second.category_name,),
    )[0]

    assert len(result.category_names) == 2
    last = _attempts(tmp_path, "r1")[-1]
    assert (last.category_names, last.membership_ids) == (result.category_names, result.membership_ids)


def test_category_source_and_batch_selectors_are_atomic(tmp_path):
    path = valid_pdf(tmp_path)
    first, second = two_targets()
    digest = hashlib.sha1(path.read_bytes()).hexdigest()
    first = replace(first, score=replace(first.score, expected_size=path.stat().st_size, sha1_imslp=digest))
    second = replace(second, score=replace(second.score, expected_size=path.stat().st_size, sha1_imslp=digest))
    _initialize_run(tmp_path, "r1", (first, second))
    transport = FakeTransport([pdf_response(path), pdf_response(path)])
    selected = download_batch(
        tmp_path,
        "r1",
        (second, first),
        transport,
        FakeClock.fixed(),
        category_names=(second.category_name,),
        source_ids=(second.score.source_id,),
        batch_limit=1,
    )
    assert tuple(item.source_id for item in selected) == (second.score.source_id,)
    assert len(transport.calls) == 1
    assert {item.source_id for item in _attempts(tmp_path, "r1")} == {second.score.source_id}


def test_terminal_access_status_is_never_retried(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    first = download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/login.html")]), FakeClock.fixed())
    assert first[0].result.status is DownloadStatus.LOGIN_REQUIRED
    transport = FakeTransport([pdf_response(path)])
    assert download_batch(tmp_path, "r1", (target,), transport, FakeClock.fixed()) == ()
    assert transport.calls == []


def test_batch_limit_is_applied_after_terminal_sources_are_skipped(tmp_path):
    path = valid_pdf(tmp_path)
    first, second = two_targets()
    digest = hashlib.sha1(path.read_bytes()).hexdigest()
    first = replace(first, score=replace(first.score, expected_size=path.stat().st_size, sha1_imslp=digest))
    second = replace(second, score=replace(second.score, expected_size=path.stat().st_size, sha1_imslp=digest))
    _initialize_run(tmp_path, "r1", (first, second))
    download_batch(tmp_path, "r1", (first,), FakeTransport([fixture_response("http/login.html")]), FakeClock.fixed())
    transport = FakeTransport([pdf_response(path)])

    result = download_batch(tmp_path, "r1", (first, second), transport, FakeClock.fixed(), batch_limit=1)

    assert tuple(item.source_id for item in result) == (second.score.source_id,)
    assert result[0].result.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert len(transport.calls) == 1


@pytest.mark.parametrize("mutation", ["source_id", "revision", "sha256", "blank_note", "final_url"])
def test_source_override_mutations_never_become_verified(tmp_path, mutation):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    override_path = tmp_path / f"metadata/overrides/download_sources/{target.score.source_id}.json"
    payload = json.loads((ROOT / "tests/fixtures/http/source-override.json").read_text())
    payload |= {
        "source_id": target.score.source_id,
        "page_revision_id": target.score.page_revision_id,
        "expected_size": path.stat().st_size,
        "sha1": hashlib.sha1(path.read_bytes()).hexdigest(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    if mutation == "source_id":
        payload["source_id"] = "source:f999@r202"
    elif mutation == "revision":
        payload["page_revision_id"] = 999
    elif mutation == "sha256":
        payload["sha256"] = "0" * 64
    elif mutation == "blank_note":
        payload["legal_source_note"] = " "
    atomic_write_json(override_path, payload)
    final_url = "https://different.example.edu/score.pdf" if mutation == "final_url" else payload["url"]
    transport = FakeTransport([bytes_response(path.read_bytes(), "application/pdf", final_url=final_url)])
    result = download_batch(tmp_path, "r1", (target,), transport, FakeClock.fixed())[0].result
    assert result.status is DownloadStatus.MANUAL_REVIEW
    assert not list((tmp_path / "objects").rglob("*.pdf"))
    if mutation in {"source_id", "revision", "blank_note"}:
        assert transport.calls == []


def test_exact_override_resolves_prior_manual_review_before_retry(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    first = download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/not-a-pdf.html")]), FakeClock.fixed())
    assert first[0].result.status is DownloadStatus.MANUAL_REVIEW
    override_path = tmp_path / f"metadata/overrides/download_sources/{target.score.source_id}.json"
    payload = json.loads((ROOT / "tests/fixtures/http/source-override.json").read_text())
    payload |= {
        "source_id": target.score.source_id,
        "page_revision_id": target.score.page_revision_id,
        "expected_size": path.stat().st_size,
        "sha1": hashlib.sha1(path.read_bytes()).hexdigest(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    atomic_write_json(override_path, payload)

    second = download_batch(
        tmp_path,
        "r1",
        (target,),
        FakeTransport([bytes_response(path.read_bytes(), "application/pdf", final_url=payload["url"])]),
        FakeClock.fixed(),
    )

    assert second[0].result.status is DownloadStatus.SOURCE_OVERRIDE_VERIFIED
    attempts = _attempts(tmp_path, "r1")
    assert attempts[0].status is DownloadStatus.MANUAL_REVIEW
    assert attempts[0].review_status is ReviewStatus.RESOLVED
    assert attempts[1].status is DownloadStatus.SOURCE_OVERRIDE_VERIFIED
    transport = FakeTransport([])
    assert download_batch(tmp_path, "r1", (target,), transport, FakeClock.fixed()) == ()
    assert transport.calls == []
    assert download_completion_blockers(tmp_path, "r1", FakeClock.fixed()) == ()


def test_non_success_pdf_response_is_never_accepted(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    response = bytes_response(
        path.read_bytes(),
        "application/pdf",
        status=404,
        final_url="https://s9.imslp.org/files/score.pdf",
    )
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([response]), FakeClock.fixed())[0].result
    assert result.status is DownloadStatus.MANUAL_REVIEW
    assert result.detail == "http_unexpected"
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_invalid_manifest_and_noncanonical_runstate_pointer_fail_closed(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    manifest = tmp_path / "metadata/runs/r1-download-attempts.json"
    atomic_write_json(manifest, {"schema_version": 1, "model_type": "DownloadAttemptManifest", "items": [{"forged": True}]})
    with pytest.raises(DownloadError, match="manifest"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())

    manifest.unlink()
    state_path = tmp_path / "metadata/runs/r1-state.json"
    state = RunState.from_dict(read_json(state_path))
    atomic_write_json(state_path, replace(state, download_attempt_manifest_path="metadata/runs/other.json").to_dict())
    with pytest.raises(DownloadError, match="noncanonical"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())


def test_attempt_part_path_and_evidence_hash_are_replay_guarded(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/not-a-pdf.html")]), FakeClock.fixed())
    attempt = _attempts(tmp_path, "r1")[0]
    _write_path = tmp_path / "metadata/runs/r1-download-attempts.json"
    atomic_write_json(
        _write_path,
        {
            "schema_version": 1,
            "model_type": "DownloadAttemptManifest",
            "items": [replace(attempt, part_path="objects/.parts/other/f301-r202.part").to_dict()],
        },
    )
    with pytest.raises(DownloadError, match="part path"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())

    atomic_write_json(
        _write_path,
        {
            "schema_version": 1,
            "model_type": "DownloadAttemptManifest",
            "items": [attempt.to_dict()],
        },
    )
    (tmp_path / attempt.evidence_path).write_bytes(b"tampered")
    with pytest.raises(DownloadError, match="evidence bytes"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())


def test_known_hash_success_persists_canonical_object_authority(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())[0].result
    attempt = _attempts(tmp_path, "r1")[0]
    assert attempt.evidence_path == f"metadata/runs/r1/evidence/{target.score.source_id}-1-success.json"
    evidence = read_json(tmp_path / attempt.evidence_path)
    assert evidence == {
        "schema_version": 1,
        "model_type": "DownloadSuccessEvidence",
        "run_id": "r1",
        "source_id": target.score.source_id,
        "page_revision_id": target.score.page_revision_id,
        "size": result.size,
        "sha1": result.sha1,
        "sha256": result.sha256,
        "object_path": f"objects/{result.sha256[:2]}/{result.sha256}.pdf",
        "source_hash_missing": False,
        "verified_at": FakeClock.fixed().now().isoformat(),
    }


@pytest.mark.parametrize("kind", ["known", "missing", "override"])
def test_every_success_kind_fails_closed_if_object_authority_is_lost(tmp_path, kind):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=kind != "missing")
    _initialize_run(tmp_path, "r1", (target,))
    if kind == "override":
        override_path = tmp_path / f"metadata/overrides/download_sources/{target.score.source_id}.json"
        payload = json.loads((ROOT / "tests/fixtures/http/source-override.json").read_text())
        payload |= {
            "source_id": target.score.source_id,
            "page_revision_id": target.score.page_revision_id,
            "expected_size": path.stat().st_size,
            "sha1": hashlib.sha1(path.read_bytes()).hexdigest(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        atomic_write_json(override_path, payload)
        response = bytes_response(path.read_bytes(), "application/pdf", final_url=payload["url"])
    else:
        response = pdf_response(path)
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([response]), FakeClock.fixed())[0].result
    object_path = tmp_path / f"objects/{result.sha256[:2]}/{result.sha256}.pdf"
    if kind == "known":
        object_path.unlink()
    elif kind == "missing":
        replacement = object_path.with_suffix(".replacement")
        replacement.write_bytes(b"x" * object_path.stat().st_size)
        object_path.unlink()
        replacement.rename(object_path)
    else:
        original = object_path.with_suffix(".original")
        object_path.rename(original)
        object_path.symlink_to(original)
    with pytest.raises(DownloadError, match="object|success"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())
    with pytest.raises(DownloadError, match="object|success"):
        download_completion_blockers(tmp_path, "r1", FakeClock.fixed())


@pytest.mark.parametrize("include_sha1", [True, False], ids=["known-hash", "missing-hash"])
def test_success_evidence_cannot_rebind_to_a_different_valid_pdf(tmp_path, include_sha1):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=include_sha1)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())
    attempt = _attempts(tmp_path, "r1")[0]

    rebound = tmp_path / "rebound.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=144)
    writer.add_blank_page(width=72, height=72)
    with rebound.open("wb") as output:
        writer.write(output)
    rebound_body = rebound.read_bytes()
    rebound_sha256 = hashlib.sha256(rebound_body).hexdigest()
    rebound_sha1 = hashlib.sha1(rebound_body).hexdigest() if include_sha1 else None
    rebound_object = tmp_path / f"objects/{rebound_sha256[:2]}/{rebound_sha256}.pdf"
    rebound_object.parent.mkdir(parents=True, exist_ok=True)
    rebound_object.write_bytes(rebound_body)

    evidence_path = tmp_path / attempt.evidence_path
    evidence = read_json(evidence_path)
    evidence |= {
        "size": len(rebound_body),
        "sha1": rebound_sha1,
        "sha256": rebound_sha256,
        "object_path": rebound_object.relative_to(tmp_path).as_posix(),
    }
    atomic_write_json(evidence_path, evidence)
    rebound_attempt = replace(
        attempt,
        bytes_written=len(rebound_body),
        evidence_sha256=hash_file_sha256(evidence_path),
    )
    atomic_write_json(
        tmp_path / "metadata/runs/r1-download-attempts.json",
        {
            "schema_version": 1,
            "model_type": "DownloadAttemptManifest",
            "items": [rebound_attempt.to_dict()],
        },
    )

    with pytest.raises(DownloadError, match="success|frozen|identity|digest"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())
    with pytest.raises(DownloadError, match="success|frozen|identity|digest"):
        download_completion_blockers(tmp_path, "r1", FakeClock.fixed())


def test_source_hash_review_rejects_forged_caller_result_and_non_pdf_object(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=False)
    _initialize_run(tmp_path, "r1", (target,))
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())[0].result
    forged_bytes = b"not a pdf"
    forged_hash = hashlib.sha256(forged_bytes).hexdigest()
    forged_object = tmp_path / f"objects/{forged_hash[:2]}/{forged_hash}.pdf"
    forged_object.parent.mkdir(parents=True, exist_ok=True)
    forged_object.write_bytes(forged_bytes)
    review = SourceHashReview(
        source_id=target.score.source_id,
        page_revision_id=target.score.page_revision_id,
        object_sha256=forged_hash,
        decision="accept_structural_without_source_hash",
        reviewer_note="forged review",
        reviewed_at=FakeClock.fixed().now(),
    )
    atomic_write_json(tmp_path / f"metadata/overrides/source_hash_reviews/{target.score.source_id}.json", review.to_dict())
    forged_result = replace(result, size=len(forged_bytes), sha256=forged_hash)
    with pytest.raises(DownloadError, match="exact|replay|object"):
        resolve_source_hash_review(tmp_path, "r1", target, forged_result)


def test_resolved_manual_without_exact_override_evidence_is_invalid_history(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/not-a-pdf.html")]), FakeClock.fixed())
    attempt = _attempts(tmp_path, "r1")[0]
    atomic_write_json(
        tmp_path / "metadata/runs/r1-download-attempts.json",
        {
            "schema_version": 1,
            "model_type": "DownloadAttemptManifest",
            "items": [replace(attempt, review_status=ReviewStatus.RESOLVED, evidence_path=None, evidence_sha256=None).to_dict()],
        },
    )
    with pytest.raises(DownloadError, match="manual|evidence|history"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())


def test_terminal_html_with_cleared_evidence_fields_is_rejected(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/login.html")]), FakeClock.fixed())
    attempt = _attempts(tmp_path, "r1")[0]
    atomic_write_json(
        tmp_path / "metadata/runs/r1-download-attempts.json",
        {
            "schema_version": 1,
            "model_type": "DownloadAttemptManifest",
            "items": [replace(attempt, evidence_path=None, evidence_sha256=None).to_dict()],
        },
    )
    with pytest.raises(DownloadError, match="evidence"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())


def test_status_selector_uses_latest_effective_status_and_combines_filters(tmp_path):
    path = valid_pdf(tmp_path)
    first, second = two_targets()
    digest = hashlib.sha1(path.read_bytes()).hexdigest()
    first = replace(first, score=replace(first.score, expected_size=path.stat().st_size, sha1_imslp=digest))
    second = replace(second, score=replace(second.score, expected_size=path.stat().st_size, sha1_imslp=digest))
    _initialize_run(tmp_path, "r1", (first, second))
    download_batch(tmp_path, "r1", (first,), FakeTransport([fixture_response("http/login.html")]), FakeClock.fixed())
    transport = FakeTransport([pdf_response(path)])
    selected = download_batch(
        tmp_path,
        "r1",
        (first, second),
        transport,
        FakeClock.fixed(),
        statuses=(DownloadStatus.NOT_STARTED,),
        category_names=(second.category_name,),
        source_ids=(second.score.source_id,),
        batch_limit=1,
    )
    assert tuple(item.source_id for item in selected) == (second.score.source_id,)
    assert len(transport.calls) == 1
    terminal_transport = FakeTransport([])
    terminal = download_batch(
        tmp_path,
        "r1",
        (first,),
        terminal_transport,
        FakeClock.fixed(),
        statuses=(DownloadStatus.LOGIN_REQUIRED,),
    )
    assert terminal[0].result.status is DownloadStatus.LOGIN_REQUIRED
    assert terminal_transport.calls == []


def test_unfinished_attempt_maps_to_not_started_status_selector(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))

    def crash(phase):
        if phase == "attempt_started":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed(), transition_hook=crash)
    transport = FakeTransport([pdf_response(path)])
    result = download_batch(
        tmp_path,
        "r1",
        (target,),
        transport,
        FakeClock.fixed(),
        statuses=(DownloadStatus.NOT_STARTED,),
        max_attempts=1,
    )
    assert result[0].result.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "phase",
    ["invalid_intent_written", "invalid_moved", "invalid_attempt_checkpointed", "invalid_before_cleanup"],
)
def test_invalid_pdf_quarantine_wal_recovers_every_crash_window(tmp_path, phase):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=False)
    _initialize_run(tmp_path, "r1", (target,))
    broken_body = b"%PDF-broken" + b"x" * (path.stat().st_size - len(b"%PDF-broken"))
    broken = bytes_response(broken_body, "application/pdf", final_url="https://s9.imslp.org/files/score.pdf")

    def crash(current):
        if current == phase:
            raise RuntimeError(f"crash at {phase}")

    with pytest.raises(RuntimeError, match=phase):
        download_batch(tmp_path, "r1", (target,), FakeTransport([broken]), FakeClock.fixed(), transition_hook=crash)
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())
    assert result[0].result.status is DownloadStatus.MANUAL_REVIEW
    assert result[0].result.detail == "pdf_invalid"
    attempt = _attempts(tmp_path, "r1")[0]
    assert attempt.phase is AttemptPhase.FINISHED
    assert attempt.evidence_path is not None
    assert (tmp_path / attempt.evidence_path).is_file()
    assert not list((tmp_path / "quarantine/transactions").glob("download-invalid-*.json"))


@pytest.mark.parametrize("mutation", ["unknown_bytes", "symlink"])
def test_invalid_quarantine_wal_rejects_unknown_or_symlinked_source(tmp_path, mutation):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=False)
    _initialize_run(tmp_path, "r1", (target,))
    broken_body = b"%PDF-broken" + b"x" * (path.stat().st_size - len(b"%PDF-broken"))
    broken = bytes_response(broken_body, "application/pdf", final_url="https://s9.imslp.org/files/score.pdf")

    def crash(phase):
        if phase == "invalid_intent_written":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        download_batch(tmp_path, "r1", (target,), FakeTransport([broken]), FakeClock.fixed(), transition_hook=crash)
    part = tmp_path / "objects/.parts/r1/f301-r202.part"
    if mutation == "unknown_bytes":
        altered = bytearray(part.read_bytes())
        altered[-1] ^= 1
        part.write_bytes(altered)
    else:
        retained = tmp_path / "retained-invalid.part"
        part.rename(retained)
        part.symlink_to(retained)
    with pytest.raises(DownloadError, match="quarantine|source|unsafe|mismatch"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())


@pytest.mark.parametrize("mutation", ["both_present", "neither_present"])
def test_invalid_quarantine_wal_requires_exactly_one_copy(tmp_path, mutation):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=False)
    _initialize_run(tmp_path, "r1", (target,))
    broken_body = b"%PDF-broken" + b"x" * (path.stat().st_size - len(b"%PDF-broken"))
    broken = bytes_response(broken_body, "application/pdf", final_url="https://s9.imslp.org/files/score.pdf")

    def crash(phase):
        if phase == "invalid_intent_written":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        download_batch(tmp_path, "r1", (target,), FakeTransport([broken]), FakeClock.fixed(), transition_hook=crash)
    intent = next((tmp_path / "quarantine/transactions").glob("download-invalid-*.json"))
    payload = read_json(intent)
    source = tmp_path / payload["source_path"]
    destination = tmp_path / payload["quarantine_path"]
    if mutation == "both_present":
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    else:
        source.unlink()

    with pytest.raises(DownloadError, match="exactly one|quarantine"):
        download_batch(tmp_path, "r1", (target,), FakeTransport([]), FakeClock.fixed())
    assert intent.is_file()
    if mutation == "both_present":
        assert source.is_file()
        assert destination.is_file()


def test_pending_source_hash_review_is_visible_to_status_selector(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=False)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())
    transport = FakeTransport([])
    result = download_batch(
        tmp_path,
        "r1",
        (target,),
        transport,
        FakeClock.fixed(),
        statuses=(DownloadStatus.DOWNLOADED_VERIFIED,),
    )
    assert result[0].result.status is DownloadStatus.DOWNLOADED_VERIFIED
    assert result[0].result.review_status is ReviewStatus.PENDING
    assert transport.calls == []


def test_post_terminal_metadata_conflict_wins_without_illegal_new_attempt(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())
    category = "For 4 guitars (arr)"
    membership = f"membership:{hashlib.sha256(category.encode()).hexdigest()[:10]}:{target.score.source_id}"
    conflicting = replace(
        target,
        category_name=category,
        membership_id=membership,
        score=replace(target.score, expected_size=target.score.expected_size + 1),
    )
    transport = FakeTransport([])
    result = download_batch(tmp_path, "r1", (target, conflicting), transport, FakeClock.fixed())
    assert result[0].result.status is DownloadStatus.MANUAL_REVIEW
    assert result[0].result.detail == "source_metadata_conflict"
    assert len(result[0].category_names) == 2
    assert transport.calls == []
    assert len(_attempts(tmp_path, "r1")) == 1


def test_membership_wait_history_cannot_retry_before_recorded_deadline(tmp_path):
    path = valid_pdf(tmp_path)
    target = target_for_path(path)
    _initialize_run(tmp_path, "r1", (target,))
    download_batch(tmp_path, "r1", (target,), FakeTransport([fixture_response("http/membership-wait.html")]), FakeClock.fixed())
    due = FakeClock.fixed()
    due.current += timedelta(seconds=60)
    download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), due)
    attempts = list(_attempts(tmp_path, "r1"))
    attempts[1] = replace(attempts[1], started_at=attempts[0].retry_after - timedelta(seconds=1), completed_at=attempts[0].retry_after - timedelta(seconds=1))
    atomic_write_json(
        tmp_path / "metadata/runs/r1-download-attempts.json",
        {"schema_version": 1, "model_type": "DownloadAttemptManifest", "items": [item.to_dict() for item in attempts]},
    )
    with pytest.raises(DownloadError, match="retry_after|wait|history"):
        download_completion_blockers(tmp_path, "r1", FakeClock.fixed())


def test_same_source_is_downloaded_once_with_all_associations(tmp_path):
    path = valid_pdf(tmp_path)
    first = target_for_path(path)
    category = "For 4 guitars (arr)"
    membership = f"membership:{hashlib.sha256(category.encode()).hexdigest()[:10]}:{first.score.source_id}"
    second = replace(first, category_name=category, membership_id=membership)
    _initialize_run(tmp_path, "r1", (first,))
    transport = FakeTransport([pdf_response(path)])
    results = download_batch(tmp_path, "r1", (second, first), transport, FakeClock.fixed())
    assert len(results) == 1
    assert len(transport.calls) == 1
    pairs = tuple(zip(results[0].category_names, results[0].membership_ids, strict=True))
    assert pairs == tuple(sorted(((first.category_name, first.membership_id), (second.category_name, second.membership_id))))
    attempt = _attempts(tmp_path, "r1")[0]
    assert tuple(zip(attempt.category_names, attempt.membership_ids, strict=True)) == pairs


def test_group_metadata_conflict_makes_no_request_and_keeps_associations(tmp_path):
    path = valid_pdf(tmp_path)
    first = target_for_path(path)
    category = "For 4 guitars (arr)"
    membership = f"membership:{hashlib.sha256(category.encode()).hexdigest()[:10]}:{first.score.source_id}"
    second = replace(first, category_name=category, membership_id=membership, score=replace(first.score, expected_size=first.score.expected_size + 1))
    _initialize_run(tmp_path, "r1", (first,))
    transport = FakeTransport([])
    result = download_batch(tmp_path, "r1", (first, second), transport, FakeClock.fixed())[0]
    assert result.result.detail == "source_metadata_conflict"
    assert result.result.status is DownloadStatus.MANUAL_REVIEW
    assert len(result.category_names) == 2
    assert transport.calls == []


@pytest.mark.parametrize("field", ["source_id", "page_revision_id", "object_sha256", "reviewer_note", "decision"])
def test_source_hash_review_replay_or_rejection_never_resolves(tmp_path, field):
    path = valid_pdf(tmp_path)
    target = target_for_path(path, include_sha1=False)
    _initialize_run(tmp_path, "r1", (target,))
    result = download_batch(tmp_path, "r1", (target,), FakeTransport([pdf_response(path)]), FakeClock.fixed())[0].result
    review = SourceHashReview(
        source_id=target.score.source_id,
        page_revision_id=target.score.page_revision_id,
        object_sha256=result.sha256,
        decision="accept_structural_without_source_hash",
        reviewer_note="reviewed without source hash",
        reviewed_at=FakeClock.fixed().now(),
    ).to_dict()
    if field == "source_id":
        review["source_id"] = "source:f999@r202"
    elif field == "page_revision_id":
        review["page_revision_id"] = 999
    elif field == "object_sha256":
        review["object_sha256"] = "0" * 64
    elif field == "reviewer_note":
        review["reviewer_note"] = " "
    else:
        review["decision"] = "reject"
    atomic_write_json(tmp_path / f"metadata/overrides/source_hash_reviews/{target.score.source_id}.json", review)
    with pytest.raises((DownloadError, ValueError)):
        resolve_source_hash_review(tmp_path, "r1", target, result)
