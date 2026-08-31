from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import imslp_library.materialize as materialize
from imslp_library.enums import StorageMethod
from imslp_library.jsonio import atomic_write_models, read_models
from imslp_library.materialize import (
    MaterializationConflictError,
    StorageCapabilityError,
    materialize_membership,
    probe_storage_capability,
)
from imslp_library.storage import hash_file_sha256, store_verified_pdf
from tests.basic_helpers import write_minimal_pdf
from tests.model_helpers import make_membership, make_score


def score_for_path(path, file_id):
    return make_score(
        file_id=file_id,
        expected_size=path.stat().st_size,
        sha1_imslp=None,
        source_hash_missing=True,
    )


def test_capability_probe_prefers_hardlink_and_cleans_exact_probe(tmp_path):
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    assert probe_storage_capability(tmp_path) is StorageMethod.HARDLINK
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".imslp-link-probe-*"))


def test_capability_probe_falls_back_to_relative_symlink(tmp_path, monkeypatch):
    def no_link(*_args, **_kwargs):
        raise OSError("unsupported")

    monkeypatch.setattr(materialize.os, "link", no_link)
    assert probe_storage_capability(tmp_path) is StorageMethod.RELATIVE_SYMLINK


def test_capability_failure_leaves_no_category_path(tmp_path, monkeypatch):
    monkeypatch.setattr(materialize.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(materialize.os, "symlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(StorageCapabilityError):
        materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), make_stored_stub(tmp_path))
    assert not (tmp_path / "For guitar").exists()
    assert not list(tmp_path.glob(".imslp-link-probe-*"))


