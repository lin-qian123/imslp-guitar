from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .client import Clock, FileMetadata, ImslpClient, RevisionRecord
from .config import LibraryConfig
from .enums import RunStatus
from .headings import parse_heading_tree
from .jsonio import atomic_write_json, read_json
from .models import CategorySnapshot, FrozenPage, RunSnapshot, RunState, ScoreFile
from .paths import _assert_safe_write_target


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CACHE_ITEM_KEYS = {
    "page_id",
    "revision_id",
    "source_path",
    "quarantine_path",
    "size",
    "expected_sha256",
    "actual_sha256",
    "reason",
    "quarantined_at",
}


class SnapshotError(RuntimeError):
    """A frozen-run invariant failed; no current content may be substituted."""


def _paths(root: Path, run_id: str) -> tuple[Path, Path]:
    base = root / "metadata/runs"
    return base / f"{run_id}.json", base / f"{run_id}-state.json"


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_from_path(path: Path) -> RunSnapshot:
    try:
        return RunSnapshot.from_dict(read_json(path))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SnapshotError(f"invalid run snapshot: {path}") from exc


def _state_from_path(path: Path) -> RunState:
    try:
        return RunState.from_dict(read_json(path))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SnapshotError(f"invalid run state: {path}") from exc


def _new_state(root: Path, run_id: str, snapshot_path: Path, now: datetime) -> RunState:
    return RunState(
        schema_version=1,
        run_id=run_id,
        snapshot_path=_relative(root, snapshot_path),
        snapshot_sha256=None,
        start_drift_report_path=None,
        start_drift_report_sha256=None,
        end_drift_report_path=None,
        end_drift_report_sha256=None,
        download_attempt_manifest_path=None,
        updated_at=now,
    )


def _checkpoint(
    root: Path,
    snapshot_path: Path,
    state_path: Path,
    snapshot: RunSnapshot,
    state: RunState,
    now: datetime,
) -> RunState:
    if snapshot.status is not RunStatus.SNAPSHOT_INCOMPLETE:
        raise SnapshotError("only an incomplete snapshot may be checkpointed")
    _assert_safe_write_target(root, snapshot_path, "run snapshot")
    _assert_safe_write_target(root, state_path, "run state")
    atomic_write_json(snapshot_path, snapshot.to_dict())
    updated = replace(state, snapshot_sha256=None, updated_at=now)
    atomic_write_json(state_path, updated.to_dict())
    return updated


def _cache_path(root: Path, page: FrozenPage) -> Path:
    expected = root / "metadata/.cache/pages" / str(page.page_id) / f"{page.revision_id}.wiki"
    if _relative(root, expected) != page.wikitext_path:
        raise SnapshotError("frozen page cache path is not canonical")
    return expected


def _atomic_write_bytes(root: Path, path: Path, content: bytes) -> None:
    _assert_safe_write_target(root, path, "revision cache")
    if path.is_symlink():
        raise SnapshotError("revision cache target is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_write_target(root, path, "revision cache")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def _safe_manifest_relative(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _validate_manifest_item(
    root: Path,
    run_id: str,
    expected_hashes: dict[tuple[int, int], str],
    item: dict[str, object],
) -> None:
    if set(item) != _CACHE_ITEM_KEYS:
        raise SnapshotError("invalid CacheQuarantineManifest item keys")
    try:
        timestamp = datetime.fromisoformat(item["quarantined_at"]) if isinstance(item["quarantined_at"], str) else None
    except ValueError:
        timestamp = None
    valid = (
        type(item["page_id"]) is int
        and item["page_id"] >= 0
        and type(item["revision_id"]) is int
        and item["revision_id"] >= 0
        and _safe_manifest_relative(item["source_path"])
        and _safe_manifest_relative(item["quarantine_path"])
        and type(item["size"]) is int
        and item["size"] >= 0
        and isinstance(item["expected_sha256"], str)
        and _SHA256_RE.fullmatch(item["expected_sha256"]) is not None
        and isinstance(item["actual_sha256"], str)
        and _SHA256_RE.fullmatch(item["actual_sha256"]) is not None
        and item["reason"] == "cache_sha256_mismatch"
        and timestamp is not None
        and timestamp.tzinfo is not None
        and timestamp.utcoffset() is not None
    )
    if not valid:
        raise SnapshotError("invalid CacheQuarantineManifest item")
    identity = (item["page_id"], item["revision_id"])
    expected_source = f"metadata/.cache/pages/{item['page_id']}/{item['revision_id']}.wiki"
    expected_quarantine = f"quarantine/cache/{run_id}/{item['page_id']}/{item['revision_id']}.wiki"
    if (
        item["source_path"] != expected_source
        or item["quarantine_path"] != expected_quarantine
        or expected_hashes.get(identity) != item["expected_sha256"]
    ):
        raise SnapshotError("CacheQuarantineManifest does not match the frozen checkpoint")
    quarantine = root / str(item["quarantine_path"])
    _assert_safe_write_target(root, quarantine, "cache quarantine")
    if quarantine.is_symlink() or not quarantine.is_file():
        raise SnapshotError("cache quarantine manifest does not equal quarantine tree")
    if quarantine.stat().st_size != item["size"] or _sha256_file(quarantine) != item["actual_sha256"]:
        raise SnapshotError("cache quarantine manifest does not equal quarantine tree")


