from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from imslp_library.client import CategoryMember, FileMetadata, RevisionRecord
from imslp_library.config import LibraryConfig, load_allowlist
from imslp_library.enums import RunStatus
from imslp_library.jsonio import atomic_write_json
from imslp_library.models import RunSnapshot, RunState, ScoreFile
from imslp_library import snapshot as snapshot_module
from imslp_library.snapshot import SnapshotError, freeze_snapshot, load_complete_snapshot
from tests.basic_helpers import ROOT
from tests.network_helpers import FakeClock


RUN_ID = "run-20260830T120000Z"
ALPHA = "| *****FILES***** =\n{{#fte:imslpfile\n|File Name 1=alpha.pdf\n}}\n| *****WORK INFO*****\n|Instrumentation=3 guitars"
BETA = "| *****FILES***** =\n{{#fte:imslpfile\n|File Name 1=beta.pdf\n}}\n| *****WORK INFO*****\n|Instrumentation=3 guitars"


class SimulatedPowerLoss(BaseException):
    pass


def _config(tmp_path: Path, count: int = 1) -> LibraryConfig:
    payload = json.loads((ROOT / "config/categories.json").read_text(encoding="utf-8"))
    payload["version"] = f"test-{count}"
    payload["categories"] = payload["categories"][:count]
    path = tmp_path / f"categories-{count}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_allowlist(path)


def _revision(page_id: int, revision_id: int, title: str, content: str) -> RevisionRecord:
    return RevisionRecord(page_id, revision_id, title, content)


def _file(file_id: str, filename: str) -> FileMetadata:
    return FileMetadata(
        file_id=file_id,
        filename=filename,
        source_url=f"https://imslp.org/files/{filename}",
        expected_size=123,
        sha1_imslp="a" * 40,
        mime="application/pdf",
        copyright_label="Public Domain",
    )


class SnapshotClient:
    def __init__(
        self,
        members: dict[str, tuple[CategoryMember, ...]],
        revisions: tuple[RevisionRecord, ...],
        files: tuple[FileMetadata, ...],
        *,
        fail_category: str | None = None,
        fail_exact: Exception | None = None,
        fail_exact_for: dict[tuple[int, ...], Exception] | None = None,
        before_first_request=None,
    ) -> None:
        self.members = members
        self.revisions = revisions
        self.files = files
        self.fail_category = fail_category
        self.fail_exact = fail_exact
        self.fail_exact_for = fail_exact_for or {}
        self.before_first_request = before_first_request
        self.calls: list[tuple[str, object]] = []
        self._requested = False

    def _before(self) -> None:
        if not self._requested:
            self._requested = True
            if self.before_first_request is not None:
                self.before_first_request()

    def category_members(self, name: str) -> tuple[CategoryMember, ...]:
        self._before()
        self.calls.append(("category_members", name))
        if name == self.fail_category:
            raise RuntimeError("injected category crash")
        return self.members[name]

    def current_revisions(self, page_ids: tuple[int, ...]) -> tuple[RevisionRecord, ...]:
        self._before()
        self.calls.append(("current_revisions", page_ids))
        wanted = set(page_ids)
        return tuple(item for item in self.revisions if item.page_id in wanted)

    def exact_revisions(self, revision_ids: tuple[int, ...]) -> tuple[RevisionRecord, ...]:
        self._before()
        self.calls.append(("exact_revisions", revision_ids))
        if self.fail_exact is not None:
            raise self.fail_exact
        if revision_ids in self.fail_exact_for:
            raise self.fail_exact_for[revision_ids]
        wanted = set(revision_ids)
        return tuple(item for item in self.revisions if item.revision_id in wanted)

    def imageinfo(self, filenames: tuple[str, ...]) -> tuple[FileMetadata, ...]:
        self._before()
        self.calls.append(("imageinfo", filenames))
        wanted = set(filenames)
        return tuple(item for item in self.files if item.filename in wanted)


def _members(config: LibraryConfig, *page_ids: int) -> dict[str, tuple[CategoryMember, ...]]:
    return {
        category.name: tuple(CategoryMember(page_id, f"Work {page_id} (Composer, Test)") for page_id in page_ids)
        for category in config.categories
    }


