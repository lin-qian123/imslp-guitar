from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from imslp_library.client import CategoryMember, FileMetadata, RevisionRecord
from imslp_library.config import LibraryConfig, load_allowlist
from imslp_library.enums import RunStatus
from imslp_library.models import RunSnapshot, RunState
from imslp_library.snapshot import SnapshotError, freeze_snapshot, load_complete_snapshot
from tests.basic_helpers import ROOT
from tests.network_helpers import FakeClock


RUN_ID = "run-20260830T120000Z"
ALPHA = "| *****FILES***** =\n{{#fte:imslpfile\n|File Name 1=alpha.pdf\n}}\n| *****WORK INFO*****\n|Instrumentation=3 guitars"
BETA = "| *****FILES***** =\n{{#fte:imslpfile\n|File Name 1=beta.pdf\n}}\n| *****WORK INFO*****\n|Instrumentation=3 guitars"


def _config(count: int = 1, *, config_hash: str = "1" * 64) -> LibraryConfig:
    source = load_allowlist(ROOT / "config/categories.json")
    return replace(source, categories=source.categories[:count], config_hash=config_hash)


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
    config = _config(2)
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
    with pytest.raises(SnapshotError, match="config hash"):
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
    config = _config()
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
    config = _config()
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
    config = _config()
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
    config = _config()
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
    config = _config()
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