def _read_cache_manifest(
    root: Path,
    path: Path,
    run_id: str,
    expected_hashes: dict[tuple[int, int], str],
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    if path.is_symlink():
        raise SnapshotError("CacheQuarantineManifest cannot be a symlink")
    try:
        payload = read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid CacheQuarantineManifest") from exc
    if set(payload) != {"schema_version", "model_type", "items"}:
        raise SnapshotError("invalid CacheQuarantineManifest envelope keys")
    if payload["schema_version"] != 1 or payload["model_type"] != "CacheQuarantineManifest":
        raise SnapshotError("invalid CacheQuarantineManifest envelope identity")
    items = payload["items"]
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise SnapshotError("invalid CacheQuarantineManifest items")
    identities: set[tuple[int, int]] = set()
    for item in items:
        _validate_manifest_item(root, run_id, expected_hashes, item)
        identity = (item["page_id"], item["revision_id"])
        if identity in identities:
            raise SnapshotError("duplicate CacheQuarantineManifest identity")
        identities.add(identity)
    return items


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quarantine_corrupt_cache(root: Path, run_id: str, page: FrozenPage, clock: Clock) -> None:
    source = _cache_path(root, page)
    quarantine = root / "quarantine/cache" / run_id / str(page.page_id) / f"{page.revision_id}.wiki"
    manifest_path = root / "quarantine/manifests" / f"cache-{run_id}.json"
    for path, label in ((source, "revision cache"), (quarantine, "cache quarantine"), (manifest_path, "cache quarantine manifest")):
        _assert_safe_write_target(root, path, label)
    if source.is_symlink() or not source.is_file():
        raise SnapshotError("corrupt revision cache is not a regular file")
    mode = os.stat(source, follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode) or os.stat(source, follow_symlinks=False).st_nlink != 1:
        raise SnapshotError("corrupt revision cache must be singly linked")
    if quarantine.exists() or quarantine.is_symlink():
        raise SnapshotError("cache quarantine destination already exists")
    run_snapshot = _snapshot_from_path(root / "metadata/runs" / f"{run_id}.json")
    expected_hashes = {
        (item.page_id, item.revision_id): item.wikitext_sha256
        for item in run_snapshot.pages
    }
    if expected_hashes.get((page.page_id, page.revision_id)) != page.wikitext_sha256:
        raise SnapshotError("corrupt cache page is not in the frozen checkpoint")
    items = _read_cache_manifest(root, manifest_path, run_id, expected_hashes)
    if any(item["page_id"] == page.page_id and item["revision_id"] == page.revision_id for item in items):
        raise SnapshotError("cache quarantine identity already exists")
    size = source.stat().st_size
    actual = _sha256_file(source)
    item: dict[str, object] = {
        "page_id": page.page_id,
        "revision_id": page.revision_id,
        "source_path": _relative(root, source),
        "quarantine_path": _relative(root, quarantine),
        "size": size,
        "expected_sha256": page.wikitext_sha256,
        "actual_sha256": actual,
        "reason": "cache_sha256_mismatch",
        "quarantined_at": clock.now().isoformat(),
    }
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, quarantine)
    _fsync_directory(source.parent)
    _fsync_directory(quarantine.parent)
    try:
        atomic_write_json(
            manifest_path,
            {"schema_version": 1, "model_type": "CacheQuarantineManifest", "items": [*items, item]},
        )
        _read_cache_manifest(root, manifest_path, run_id, expected_hashes)
    except BaseException:
        if quarantine.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(quarantine, source)
            _fsync_directory(source.parent)
            _fsync_directory(quarantine.parent)
        raise