def _read_snapshot(root: Path) -> RunSnapshot:
    return RunSnapshot.from_dict(json.loads((root / f"metadata/runs/{RUN_ID}.json").read_text(encoding="utf-8")))


def _read_state(root: Path) -> RunState:
    return RunState.from_dict(json.loads((root / f"metadata/runs/{RUN_ID}-state.json").read_text(encoding="utf-8")))


def test_initial_files_exist_before_network_and_category_resume_is_config_bound(tmp_path: Path) -> None:
    config = _config(tmp_path, 2)
    snapshot_path = tmp_path / f"metadata/runs/{RUN_ID}.json"
    state_path = tmp_path / f"metadata/runs/{RUN_ID}-state.json"

    def assert_initial_checkpoint() -> None:
        assert _read_snapshot(tmp_path).status is RunStatus.SNAPSHOT_INCOMPLETE
        assert _read_snapshot(tmp_path).categories == ()
        assert _read_state(tmp_path).snapshot_sha256 is None

    crashed = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
        fail_category=config.categories[1].name,
        before_first_request=assert_initial_checkpoint,
    )
    with pytest.raises(RuntimeError, match="category crash"):
        freeze_snapshot(tmp_path, RUN_ID, config, crashed, FakeClock.fixed())

    incomplete = _read_snapshot(tmp_path)
    assert incomplete.status is RunStatus.SNAPSHOT_INCOMPLETE
    assert [item.name for item in incomplete.categories] == [config.categories[0].name]
    assert [(item.page_id, item.revision_id) for item in incomplete.pages] == [(101, 201)]
    assert snapshot_path.is_file() and state_path.is_file()
    assert not list(snapshot_path.parent.glob("*.tmp"))

    wrong_config = replace(config, config_hash="2" * 64)
    untouched = SnapshotClient(_members(config, 101), (), ())
    with pytest.raises(SnapshotError, match="config.*(hash|binding)"):
        freeze_snapshot(tmp_path, RUN_ID, wrong_config, untouched, FakeClock.fixed())
    assert untouched.calls == []

    resumed = SnapshotClient(
        _members(config, 101),
        (
            _revision(101, 999, "Work 101 (Composer, Test)", ALPHA.replace("alpha.pdf", "current.pdf")),
            _revision(101, 201, "Work 101 (Composer, Test)", ALPHA),
        ),
        (_file("301", "alpha.pdf"),),
    )
    completed = freeze_snapshot(tmp_path, RUN_ID, config, resumed, FakeClock.fixed())
    assert completed.status is RunStatus.SNAPSHOT_COMPLETE
    assert ("category_members", config.categories[0].name) not in resumed.calls
    assert ("category_members", config.categories[1].name) in resumed.calls
    assert ("exact_revisions", (201,)) in resumed.calls
    assert not any(call[0] == "current_revisions" for call in resumed.calls)
    assert [item.name for item in completed.categories] == sorted(category.name for category in config.categories)


def test_all_29_categories_exact_files_completion_hash_and_immutable_reentry(tmp_path: Path) -> None:
    config = load_allowlist(ROOT / "config/categories.json")
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    snapshot = freeze_snapshot(tmp_path, RUN_ID, config, client, FakeClock.fixed())

    assert snapshot.status is RunStatus.SNAPSHOT_COMPLETE
    assert snapshot.snapshot_completed_at == FakeClock.fixed().now()
    assert len(snapshot.categories) == 29
    assert snapshot.pages[0].category_names == tuple(sorted(category.name for category in config.categories))
    assert snapshot.score_files[0].source_id == "source:f301@r201"
    cache = tmp_path / "metadata/.cache/pages/101/201.wiki"
    assert cache.read_text(encoding="utf-8") == ALPHA
    state = _read_state(tmp_path)
    snapshot_path = tmp_path / state.snapshot_path
    assert state.snapshot_sha256 == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    assert load_complete_snapshot(tmp_path, RUN_ID) == snapshot

    before_bytes = snapshot_path.read_bytes()
    before_mtime = snapshot_path.stat().st_mtime_ns
    no_requests = SnapshotClient({}, (), ())
    assert freeze_snapshot(tmp_path, RUN_ID, config, no_requests, FakeClock.fixed()) == snapshot
    assert no_requests.calls == []
    assert snapshot_path.read_bytes() == before_bytes
    assert snapshot_path.stat().st_mtime_ns == before_mtime


