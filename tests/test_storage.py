from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import imslp_library.storage as storage
from tests.basic_helpers import write_minimal_pdf
from tests.model_helpers import make_score


def score_for_path(path: Path, file_id: str):
    return make_score(
        file_id=file_id,
        expected_size=path.stat().st_size,
        sha1_imslp=None,
        source_hash_missing=True,
    )


def test_hash_and_object_address_are_deterministic(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    digest = storage.hash_file_sha256(source)
    assert len(digest) == 64
    assert storage.object_path_for_hash(tmp_path, digest) == tmp_path / f"objects/{digest[:2]}/{digest}.pdf"
    with pytest.raises(ValueError, match="SHA-256"):
        storage.object_path_for_hash(tmp_path, "bad")


def test_distinct_valid_pdfs_create_distinct_objects(tmp_path):
    one = write_minimal_pdf(tmp_path / "one.pdf", width=72)
    two = write_minimal_pdf(tmp_path / "two.pdf", width=73)
    first = storage.store_verified_pdf(tmp_path, one, score_for_path(one, "301"), "r1")
    second = storage.store_verified_pdf(tmp_path, two, score_for_path(two, "302"), "r1")
    assert first.sha256 != second.sha256
    assert len(list((tmp_path / "objects").rglob("*.pdf"))) == 2


def test_identical_bytes_reuse_one_immutable_object(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(source.read_bytes())
    first = storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    stat_before = (tmp_path / first.object_path).stat()
    second = storage.store_verified_pdf(tmp_path, copy, score_for_path(copy, "302"), "r1")
    assert first.sha256 == second.sha256
    assert (tmp_path / second.object_path).stat().st_ino == stat_before.st_ino
    assert len(list((tmp_path / "objects").rglob("*.pdf"))) == 1


def test_interruption_leaves_part_but_no_object(tmp_path, monkeypatch):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    original_copy = storage._copy_to_part

    def fail_after_first_chunk(source_path, part_path):
        with source_path.open("rb") as src, part_path.open("wb") as dst:
            dst.write(src.read(32))
            dst.flush()
            os.fsync(dst.fileno())
        raise OSError("injected after part creation")

    monkeypatch.setattr(storage, "_copy_to_part", fail_after_first_chunk)
    with pytest.raises(OSError, match="injected after part creation"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    parts = list((tmp_path / "objects").rglob("*.part"))
    assert len(parts) == 1
    assert not list((tmp_path / "objects").rglob("*.pdf"))
    evidence_path = tmp_path / "metadata/runs/r1-object-writes.json"
    interrupted = json.loads(evidence_path.read_text(encoding="utf-8"))["items"][-1]
    assert interrupted["status"] == "object_write_interrupted"
    assert interrupted["bytes_written"] == 32
    assert interrupted["part_path"] == parts[0].relative_to(tmp_path).as_posix()

    monkeypatch.setattr(storage, "_copy_to_part", original_copy)
    stored = storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert (tmp_path / stored.object_path).is_file()
    assert not list((tmp_path / "objects").rglob("*.part"))
    attempts = json.loads(evidence_path.read_text(encoding="utf-8"))["items"]
    assert [item["status"] for item in attempts] == [
        "object_write_interrupted",
        "object_write_complete",
    ]
    assert attempts[-1]["bytes_written"] == source.stat().st_size
    assert attempts[-1]["part_path"] == interrupted["part_path"]


def test_same_hash_different_sources_use_unique_part_names(tmp_path, monkeypatch):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    seen: list[Path] = []
    original_copy = storage._copy_to_part

    def always_fail(_source, part):
        part.write_bytes(b"x")
        seen.append(part)
        raise OSError("stop")

    monkeypatch.setattr(storage, "_copy_to_part", always_fail)
    for file_id in ("301", "302"):
        with pytest.raises(OSError, match="stop"):
            storage.store_verified_pdf(tmp_path, source, score_for_path(source, file_id), "r1")
    assert len(set(seen)) == 2
    assert all(path.parent == storage.object_path_for_hash(tmp_path, storage.hash_file_sha256(source)).parent for path in seen)
    monkeypatch.setattr(storage, "_copy_to_part", original_copy)
    storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert not seen[0].exists()
    assert seen[1].read_bytes() == b"x"


def test_corrupt_expected_object_is_quarantined_and_replaced(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    digest = storage.hash_file_sha256(source)
    target = storage.object_path_for_hash(tmp_path, digest)
    target.parent.mkdir(parents=True)
    bad = b"corrupt existing bytes"
    target.write_bytes(bad)
    stored = storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert storage.hash_file_sha256(tmp_path / stored.object_path) == digest
    manifest_path = tmp_path / "quarantine/manifests/objects-r1.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["model_type"] == "ObjectQuarantineManifest"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    quarantined = tmp_path / item["quarantine_path"]
    assert quarantined.read_bytes() == bad
    assert item["original_path"] == target.relative_to(tmp_path).as_posix()
    assert item["size"] == len(bad)
    assert item["sha256"] == storage.hash_file_sha256(quarantined)
    assert item["reason"] == "object_hash_mismatch"
    storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert len(json.loads(manifest_path.read_text(encoding="utf-8"))["items"]) == 1


def test_rejects_invalid_pdf_without_active_object(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"not pdf")
    with pytest.raises(ValueError, match="PDF"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_unsafe_run_id_is_rejected_before_object_write(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    with pytest.raises(ValueError, match="run_id"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "../escape")
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_objects_symlink_cannot_escape_library_root(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "objects").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink ancestor"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert list(outside.iterdir()) == []


def test_object_address_symlink_cannot_alias_outside_file(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    digest = storage.hash_file_sha256(source)
    target = storage.object_path_for_hash(tmp_path, digest)
    target.parent.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.pdf"
    outside.write_bytes(source.read_bytes())
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert outside.read_bytes() == source.read_bytes()


def test_invalid_attempt_manifest_is_rejected_before_object_write(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    evidence = tmp_path / "metadata/runs/r1-object-writes.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_type": "ObjectWriteAttemptManifest",
                "items": [
                    {
                        "source_id": "source:f301@r202",
                        "part_path": "objects/aa/x.part",
                        "bytes_written": 1,
                        "status": "invented",
                        "error": None,
                        "attempted_at": "not-aware",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ObjectWriteAttemptManifest item"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_attempt_manifest_symlink_is_rejected_before_object_write(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-runs"
    outside.mkdir()
    (tmp_path / "metadata").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink ancestor"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert list(outside.iterdir()) == []
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_symlink_library_root_is_rejected_before_external_write(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-storage-root"
    outside.mkdir()
    root = tmp_path / "library"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="library root is a symlink"):
        storage.store_verified_pdf(root, source, score_for_path(source, "301"), "r1")
    assert list(outside.iterdir()) == []


def test_quarantine_symlink_is_rejected_before_corrupt_object_move(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    digest = storage.hash_file_sha256(source)
    target = storage.object_path_for_hash(tmp_path, digest)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-quarantine"
    outside.mkdir()
    (tmp_path / "quarantine").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink ancestor"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert target.read_bytes() == b"corrupt"
    assert list(outside.iterdir()) == []


def test_existing_object_reconciles_one_complete_attempt_after_record_failure(tmp_path, monkeypatch):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")
    original_copy = storage._copy_to_part

    def interrupt(source_path, part_path):
        part_path.write_bytes(source_path.read_bytes()[:32])
        raise OSError("first interruption")

    monkeypatch.setattr(storage, "_copy_to_part", interrupt)
    with pytest.raises(OSError, match="first interruption"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    monkeypatch.setattr(storage, "_copy_to_part", original_copy)
    original_record = storage._record_attempt

    def fail_complete(*args, **kwargs):
        status = args[4]
        if status == "object_write_complete":
            raise OSError("complete record fsync failed")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(storage, "_record_attempt", fail_complete)
    with pytest.raises(OSError, match="complete record fsync failed"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    monkeypatch.setattr(storage, "_record_attempt", original_record)
    evidence = tmp_path / "metadata/runs/r1-object-writes.json"
    assert [item["status"] for item in json.loads(evidence.read_text())["items"]] == ["object_write_interrupted"]
    stored = storage.store_verified_pdf(tmp_path, source, score, "r1")
    assert (tmp_path / stored.object_path).is_file()
    storage.store_verified_pdf(tmp_path, source, score, "r1")
    attempts = json.loads(evidence.read_text(encoding="utf-8"))["items"]
    assert [item["status"] for item in attempts] == ["object_write_interrupted", "object_write_complete"]
    assert attempts[-1]["bytes_written"] == source.stat().st_size
    assert not list((tmp_path / "objects").rglob("*.part"))


def test_unowned_regular_part_is_rejected_without_modification(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")
    digest = storage.hash_file_sha256(source)
    target = storage.object_path_for_hash(tmp_path, digest)
    part = storage._part_path(target, score, "r1")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"unowned")
    before = part.read_bytes()
    with pytest.raises(ValueError, match="unowned part"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    assert part.read_bytes() == before
    assert not target.exists()


def test_part_symlink_is_rejected_without_touching_external_file(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")
    target = storage.object_path_for_hash(tmp_path, storage.hash_file_sha256(source))
    part = storage._part_path(target, score, "r1")
    part.parent.mkdir(parents=True)
    outside = tmp_path / "outside-part"
    outside.write_bytes(b"keep")
    part.symlink_to(outside)
    with pytest.raises(ValueError, match="part"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    assert outside.read_bytes() == b"keep"
    assert part.is_symlink()


def test_interrupted_part_size_must_match_ownership_record(tmp_path, monkeypatch):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")
    original_copy = storage._copy_to_part

    def interrupt(source_path, part_path):
        part_path.write_bytes(source_path.read_bytes()[:32])
        raise OSError("interrupted")

    monkeypatch.setattr(storage, "_copy_to_part", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    part = next((tmp_path / "objects").rglob("*.part"))
    part.write_bytes(b"changed")
    monkeypatch.setattr(storage, "_copy_to_part", original_copy)
    with pytest.raises(ValueError, match="unowned part"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    assert part.read_bytes() == b"changed"


def test_copy_boundary_cannot_commit_multiply_linked_part(tmp_path, monkeypatch):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")

    def hardlink_source(source_path, part_path):
        os.link(source_path, part_path)

    monkeypatch.setattr(storage, "_copy_to_part", hardlink_source)
    with pytest.raises(ValueError, match="link count"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_concurrent_same_source_has_one_complete_and_no_interrupted(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: storage.store_verified_pdf(tmp_path, source, score, "r1"), range(2)))
    assert results[0].sha256 == results[1].sha256
    attempts = json.loads((tmp_path / "metadata/runs/r1-object-writes.json").read_text())["items"]
    matching = [item for item in attempts if item["source_id"] == score.source_id]
    assert [item["status"] for item in matching] == ["object_write_complete"]


def test_concurrent_distinct_sources_same_bytes_keep_both_completions(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    scores = [score_for_path(source, "301"), score_for_path(source, "302")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda score: storage.store_verified_pdf(tmp_path, source, score, "r1"), scores))
    assert results[0].sha256 == results[1].sha256
    attempts = json.loads((tmp_path / "metadata/runs/r1-object-writes.json").read_text())["items"]
    assert {(item["source_id"], item["status"]) for item in attempts} == {
        (scores[0].source_id, "object_write_complete"),
        (scores[1].source_id, "object_write_complete"),
    }


def test_old_same_source_completion_for_other_hash_does_not_suppress_new_completion(tmp_path):
    first = write_minimal_pdf(tmp_path / "first.pdf", width=72)
    second = write_minimal_pdf(tmp_path / "second.pdf", width=73)
    assert first.stat().st_size == second.stat().st_size
    score = score_for_path(first, "301")
    one = storage.store_verified_pdf(tmp_path, first, score, "r1")
    two = storage.store_verified_pdf(tmp_path, second, score, "r1")
    assert one.sha256 != two.sha256
    attempts = json.loads((tmp_path / "metadata/runs/r1-object-writes.json").read_text())["items"]
    matching = [item for item in attempts if item["source_id"] == score.source_id and item["status"] == "object_write_complete"]
    assert len(matching) == 2
    assert len({item["part_path"] for item in matching}) == 2


def test_prebuilt_object_in_other_run_gets_current_run_completion(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")
    storage.store_verified_pdf(tmp_path, source, score, "r0")
    storage.store_verified_pdf(tmp_path, source, score, "r1")
    attempts = json.loads((tmp_path / "metadata/runs/r1-object-writes.json").read_text())["items"]
    assert [item["status"] for item in attempts] == ["object_write_complete"]


def test_object_lock_symlink_is_rejected_without_external_modification(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    digest = storage.hash_file_sha256(source)
    lock = tmp_path / f"objects/.locks/{digest}.lock"
    lock.parent.mkdir(parents=True)
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"keep")
    lock.symlink_to(outside)
    with pytest.raises(ValueError, match="lock"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert outside.read_bytes() == b"keep"
    assert not storage.object_path_for_hash(tmp_path, digest).exists()


def test_object_quarantine_manifest_failure_recovers_from_intent(tmp_path, monkeypatch):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    score = score_for_path(source, "301")
    target = storage.object_path_for_hash(tmp_path, storage.hash_file_sha256(source))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    original_append = storage._append_manifest

    def fail_final(path, model_type, item, item_keys, validator):
        if model_type == "ObjectQuarantineManifest":
            raise OSError("manifest fsync failed")
        return original_append(path, model_type, item, item_keys, validator)

    monkeypatch.setattr(storage, "_append_manifest", fail_final)
    with pytest.raises(OSError, match="manifest fsync failed"):
        storage.store_verified_pdf(tmp_path, source, score, "r1")
    intents = list((tmp_path / "quarantine/transactions").glob("*.json"))
    assert len(intents) == 1
    assert not target.exists()
    monkeypatch.setattr(storage, "_append_manifest", original_append)
    stored = storage.store_verified_pdf(tmp_path, source, score, "r1")
    assert (tmp_path / stored.object_path).is_file()
    assert not list((tmp_path / "quarantine/transactions").glob("*.json"))
    manifest = json.loads((tmp_path / "quarantine/manifests/objects-r1.json").read_text())
    assert len(manifest["items"]) == 1