def _cache_is_valid(root: Path, run_id: str, page: FrozenPage, clock: Clock) -> bool:
    path = _cache_path(root, page)
    _assert_safe_write_target(root, path, "revision cache")
    if path.is_symlink():
        raise SnapshotError("revision cache is a symlink")
    if not path.exists():
        return False
    if not path.is_file():
        raise SnapshotError("revision cache is not a regular file")
    if _sha256_file(path) == page.wikitext_sha256:
        return True
    _quarantine_corrupt_cache(root, run_id, page, clock)
    return False


def _page_filenames(root: Path, page: FrozenPage) -> tuple[str, ...]:
    path = _cache_path(root, page)
    try:
        wikitext = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SnapshotError("cannot read exact revision cache") from exc
    if _sha256_bytes(wikitext.encode("utf-8")) != page.wikitext_sha256:
        raise SnapshotError("exact revision cache hash changed")
    try:
        chunks = parse_heading_tree(wikitext).file_templates
    except (TypeError, ValueError) as exc:
        raise SnapshotError("cannot parse exact revision file metadata") from exc
    return tuple(sorted({chunk.filename for chunk in chunks}))


def _score_metadata_complete(root: Path, page: FrozenPage, scores: tuple[ScoreFile, ...]) -> bool:
    expected = set(_page_filenames(root, page))
    actual_scores = tuple(
        score for score in scores
        if score.page_id == page.page_id and score.page_revision_id == page.revision_id
    )
    actual = {score.filename for score in actual_scores}
    return actual == expected and len(actual_scores) == len(actual)


def _scores_for_page(
    root: Path,
    page: FrozenPage,
    metadata_by_filename: dict[str, FileMetadata],
) -> tuple[ScoreFile, ...]:
    path = _cache_path(root, page)
    wikitext = path.read_text(encoding="utf-8")
    tree = parse_heading_tree(wikitext)
    chunks_by_name: dict[str, list[object]] = {}
    for chunk in tree.file_templates:
        chunks_by_name.setdefault(chunk.filename, []).append(chunk)
    scores: list[ScoreFile] = []
    for filename in sorted(chunks_by_name):
        metadata = metadata_by_filename.get(filename)
        if metadata is None:
            raise SnapshotError(f"file metadata missing for exact revision attachment: {filename}")
        chunks = chunks_by_name[filename]
        for chunk in chunks:
            if chunk.file_id_conflict:
                raise SnapshotError(f"conflicting file IDs in exact revision attachment: {filename}")
            if chunk.file_id is not None and chunk.file_id != metadata.file_id:
                raise SnapshotError(f"wikitext/imageinfo file ID mismatch: {filename}")
        scores.append(ScoreFile(
            source_id=f"source:f{metadata.file_id}@r{page.revision_id}",
            file_id=metadata.file_id,
            page_id=page.page_id,
            page_revision_id=page.revision_id,
            filename=metadata.filename,
            source_url=metadata.source_url,
            expected_size=metadata.expected_size,
            sha1_imslp=metadata.sha1_imslp,
            source_hash_missing=metadata.sha1_imslp is None,
            mime=metadata.mime,
            copyright_label=metadata.copyright_label,
            sha256=None,
            object_path=None,
        ))
    return tuple(scores)


def _validate_existing(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    snapshot_path: Path,
    state_path: Path,
) -> tuple[RunSnapshot, RunState] | None:
    if snapshot_path.exists() != state_path.exists():
        raise SnapshotError("run snapshot and state must exist together")
    if not snapshot_path.exists():
        return None
    if snapshot_path.is_symlink() or state_path.is_symlink():
        raise SnapshotError("run snapshot and state cannot be symlinks")
    snapshot = _snapshot_from_path(snapshot_path)
    state = _state_from_path(state_path)
    expected_path = _relative(root, snapshot_path)
    if snapshot.run_id != run_id or state.run_id != run_id or state.snapshot_path != expected_path:
        raise SnapshotError("run identity or snapshot path mismatch")
    if snapshot.config_version != config.version or snapshot.config_sha256 != config.config_hash:
        raise SnapshotError("run config hash/version does not match resume request")
    return snapshot, state