def test_incomplete_snapshot_cannot_be_loaded_for_downstream_work(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (),
        fail_exact=RuntimeError("stop before exact content"),
    )
    with pytest.raises(RuntimeError, match="stop before exact"):
        freeze_snapshot(tmp_path, RUN_ID, config, client, FakeClock.fixed())
    with pytest.raises(SnapshotError, match="incomplete"):
        load_complete_snapshot(tmp_path, RUN_ID)


def test_exact_revision_batch_checkpoint_resumes_without_refetching_completed_batch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    revisions = (
        _revision(101, 201, "Work 101 (Composer, Test)", ALPHA),
        _revision(102, 202, "Work 102 (Composer, Test)", BETA),
    )
    files = (_file("301", "alpha.pdf"), _file("302", "beta.pdf"))
    interrupted = SnapshotClient(
        _members(config, 101, 102),
        revisions,
        files,
        fail_exact_for={(202,): RuntimeError("second exact batch interrupted")},
    )
    with pytest.raises(RuntimeError, match="second exact batch"):
        freeze_snapshot(
            tmp_path,
            RUN_ID,
            config,
            interrupted,
            FakeClock.fixed(),
            revision_batch_size=1,
        )

    incomplete = _read_snapshot(tmp_path)
    assert incomplete.status is RunStatus.SNAPSHOT_INCOMPLETE
    assert [score.source_id for score in incomplete.score_files] == ["source:f301@r201"]
    assert (tmp_path / "metadata/.cache/pages/101/201.wiki").read_text(encoding="utf-8") == ALPHA
    assert not (tmp_path / "metadata/.cache/pages/102/202.wiki").exists()

    resumed = SnapshotClient(_members(config, 101, 102), revisions, files)
    completed = freeze_snapshot(
        tmp_path,
        RUN_ID,
        config,
        resumed,
        FakeClock.fixed(),
        revision_batch_size=1,
    )
    assert completed.status is RunStatus.SNAPSHOT_COMPLETE
    assert ("exact_revisions", (201,)) not in resumed.calls
    assert ("exact_revisions", (202,)) in resumed.calls


