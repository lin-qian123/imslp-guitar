from __future__ import annotations

import json
import os
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
    with pytest.raises(ValueError, match="inside library root"):
        storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
    assert not list(outside.rglob("*.pdf"))


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
