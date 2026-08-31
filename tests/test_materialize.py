from __future__ import annotations

import json
import os
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
        materialize_membership(tmp_path, membership, stored)
    assert not occupied.exists()
    manifest = json.loads((tmp_path / "quarantine/manifests/paths-materialize.json").read_text(encoding="utf-8"))
    assert manifest["model_type"] == "PathQuarantineManifest"
    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert item["original_path"] == membership.planned_local_path
    assert (tmp_path / item["quarantine_path"]).read_bytes() == old
    assert item["size"] == len(old)
    assert item["sha256"] == hash_file_sha256(tmp_path / item["quarantine_path"])
    assert item["reason"] == "occupied_path_content_mismatch"


def test_invalid_path_manifest_blocks_quarantine_before_move(tmp_path):
    stored = make_stored_stub(tmp_path)
    membership = make_membership("For guitar", "one.pdf")
    occupied = tmp_path / membership.planned_local_path
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"occupied")
    manifest = tmp_path / "quarantine/manifests/paths-materialize.json"
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
        materialize_membership(tmp_path, membership, stored)
    assert occupied.read_bytes() == b"occupied"


def test_materialization_method_survives_manifest_round_trip(tmp_path):
    source = write_minimal_pdf(tmp_path / "source.pdf")
    stored = store_verified_pdf(tmp_path, source, score_for_path(source, "301"), run_id="r1")
    pending = make_membership("For guitar", "one.pdf")
    result = materialize_membership(tmp_path, pending, stored)
    committed = replace(pending, local_path=result.local_path, storage_method=result.storage_method)
    manifest = tmp_path / "metadata/memberships.json"
    atomic_write_models(manifest, "MembershipManifest", [committed])
    assert read_models(manifest, "MembershipManifest") == (committed,)