def load_complete_snapshot(root: Path, run_id: str) -> RunSnapshot:
    root = Path(root)
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id is not path-safe")
    snapshot_path, state_path = _paths(root, run_id)
    if not snapshot_path.exists() or not state_path.exists():
        raise SnapshotError("run snapshot or state is missing")
    if snapshot_path.is_symlink() or state_path.is_symlink():
        raise SnapshotError("run snapshot and state cannot be symlinks")
    snapshot = _snapshot_from_path(snapshot_path)
    state = _state_from_path(state_path)
    if (
        snapshot.run_id != run_id
        or state.run_id != run_id
        or state.snapshot_path != _relative(root, snapshot_path)
    ):
        raise SnapshotError("run identity or snapshot path mismatch")
    if snapshot.status is not RunStatus.SNAPSHOT_COMPLETE or state.snapshot_sha256 is None:
        raise SnapshotError("snapshot is incomplete and cannot be used downstream")
    actual = _sha256_file(snapshot_path)
    if state.snapshot_sha256 != actual:
        raise SnapshotError("completed snapshot SHA-256 does not match RunState")
    return snapshot


def freeze_snapshot(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    client: ImslpClient,
    clock: Clock,
    *,
    revision_batch_size: int = 50,
) -> RunSnapshot:
    """Freeze an allowlisted IMSLP run using only captured exact revision IDs."""

    root = Path(root)
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id is not path-safe")
    if not isinstance(config, LibraryConfig):
        raise TypeError("config must be a LibraryConfig")
    if not isinstance(config.config_hash, str) or _SHA256_RE.fullmatch(config.config_hash) is None:
        raise ValueError("config hash must be canonical SHA-256")
    if type(revision_batch_size) is not int or revision_batch_size <= 0:
        raise ValueError("revision_batch_size must be positive")
    root.mkdir(parents=True, exist_ok=True)
    snapshot_path, state_path = _paths(root, run_id)
    existing = _validate_existing(root, run_id, config, snapshot_path, state_path)
    if existing is None:
        started = clock.now()
        snapshot = RunSnapshot(
            schema_version=1,
            run_id=run_id,
            config_version=config.version,
            config_sha256=config.config_hash,
            snapshot_started_at=started,
            snapshot_completed_at=None,
            status=RunStatus.SNAPSHOT_INCOMPLETE,
            categories=(),
            pages=(),
            score_files=(),
        )
        state = _new_state(root, run_id, snapshot_path, started)
        # Both durable files precede the first possible client request.
        atomic_write_json(snapshot_path, snapshot.to_dict())
        atomic_write_json(state_path, state.to_dict())
    else:
        snapshot, state = existing
        if snapshot.status is RunStatus.SNAPSHOT_COMPLETE:
            actual = _sha256_file(snapshot_path)
            if state.snapshot_sha256 is None:
                state = replace(state, snapshot_sha256=actual, updated_at=clock.now())
                atomic_write_json(state_path, state.to_dict())
            elif state.snapshot_sha256 != actual:
                raise SnapshotError("completed snapshot SHA-256 does not match RunState")
            return snapshot
        if snapshot.status is not RunStatus.SNAPSHOT_INCOMPLETE or state.snapshot_sha256 is not None:
            raise SnapshotError("run cannot resume from its current state")

    category_by_name = {category.name: category for category in snapshot.categories}
    pages_by_id = {page.page_id: page for page in snapshot.pages}
    if len(pages_by_id) != len(snapshot.pages):
        raise SnapshotError("snapshot contains duplicate page identities")
    approved_names = {category.name for category in config.categories}
    if not set(category_by_name).issubset(approved_names):
        raise SnapshotError("snapshot contains a category outside the config")

    for category in config.categories:
        if category.name in category_by_name:
            continue
        members = client.category_members(category.name)
        member_by_id = {member.page_id: member for member in members}
        if len(member_by_id) != len(members):
            raise SnapshotError("category response contains duplicate page IDs")
        unseen_ids = tuple(sorted(set(member_by_id) - set(pages_by_id)))
        if unseen_ids:
            current = client.current_revisions(unseen_ids)
            current_by_page = {record.page_id: record for record in current}
            if len(current_by_page) != len(current) or set(current_by_page) != set(unseen_ids):
                raise SnapshotError("current revision response does not exactly match category members")
            for page_id in unseen_ids:
                record = current_by_page[page_id]
                if record.title != member_by_id[page_id].title:
                    raise SnapshotError("category member title does not match revision title")
                digest = _sha256_bytes(record.wikitext.encode("utf-8"))
                path = root / "metadata/.cache/pages" / str(page_id) / f"{record.revision_id}.wiki"
                pages_by_id[page_id] = FrozenPage(
                    page_id=page_id,
                    revision_id=record.revision_id,
                    page_title=record.title,
                    category_names=(category.name,),
                    wikitext_path=_relative(root, path),
                    wikitext_sha256=digest,
                )
        for page_id, member in member_by_id.items():
            page = pages_by_id[page_id]
            if page.page_title != member.title:
                raise SnapshotError("overlapping category member title conflict")
            pages_by_id[page_id] = replace(
                page,
                category_names=tuple(sorted(set(page.category_names) | {category.name})),
            )
        category_by_name[category.name] = CategorySnapshot(
            name=category.name,
            member_page_ids=tuple(member_by_id),
            member_count=len(member_by_id),
        )
        snapshot = replace(
            snapshot,
            categories=tuple(category_by_name.values()),
            pages=tuple(pages_by_id.values()),
        )
        state = _checkpoint(root, snapshot_path, state_path, snapshot, state, clock.now())

    ordered_pages = tuple(sorted(pages_by_id.values(), key=lambda page: (page.page_id, page.revision_id)))
    scores = list(snapshot.score_files)
    pending_pages: list[FrozenPage] = []
    for page in ordered_pages:
        cache_valid = _cache_is_valid(root, run_id, page, clock)
        if not cache_valid or not _score_metadata_complete(root, page, tuple(scores)):
            pending_pages.append(page)

    for offset in range(0, len(pending_pages), revision_batch_size):
        batch = tuple(pending_pages[offset:offset + revision_batch_size])
        needs_exact = tuple(page for page in batch if not _cache_is_valid(root, run_id, page, clock))
        if needs_exact:
            requested_revision_ids = tuple(page.revision_id for page in needs_exact)
            returned = client.exact_revisions(requested_revision_ids)
            by_revision = {record.revision_id: record for record in returned}
            if len(by_revision) != len(returned) or set(by_revision) != set(requested_revision_ids):
                raise SnapshotError("exact revision response does not match captured revision IDs")
            for page in needs_exact:
                record: RevisionRecord = by_revision[page.revision_id]
                if record.page_id != page.page_id or record.title != page.page_title:
                    raise SnapshotError("exact revision response changed page identity")
                content = record.wikitext.encode("utf-8")
                if _sha256_bytes(content) != page.wikitext_sha256:
                    raise SnapshotError("returned content hash mismatch for captured exact revision")
                _atomic_write_bytes(root, _cache_path(root, page), content)

        filenames = tuple(sorted({filename for page in batch for filename in _page_filenames(root, page)}))
        metadata = client.imageinfo(filenames) if filenames else ()
        metadata_by_filename = {item.filename: item for item in metadata}
        if len(metadata_by_filename) != len(metadata) or set(metadata_by_filename) != set(filenames):
            raise SnapshotError("file metadata response does not exactly match exact revision attachments")
        batch_identities = {(page.page_id, page.revision_id) for page in batch}
        scores = [
            score for score in scores
            if (score.page_id, score.page_revision_id) not in batch_identities
        ]
        for page in batch:
            scores.extend(_scores_for_page(root, page, metadata_by_filename))
        snapshot = replace(snapshot, score_files=tuple(scores))
        state = _checkpoint(root, snapshot_path, state_path, snapshot, state, clock.now())

    if set(category_by_name) != approved_names:
        raise SnapshotError("not all configured categories were checkpointed")
    for page in ordered_pages:
        if not _cache_is_valid(root, run_id, page, clock) or not _score_metadata_complete(root, page, snapshot.score_files):
            raise SnapshotError("exact revisions and file metadata are incomplete")

    completed = replace(
        snapshot,
        status=RunStatus.SNAPSHOT_COMPLETE,
        snapshot_completed_at=clock.now(),
    )
    # The one-way transition is the last snapshot write. Re-entry only verifies it.
    atomic_write_json(snapshot_path, completed.to_dict())
    snapshot_digest = _sha256_file(snapshot_path)
    final_state = replace(state, snapshot_sha256=snapshot_digest, updated_at=clock.now())
    atomic_write_json(state_path, final_state.to_dict())
    try:
        snapshot_path.chmod(0o444)
    except OSError:
        pass
    return completed