def test_corrupt_cache_from_completed_exact_batch_is_quarantined_and_refetched(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    revisions = (
        _revision(101, 201, "Work 101 (Composer, Test)", ALPHA),
        _revision(102, 202, "Work 102 (Composer, Test)", BETA),
    )
    files = (_file("301", "alpha.pdf"), _file("302", "beta.pdf"))
    interrupted = SnapshotClient(
        _members(config, 101, 102),
        revisions,
        files,
        fail_exact_for={(202,): RuntimeError("second batch interrupted")},
    )
    with pytest.raises(RuntimeError, match="second batch"):
        freeze_snapshot(
            library, RUN_ID, config, interrupted, FakeClock.fixed(), revision_batch_size=1
        )
    cache = library / "metadata/.cache/pages/101/201.wiki"
    cache.write_bytes(b"corrupt after completed batch")

    resumed = SnapshotClient(_members(config, 101, 102), revisions, files)
    completed = freeze_snapshot(
        library, RUN_ID, config, resumed, FakeClock.fixed(), revision_batch_size=1
    )
    assert completed.status is RunStatus.SNAPSHOT_COMPLETE
    assert ("exact_revisions", (201,)) in resumed.calls
    assert ("exact_revisions", (202,)) in resumed.calls
    assert cache.read_text(encoding="utf-8") == ALPHA
    assert (
        library / f"quarantine/cache/{RUN_ID}/101/201.wiki"
    ).read_bytes() == b"corrupt after completed batch"


def _leave_category_checkpoint(root: Path, config: LibraryConfig) -> None:
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
        fail_exact=RuntimeError("exact pass interrupted"),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        freeze_snapshot(root, RUN_ID, config, client, FakeClock.fixed())


def test_exact_revision_is_pinned_and_corrupt_cache_is_quarantined_exactly(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _leave_category_checkpoint(tmp_path, config)
    cache = tmp_path / "metadata/.cache/pages/101/201.wiki"
    cache.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b"not the captured revision"
    cache.write_bytes(corrupt)

    resumed = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    snapshot = freeze_snapshot(tmp_path, RUN_ID, config, resumed, FakeClock.fixed())

    assert snapshot.status is RunStatus.SNAPSHOT_COMPLETE
    assert ("exact_revisions", (201,)) in resumed.calls
    assert not any(call[0] == "current_revisions" for call in resumed.calls)
    quarantine = tmp_path / f"quarantine/cache/{RUN_ID}/101/201.wiki"
    assert quarantine.read_bytes() == corrupt
    manifest_path = tmp_path / f"quarantine/manifests/cache-{RUN_ID}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "model_type", "items"}
    assert manifest["schema_version"] == 1
    assert manifest["model_type"] == "CacheQuarantineManifest"
    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert set(item) == {
        "page_id", "revision_id", "source_path", "quarantine_path", "size",
        "expected_sha256", "actual_sha256", "reason", "quarantined_at",
    }
    assert item["page_id"] == 101 and item["revision_id"] == 201
    assert item["source_path"] == "metadata/.cache/pages/101/201.wiki"
    assert item["quarantine_path"] == f"quarantine/cache/{RUN_ID}/101/201.wiki"
    assert item["size"] == quarantine.stat().st_size == len(corrupt)
    assert item["actual_sha256"] == hashlib.sha256(quarantine.read_bytes()).hexdigest()
    assert item["expected_sha256"] == hashlib.sha256(ALPHA.encode("utf-8")).hexdigest()
    assert item["reason"] == "cache_sha256_mismatch"


def test_refetched_exact_revision_hash_mismatch_fails_after_quarantine(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _leave_category_checkpoint(tmp_path, config)
    cache = tmp_path / "metadata/.cache/pages/101/201.wiki"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"corrupt")
    changed = ALPHA.replace("alpha.pdf", "changed.pdf")
    resumed = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", changed),),
        (_file("999", "changed.pdf"),),
    )

    with pytest.raises(SnapshotError, match="returned content hash mismatch"):
        freeze_snapshot(tmp_path, RUN_ID, config, resumed, FakeClock.fixed())
    assert _read_snapshot(tmp_path).status is RunStatus.SNAPSHOT_INCOMPLETE
    assert not cache.exists()
    assert (tmp_path / f"quarantine/cache/{RUN_ID}/101/201.wiki").read_bytes() == b"corrupt"


def test_identical_records_in_different_api_order_have_identical_canonical_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    revisions = (
        _revision(101, 201, "Alpha (Composer, Test)", ALPHA),
        _revision(102, 202, "Beta (Composer, Test)", BETA),
    )
    files = (_file("301", "alpha.pdf"), _file("302", "beta.pdf"))
    roots = (tmp_path / "one", tmp_path / "two")
    category_name = config.categories[0].name
    clients = (
        SnapshotClient(
            {category_name: (CategoryMember(102, "Beta (Composer, Test)"), CategoryMember(101, "Alpha (Composer, Test)"))},
            tuple(reversed(revisions)),
            tuple(reversed(files)),
        ),
        SnapshotClient(
            {category_name: (CategoryMember(101, "Alpha (Composer, Test)"), CategoryMember(102, "Beta (Composer, Test)"))},
            revisions,
            files,
        ),
    )

    for root, client in zip(roots, clients, strict=True):
        freeze_snapshot(root, RUN_ID, config, client, FakeClock.fixed(), revision_batch_size=1)

    relative = Path(f"metadata/runs/{RUN_ID}.json")
    assert (roots[0] / relative).read_bytes() == (roots[1] / relative).read_bytes()
    first = _read_snapshot(roots[0])
    second = _read_snapshot(roots[1])
    assert first == second
    assert [(item.page_id, item.revision_id) for item in first.pages] == [(101, 201), (102, 202)]
    assert [item.source_id for item in first.score_files] == ["source:f301@r201", "source:f302@r202"]


def test_replaced_config_categories_or_hash_cannot_reuse_original_binding(tmp_path: Path) -> None:
    original = load_allowlist(ROOT / "config/categories.json")
    for forged in (
        replace(original, categories=original.categories[:1]),
        replace(original, config_hash="f" * 64),
    ):
        client = SnapshotClient({}, (), ())
        with pytest.raises(SnapshotError, match="config.*binding"):
            freeze_snapshot(tmp_path / forged.config_hash[:8], RUN_ID, forged, client, FakeClock.fixed())
        assert client.calls == []


def test_orphan_score_file_in_incomplete_snapshot_is_rejected_globally(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    _leave_category_checkpoint(library, config)
    snapshot_path = library / f"metadata/runs/{RUN_ID}.json"
    snapshot = _read_snapshot(library)
    orphan = ScoreFile(
        source_id="source:f999@r201",
        file_id="999",
        page_id=999,
        page_revision_id=201,
        filename="orphan.pdf",
        source_url="https://imslp.org/files/orphan.pdf",
        expected_size=1,
        sha1_imslp=None,
        source_hash_missing=True,
        mime="application/pdf",
        copyright_label=None,
        sha256=None,
        object_path=None,
    )
    atomic_write_json(snapshot_path, replace(snapshot, score_files=(orphan,)).to_dict())
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )

    with pytest.raises(SnapshotError, match="score file.*frozen page"):
        freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    assert client.calls == []


def test_initial_writes_reject_metadata_symlink_before_escape_or_request(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    outside = tmp_path / "outside"
    library.mkdir()
    outside.mkdir()
    (library / "metadata").symlink_to(outside, target_is_directory=True)
    client = SnapshotClient(_members(config, 101), (), ())

    with pytest.raises((ValueError, SnapshotError), match="symlink"):
        freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    assert list(outside.iterdir()) == []
    assert client.calls == []


def test_initial_snapshot_state_crash_window_reconciles_before_network(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    state_path = library / f"metadata/runs/{RUN_ID}-state.json"
    intent_path = library / f"metadata/runs/{RUN_ID}-initialization.json"
    real_write = snapshot_module.atomic_write_json
    raised = False

    def interrupt_state(path, mapping):
        nonlocal raised
        if Path(path) == state_path and mapping.get("model_type") == "RunState" and not raised:
            raised = True
            raise SimulatedPowerLoss("lost between initial snapshot and state")
        return real_write(path, mapping)

    monkeypatch.setattr(snapshot_module, "atomic_write_json", interrupt_state)
    client = SnapshotClient(_members(config, 101), (), ())
    with pytest.raises(SimulatedPowerLoss):
        freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    assert intent_path.is_file()
    assert client.calls == []

    monkeypatch.setattr(snapshot_module, "atomic_write_json", real_write)
    resumed = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    assert freeze_snapshot(library, RUN_ID, config, resumed, FakeClock.fixed()).status is RunStatus.SNAPSHOT_COMPLETE
    assert not intent_path.exists()


def test_quarantine_move_manifest_crash_is_reconciled_without_orphan(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    _leave_category_checkpoint(library, config)
    cache = library / "metadata/.cache/pages/101/201.wiki"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"corrupt")
    manifest_path = library / f"quarantine/manifests/cache-{RUN_ID}.json"
    intent_path = library / f"quarantine/transactions/cache-{RUN_ID}-101-201.json"
    quarantine = library / f"quarantine/cache/{RUN_ID}/101/201.wiki"
    real_write = snapshot_module.atomic_write_json

    def interrupt_manifest(path, mapping):
        if Path(path) == manifest_path:
            raise SimulatedPowerLoss("lost after cache move")
        return real_write(path, mapping)

    monkeypatch.setattr(snapshot_module, "atomic_write_json", interrupt_manifest)
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    with pytest.raises(SimulatedPowerLoss):
        freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    assert quarantine.read_bytes() == b"corrupt"
    assert intent_path.is_file()
    assert not manifest_path.exists()

    monkeypatch.setattr(snapshot_module, "atomic_write_json", real_write)
    resumed = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    assert freeze_snapshot(library, RUN_ID, config, resumed, FakeClock.fixed()).status is RunStatus.SNAPSHOT_COMPLETE
    assert not intent_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {item["quarantine_path"] for item in manifest["items"]} == {
        f"quarantine/cache/{RUN_ID}/101/201.wiki"
    }


def test_unmanifested_quarantine_tree_orphan_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    _leave_category_checkpoint(library, config)
    orphan = library / f"quarantine/cache/{RUN_ID}/101/201.wiki"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    with pytest.raises(SnapshotError, match="quarantine.*tree"):
        freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    assert client.calls == []


def test_complete_snapshot_without_completion_intent_cannot_be_reauthenticated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    snapshot_path = library / f"metadata/runs/{RUN_ID}.json"
    state_path = library / f"metadata/runs/{RUN_ID}-state.json"
    raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    raw["score_files"][0]["copyright_label"] = "Tampered"
    os.chmod(snapshot_path, 0o600)
    atomic_write_json(snapshot_path, raw)
    atomic_write_json(state_path, replace(_read_state(library), snapshot_sha256=None).to_dict())
    no_requests = SnapshotClient({}, (), ())

    with pytest.raises(SnapshotError, match="completion intent"):
        freeze_snapshot(library, RUN_ID, config, no_requests, FakeClock.fixed())
    assert no_requests.calls == []


def test_completion_intent_recovers_state_write_crash_without_snapshot_mutation(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    state_path = library / f"metadata/runs/{RUN_ID}-state.json"
    intent_path = library / f"metadata/runs/{RUN_ID}-completion.json"
    real_write = snapshot_module.atomic_write_json

    def interrupt_final_state(path, mapping):
        if Path(path) == state_path and mapping.get("snapshot_sha256") is not None:
            raise SimulatedPowerLoss("lost after complete snapshot write")
        return real_write(path, mapping)

    monkeypatch.setattr(snapshot_module, "atomic_write_json", interrupt_final_state)
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    with pytest.raises(SimulatedPowerLoss):
        freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    assert intent_path.is_file()
    snapshot_path = library / f"metadata/runs/{RUN_ID}.json"
    before = snapshot_path.read_bytes()
    before_mtime = snapshot_path.stat().st_mtime_ns

    monkeypatch.setattr(snapshot_module, "atomic_write_json", real_write)
    no_requests = SnapshotClient({}, (), ())
    completed = freeze_snapshot(library, RUN_ID, config, no_requests, FakeClock.fixed())
    assert completed.status is RunStatus.SNAPSHOT_COMPLETE
    assert no_requests.calls == []
    assert snapshot_path.read_bytes() == before
    assert snapshot_path.stat().st_mtime_ns == before_mtime
    assert not intent_path.exists()


def test_load_complete_blocks_residual_completion_intent_until_freeze_recovers(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    intent_path = library / f"metadata/runs/{RUN_ID}-completion.json"
    real_unlink = snapshot_module._unlink_durable

    def interrupt_intent_unlink(path):
        if Path(path) == intent_path:
            raise SimulatedPowerLoss("lost after final state before intent cleanup")
        return real_unlink(path)

    monkeypatch.setattr(snapshot_module, "_unlink_durable", interrupt_intent_unlink)
    client = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    with pytest.raises(SimulatedPowerLoss):
        freeze_snapshot(library, RUN_ID, config, client, FakeClock.fixed())
    assert intent_path.is_file()
    with pytest.raises(SnapshotError, match="unsettled.*intent"):
        load_complete_snapshot(library, RUN_ID)
    snapshot_path = library / f"metadata/runs/{RUN_ID}.json"
    before = snapshot_path.read_bytes()
    before_mtime = snapshot_path.stat().st_mtime_ns

    monkeypatch.setattr(snapshot_module, "_unlink_durable", real_unlink)
    no_requests = SnapshotClient({}, (), ())
    freeze_snapshot(library, RUN_ID, config, no_requests, FakeClock.fixed())
    assert no_requests.calls == []
    assert snapshot_path.read_bytes() == before
    assert snapshot_path.stat().st_mtime_ns == before_mtime
    assert load_complete_snapshot(library, RUN_ID).status is RunStatus.SNAPSHOT_COMPLETE


def test_forged_typed_imageinfo_fields_fail_checkpoint_authority_before_network(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    revisions = (
        _revision(101, 201, "Work 101 (Composer, Test)", ALPHA),
        _revision(102, 202, "Work 102 (Composer, Test)", BETA),
    )
    files = (_file("301", "alpha.pdf"), _file("302", "beta.pdf"))
    interrupted = SnapshotClient(
        _members(config, 101, 102),
        revisions,
        files,
        fail_exact_for={(202,): RuntimeError("leave first exact batch checkpointed")},
    )
    with pytest.raises(RuntimeError, match="first exact batch"):
        freeze_snapshot(
            library, RUN_ID, config, interrupted, FakeClock.fixed(), revision_batch_size=1
        )
    snapshot_path = library / f"metadata/runs/{RUN_ID}.json"
    snapshot = _read_snapshot(library)
    forged = replace(
        snapshot.score_files[0],
        source_id="source:f777@r201",
        file_id="777",
        source_url="https://imslp.org/files/forged-alpha.pdf",
        expected_size=999,
        sha1_imslp="b" * 40,
        source_hash_missing=False,
        mime="application/pdf",
        copyright_label="Public Domain",
    )
    atomic_write_json(snapshot_path, replace(snapshot, score_files=(forged,)).to_dict())
    no_requests = SnapshotClient({}, (), ())

    with pytest.raises(SnapshotError, match="checkpoint authority"):
        freeze_snapshot(library, RUN_ID, config, no_requests, FakeClock.fixed())
    assert no_requests.calls == []


@pytest.mark.parametrize("phase", ["snapshot", "state", "authority", "transition_cleanup"])
def test_checkpoint_transition_recovers_every_write_window_and_skips_category(
    tmp_path: Path,
    monkeypatch,
    phase: str,
) -> None:
    config = _config(tmp_path)
    library = tmp_path / phase
    snapshot_path = library / f"metadata/runs/{RUN_ID}.json"
    state_path = library / f"metadata/runs/{RUN_ID}-state.json"
    authority_path = library / f"metadata/runs/{RUN_ID}-checkpoint.json"
    transition_path = library / f"metadata/runs/{RUN_ID}-checkpoint-transition.json"
    real_write = snapshot_module.atomic_write_json
    real_unlink = snapshot_module._unlink_durable

    def interrupt_write(path, mapping):
        target = Path(path)
        should_interrupt = (
            (
                phase == "snapshot"
                and target == snapshot_path
                and mapping.get("model_type") == "RunSnapshot"
                and bool(mapping.get("categories"))
            )
            or (phase == "state" and target == state_path and transition_path.exists())
            or (phase == "authority" and target == authority_path and mapping.get("generation") == 1)
        )
        if should_interrupt:
            raise SimulatedPowerLoss(f"checkpoint {phase} write lost")
        return real_write(path, mapping)

    def interrupt_unlink(path):
        if phase == "transition_cleanup" and Path(path) == transition_path:
            raise SimulatedPowerLoss("checkpoint transition cleanup lost")
        return real_unlink(path)

    monkeypatch.setattr(snapshot_module, "atomic_write_json", interrupt_write)
    monkeypatch.setattr(snapshot_module, "_unlink_durable", interrupt_unlink)
    first = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    with pytest.raises(SimulatedPowerLoss):
        freeze_snapshot(library, RUN_ID, config, first, FakeClock.fixed())
    assert transition_path.is_file()

    monkeypatch.setattr(snapshot_module, "atomic_write_json", real_write)
    monkeypatch.setattr(snapshot_module, "_unlink_durable", real_unlink)
    resumed = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    completed = freeze_snapshot(library, RUN_ID, config, resumed, FakeClock.fixed())
    assert completed.status is RunStatus.SNAPSHOT_COMPLETE
    assert not any(call[0] in {"category_members", "current_revisions"} for call in resumed.calls)
    assert ("exact_revisions", (201,)) in resumed.calls
    assert not transition_path.exists() and not authority_path.exists()


@pytest.mark.parametrize(
    "mutation",
    ["extra_key", "run_id", "config_hash", "snapshot_hash", "missing", "symlink"],
)
def test_checkpoint_authority_is_strict_and_path_bound(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = _config(tmp_path)
    library = tmp_path / mutation
    _leave_category_checkpoint(library, config)
    authority_path = library / f"metadata/runs/{RUN_ID}-checkpoint.json"
    payload = json.loads(authority_path.read_text(encoding="utf-8"))
    if mutation == "extra_key":
        payload["extra"] = True
        atomic_write_json(authority_path, payload)
    elif mutation == "run_id":
        payload["run_id"] = "other-run"
        atomic_write_json(authority_path, payload)
    elif mutation == "config_hash":
        payload["config_sha256"] = "f" * 64
        atomic_write_json(authority_path, payload)
    elif mutation == "snapshot_hash":
        payload["snapshot_sha256"] = "f" * 64
        atomic_write_json(authority_path, payload)
    elif mutation == "missing":
        authority_path.unlink()
    else:
        outside = tmp_path / "outside-authority.json"
        authority_path.replace(outside)
        authority_path.symlink_to(outside)
    no_requests = SnapshotClient({}, (), ())

    with pytest.raises(SnapshotError, match="checkpoint authority"):
        freeze_snapshot(library, RUN_ID, config, no_requests, FakeClock.fixed())
    assert no_requests.calls == []


def test_initial_checkpoint_authority_write_crash_recovers_before_network(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    authority_path = library / f"metadata/runs/{RUN_ID}-checkpoint.json"
    initialization_path = library / f"metadata/runs/{RUN_ID}-initialization.json"
    real_write = snapshot_module.atomic_write_json
    raised = False

    def interrupt_initial_authority(path, mapping):
        nonlocal raised
        if Path(path) == authority_path and mapping.get("generation") == 0 and not raised:
            raised = True
            raise SimulatedPowerLoss("lost before initial checkpoint authority")
        return real_write(path, mapping)

    monkeypatch.setattr(snapshot_module, "atomic_write_json", interrupt_initial_authority)
    first = SnapshotClient(_members(config, 101), (), ())
    with pytest.raises(SimulatedPowerLoss):
        freeze_snapshot(library, RUN_ID, config, first, FakeClock.fixed())
    assert first.calls == []
    assert initialization_path.is_file()

    monkeypatch.setattr(snapshot_module, "atomic_write_json", real_write)
    resumed = SnapshotClient(
        _members(config, 101),
        (_revision(101, 201, "Work 101 (Composer, Test)", ALPHA),),
        (_file("301", "alpha.pdf"),),
    )
    completed = freeze_snapshot(library, RUN_ID, config, resumed, FakeClock.fixed())
    assert completed.status is RunStatus.SNAPSHOT_COMPLETE
    assert not initialization_path.exists() and not authority_path.exists()


def test_exact_batch_transition_recovers_without_refetching_committed_batch(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    library = tmp_path / "library"
    authority_path = library / f"metadata/runs/{RUN_ID}-checkpoint.json"
    transition_path = library / f"metadata/runs/{RUN_ID}-checkpoint-transition.json"
    real_write = snapshot_module.atomic_write_json
    raised = False

    def interrupt_first_exact_authority(path, mapping):
        nonlocal raised
        if Path(path) == authority_path and mapping.get("generation") == 2 and not raised:
            raised = True
            raise SimulatedPowerLoss("lost committing first exact batch authority")
        return real_write(path, mapping)

    revisions = (
        _revision(101, 201, "Work 101 (Composer, Test)", ALPHA),
        _revision(102, 202, "Work 102 (Composer, Test)", BETA),
    )
    files = (_file("301", "alpha.pdf"), _file("302", "beta.pdf"))
    monkeypatch.setattr(snapshot_module, "atomic_write_json", interrupt_first_exact_authority)
    first = SnapshotClient(_members(config, 101, 102), revisions, files)
    with pytest.raises(SimulatedPowerLoss):
        freeze_snapshot(library, RUN_ID, config, first, FakeClock.fixed(), revision_batch_size=1)
    assert transition_path.is_file()
    assert ("exact_revisions", (201,)) in first.calls

    monkeypatch.setattr(snapshot_module, "atomic_write_json", real_write)
    resumed = SnapshotClient(_members(config, 101, 102), revisions, files)
    completed = freeze_snapshot(
        library, RUN_ID, config, resumed, FakeClock.fixed(), revision_batch_size=1
    )
    assert completed.status is RunStatus.SNAPSHOT_COMPLETE
    assert ("exact_revisions", (201,)) not in resumed.calls
    assert ("exact_revisions", (202,)) in resumed.calls
    assert not transition_path.exists() and not authority_path.exists()