def test_capabilities_can_disappear_after_probe_without_leaving_category_path(tmp_path, monkeypatch):
    stored = make_stored_stub(tmp_path)
    monkeypatch.setattr(materialize, "probe_storage_capability", lambda _root: StorageMethod.HARDLINK)
    monkeypatch.setattr(materialize.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("link gone")))
    monkeypatch.setattr(materialize.os, "symlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("symlink gone")))
    with pytest.raises(StorageCapabilityError, match="materialize"):
        materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
    assert not (tmp_path / "For guitar").exists()


def test_fsync_failure_removes_uncommitted_target_and_new_empty_parents(tmp_path, monkeypatch):
    stored = make_stored_stub(tmp_path)
    monkeypatch.setattr(materialize, "probe_storage_capability", lambda _root: StorageMethod.HARDLINK)
    monkeypatch.setattr(materialize, "_fsync_directory", lambda _path: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises(StorageCapabilityError, match="materialize"):
        materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
    assert not (tmp_path / "For guitar").exists()


def test_invalid_hardlink_result_is_not_committed(tmp_path, monkeypatch):
    stored = make_stored_stub(tmp_path)
    monkeypatch.setattr(materialize, "probe_storage_capability", lambda _root: StorageMethod.HARDLINK)

    def copy_instead_of_link(source, target):
        target.write_bytes(source.read_bytes())

    monkeypatch.setattr(materialize.os, "link", copy_instead_of_link)
    with pytest.raises(StorageCapabilityError, match="materialize"):
        materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
    assert not (tmp_path / "For guitar").exists()


def make_stored_stub(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    return store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")


def test_one_object_can_have_two_category_paths(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    stored = store_verified_pdf(tmp_path, source, score_for_path(source, "301"), run_id="r1")
    first = materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
    second = materialize_membership(tmp_path, make_membership("For guitar (arr)", "two.pdf"), stored)
    assert len(list((tmp_path / "objects").rglob("*.pdf"))) == 1
    assert hash_file_sha256(tmp_path / first.local_path) == stored.sha256
    assert hash_file_sha256(tmp_path / second.local_path) == stored.sha256
    assert {first.storage_method.value, second.storage_method.value} <= {"hardlink", "relative_symlink"}


def test_materialization_prefers_hardlink_when_supported(tmp_path):
    stored = make_stored_stub(tmp_path)
    result = materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
    target = tmp_path / result.local_path
    object_path = tmp_path / stored.object_path
    assert result.storage_method is StorageMethod.HARDLINK
    assert target.stat().st_dev == object_path.stat().st_dev
    assert target.stat().st_ino == object_path.stat().st_ino


def test_relative_symlink_fallback_is_relative_and_root_contained(tmp_path, monkeypatch):
    stored = make_stored_stub(tmp_path)
    monkeypatch.setattr(materialize.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no hardlink")))
    result = materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
    target = tmp_path / result.local_path
    assert result.storage_method is StorageMethod.RELATIVE_SYMLINK
    assert target.is_symlink()
    link_text = os.readlink(target)
    assert not os.path.isabs(link_text)
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert target.resolve() == (tmp_path / stored.object_path).resolve()


def test_unsafe_planned_path_is_rejected(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership(planned_local_path="../outside.pdf")
    with pytest.raises(ValueError, match="planned_local_path"):
        materialize_membership(tmp_path, membership, stored)
    assert not (tmp_path.parent / "outside.pdf").exists()


def test_category_symlink_is_rejected_before_external_write(tmp_path):
    stored = make_stored_stub(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-category"
    outside.mkdir()
    (tmp_path / "For guitar").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink ancestor"):
        materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
    assert list(outside.iterdir()) == []


def test_symlink_library_root_is_rejected_before_materialization(tmp_path):
    real_root = tmp_path / "real-library"
    real_root.mkdir()
    stored = make_stored_stub(real_root)
    root = tmp_path / "library"
    root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="library root is a symlink"):
        materialize_membership(root, make_membership("For guitar", "one.pdf"), stored)
    assert not (real_root / "For guitar").exists()


def test_preexisting_same_object_is_idempotent(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    first = materialize_membership(tmp_path, membership, stored)
    stat_before = (tmp_path / first.local_path).lstat()
    second = materialize_membership(tmp_path, membership, stored)
    assert second == first
    assert (tmp_path / second.local_path).lstat().st_ino == stat_before.st_ino


def test_preexisting_different_path_is_quarantined_and_blocks_success(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    occupied = tmp_path / membership.planned_local_path
    occupied.parent.mkdir(parents=True)
    old = b"occupied"
    occupied.write_bytes(old)
    with pytest.raises(MaterializationConflictError, match="quarantined"):
        materialize_membership(tmp_path, membership, stored, run_id="r1")
    assert not occupied.exists()
    manifest = json.loads((tmp_path / "quarantine/manifests/paths-r1.json").read_text(encoding="utf-8"))
    assert manifest["model_type"] == "PathQuarantineManifest"
    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert item["original_path"] == membership.planned_local_path
    assert item["quarantine_path"].startswith("quarantine/paths/r1/")
    assert (tmp_path / item["quarantine_path"]).read_bytes() == old
    assert item["size"] == len(old)
    assert item["sha256"] == hash_file_sha256(tmp_path / item["quarantine_path"])
    assert item["reason"] == "occupied_path_content_mismatch"


def test_occupied_conflict_without_run_id_fails_closed(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    occupied = tmp_path / membership.planned_local_path
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"occupied")
    with pytest.raises(MaterializationConflictError, match="run_id is required"):
        materialize_membership(tmp_path, membership, stored)
    assert occupied.read_bytes() == b"occupied"
    assert not (tmp_path / "quarantine").exists()


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "..", 123])
def test_occupied_conflict_rejects_unsafe_run_id_without_move(tmp_path, run_id):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    occupied = tmp_path / membership.planned_local_path
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"occupied")
    with pytest.raises(ValueError, match="run_id"):
        materialize_membership(tmp_path, membership, stored, run_id=run_id)
    assert occupied.read_bytes() == b"occupied"


def test_invalid_path_manifest_blocks_quarantine_before_move(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    occupied = tmp_path / membership.planned_local_path
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"occupied")
    manifest = tmp_path / "quarantine/manifests/paths-r1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_type": "PathQuarantineManifest",
                "items": [{"invalid": True}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="PathQuarantineManifest item"):
        materialize_membership(tmp_path, membership, stored, run_id="r1")
    assert occupied.read_bytes() == b"occupied"


def test_quarantine_symlink_blocks_conflict_before_move(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    occupied = tmp_path / membership.planned_local_path
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"occupied")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-path-quarantine"
    outside.mkdir()
    (tmp_path / "quarantine").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink ancestor"):
        materialize_membership(tmp_path, membership, stored, run_id="r1")
    assert occupied.read_bytes() == b"occupied"
    assert list(outside.iterdir()) == []


def test_materialization_rejects_noncanonical_or_invalid_stored_objects(tmp_path):
    stored = make_stored_stub(tmp_path)
    object_path = tmp_path / stored.object_path
    rogue = tmp_path / "rogue.pdf"
    rogue.write_bytes(object_path.read_bytes())
    with pytest.raises(ValueError, match="canonical"):
        materialize_membership(tmp_path, make_membership(), replace(stored, object_path="rogue.pdf"))
    with pytest.raises(ValueError, match="size"):
        materialize_membership(tmp_path, make_membership(), replace(stored, size=stored.size + 1))

    object_path.unlink()
    bad = b"not a pdf"
    bad_hash = hashlib.sha256(bad).hexdigest()
    bad_path = tmp_path / f"objects/{bad_hash[:2]}/{bad_hash}.pdf"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_bytes(bad)
    invalid = replace(stored, sha256=bad_hash, size=len(bad), object_path=bad_path.relative_to(tmp_path).as_posix())
    with pytest.raises(ValueError, match="PDF"):
        materialize_membership(tmp_path, make_membership(), invalid)


def test_materialization_rejects_symlink_at_canonical_object_address(tmp_path):
    stored = make_stored_stub(tmp_path)
    object_path = tmp_path / stored.object_path
    outside = tmp_path.parent / f"{tmp_path.name}-stored.pdf"
    object_path.replace(outside)
    object_path.symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        materialize_membership(tmp_path, make_membership(), stored)


def test_materialization_rejects_symlink_ancestor_of_canonical_object(tmp_path):
    stored = make_stored_stub(tmp_path)
    object_path = tmp_path / stored.object_path
    prefix = object_path.parent
    backing = tmp_path / "object-prefix-backing"
    prefix.replace(backing)
    prefix.symlink_to(backing, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink ancestor"):
        materialize_membership(tmp_path, make_membership(), stored)


def test_concurrent_same_membership_returns_one_consistent_materialization(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: materialize_membership(tmp_path, membership, stored, run_id="r1"), range(2)))
    assert results[0] == results[1]
    target = tmp_path / results[0].local_path
    if results[0].storage_method is StorageMethod.HARDLINK:
        assert target.stat().st_ino == (tmp_path / stored.object_path).stat().st_ino
    else:
        assert target.is_symlink()


def test_materialization_lock_symlink_is_rejected_without_external_modification(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    identity = (tmp_path / membership.planned_local_path).relative_to(tmp_path).as_posix()
    lock_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    lock = tmp_path / f"metadata/operations/locks/materialize-{lock_hash}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-materialize-lock"
    outside.write_bytes(b"keep")
    lock.symlink_to(outside)
    with pytest.raises(ValueError, match="lock"):
        materialize_membership(tmp_path, membership, stored, run_id="r1")
    assert outside.read_bytes() == b"keep"
    assert not (tmp_path / membership.planned_local_path).exists()


def test_path_quarantine_manifest_failure_recovers_from_intent(tmp_path, monkeypatch):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    occupied = tmp_path / membership.planned_local_path
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"occupied")
    original_append = materialize._append_path_manifest
    monkeypatch.setattr(materialize, "_append_path_manifest", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("manifest fsync failed")))
    with pytest.raises(OSError, match="manifest fsync failed"):
        materialize_membership(tmp_path, membership, stored, run_id="r1")
    intents = list((tmp_path / "quarantine/transactions").glob("path-*.json"))
    assert len(intents) == 1
    assert not occupied.exists()
    monkeypatch.setattr(materialize, "_append_path_manifest", original_append)
    with pytest.raises(MaterializationConflictError, match="quarantined"):
        materialize_membership(tmp_path, membership, stored, run_id="r1")
    assert not list((tmp_path / "quarantine/transactions").glob("path-*.json"))
    manifest = json.loads((tmp_path / "quarantine/manifests/paths-r1.json").read_text())
    assert len(manifest["items"]) == 1


def test_materialization_method_survives_manifest_round_trip(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    stored = store_verified_pdf(tmp_path, source, score_for_path(source, "301"), run_id="r1")
    pending = make_membership("For guitar", "one.pdf")
    result = materialize_membership(tmp_path, pending, stored)
    committed = replace(pending, local_path=result.local_path, storage_method=result.storage_method)
    manifest = tmp_path / "metadata/memberships.json"
    atomic_write_models(manifest, "MembershipManifest", [committed])
    assert read_models(manifest, "MembershipManifest") == (committed,)
