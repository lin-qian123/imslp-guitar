from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .enums import StorageMethod
from .jsonio import atomic_write_json, read_json
from .models import MaterializationResult, Membership, StoredObject
from .paths import _assert_safe_write_target
from .storage import _validate_pdf, hash_file_sha256, object_path_for_hash


class StorageCapabilityError(RuntimeError):
    pass


class MaterializationConflictError(RuntimeError):
    pass


_PATH_ITEM_KEYS = {"original_path", "quarantine_path", "reason", "size", "sha256"}


def _safe_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None or run_id in {".", ".."}:
        raise ValueError("run_id must be a safe canonical path component")
    return run_id


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def probe_storage_capability(root: Path) -> StorageMethod:
    root = Path(root)
    _assert_safe_write_target(root, root / ".imslp-link-probe", "capability probe")
    root.mkdir(parents=True, exist_ok=True)
    probe = Path(tempfile.mkdtemp(prefix=".imslp-link-probe-", dir=root))
    source = probe / "source"
    hardlink = probe / "hardlink"
    symlink = probe / "symlink"
    try:
        source.write_bytes(b"imslp-link-capability-probe")
        try:
            os.link(source, hardlink)
            if hardlink.stat().st_ino == source.stat().st_ino and hardlink.read_bytes() == source.read_bytes():
                return StorageMethod.HARDLINK
        except OSError:
            pass
        finally:
            _unlink_if_present(hardlink)
        relative_target = os.path.relpath(source, symlink.parent)
        if os.path.isabs(relative_target):
            raise StorageCapabilityError("relative symlink probe produced an absolute target")
        try:
            os.symlink(relative_target, symlink)
            if not _inside(root, symlink) or symlink.resolve() != source.resolve():
                raise StorageCapabilityError("relative symlink probe escaped target root")
            if symlink.read_bytes() == source.read_bytes():
                return StorageMethod.RELATIVE_SYMLINK
        except OSError as exc:
            raise StorageCapabilityError("target root supports neither hardlinks nor relative symlinks") from exc
        raise StorageCapabilityError("relative symlink capability verification failed")
    finally:
        _unlink_if_present(symlink)
        _unlink_if_present(hardlink)
        _unlink_if_present(source)
        try:
            probe.rmdir()
        except FileNotFoundError:
            pass


def _planned_target(root: Path, planned: str) -> Path:
    if not isinstance(planned, str) or not planned:
        raise ValueError("planned_local_path must be a nonempty relative path")
    pure = PurePosixPath(planned)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("planned_local_path must be a safe relative path")
    target = root.joinpath(*pure.parts)
    _assert_safe_write_target(root, target, "planned_local_path")
    return target


def _stored_path(root: Path, stored: StoredObject) -> Path:
    if root.is_symlink():
        raise ValueError("library root is a symlink")
    expected = object_path_for_hash(root, stored.sha256)
    expected_relative = expected.relative_to(root).as_posix()
    if stored.object_path != expected_relative:
        raise ValueError("stored object path is not canonical")
    path = expected
    _assert_safe_write_target(root, path, "stored object path")
    if path.is_symlink() or not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("stored object must be a regular file, not a symlink")
    if path.stat().st_size != stored.size:
        raise ValueError("stored object size mismatch")
    if hash_file_sha256(path) != stored.sha256:
        raise ValueError("stored object hash mismatch")
    _validate_pdf(path)
    return path


def _existing_method(target: Path, object_path: Path) -> StorageMethod | None:
    if target.is_symlink():
        link_text = os.readlink(target)
        if os.path.isabs(link_text):
            return None
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return StorageMethod.RELATIVE_SYMLINK if resolved == object_path.resolve() else None
    if target.exists() and target.is_file():
        try:
            if target.stat().st_dev == object_path.stat().st_dev and target.stat().st_ino == object_path.stat().st_ino:
                return StorageMethod.HARDLINK
        except OSError:
            return None
    return None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quarantine_identity(path: Path) -> tuple[int, str]:
    if path.is_symlink():
        content = os.readlink(path).encode("utf-8")
        return len(content), hashlib.sha256(content).hexdigest()
    return path.stat().st_size, hash_file_sha256(path)


def _path_items(root: Path, run_id: str) -> list[object]:
    target = root / "quarantine/manifests" / f"paths-{run_id}.json"
    if not target.exists():
        return []
    payload = read_json(target)
    if set(payload) != {"schema_version", "model_type", "items"}:
        raise ValueError("invalid PathQuarantineManifest envelope")
    if payload["schema_version"] != 1 or payload["model_type"] != "PathQuarantineManifest":
        raise ValueError("invalid PathQuarantineManifest identity")
    if not isinstance(payload["items"], list):
        raise TypeError("PathQuarantineManifest items must be an array")
    for item in payload["items"]:
        valid = (
            isinstance(item, dict)
            and set(item) == _PATH_ITEM_KEYS
            and all(isinstance(item[key], str) and item[key] and ".." not in Path(item[key]).parts and not Path(item[key]).is_absolute() for key in ("original_path", "quarantine_path"))
            and item["reason"] == "occupied_path_content_mismatch"
            and type(item["size"]) is int
            and item["size"] >= 0
            and isinstance(item["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None
        )
        if not valid:
            raise ValueError("invalid PathQuarantineManifest item")
    return payload["items"]


def _append_path_manifest(root: Path, run_id: str, item: dict[str, object]) -> None:
    target = root / "quarantine/manifests" / f"paths-{run_id}.json"
    if set(item) != _PATH_ITEM_KEYS:
        raise ValueError("invalid PathQuarantineManifest item")
    items = _path_items(root, run_id)
    items.append(item)
    atomic_write_json(
        target,
        {"schema_version": 1, "model_type": "PathQuarantineManifest", "items": items},
    )


def _quarantine_occupied(root: Path, target: Path, run_id: str) -> None:
    manifest = root / "quarantine/manifests" / f"paths-{run_id}.json"
    directory = root / "quarantine/paths" / run_id
    _assert_safe_write_target(root, manifest, "path quarantine manifest")
    _assert_safe_write_target(root, directory / target.name, "path quarantine path")
    _path_items(root, run_id)
    size, digest = _quarantine_identity(target)
    directory.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(target.relative_to(root).as_posix().encode("utf-8")).hexdigest()[:12]
    quarantine = directory / f"{token}-{target.name}"
    counter = 1
    while quarantine.exists() or quarantine.is_symlink():
        quarantine = directory / f"{token}-{counter}-{target.name}"
        counter += 1
    os.replace(target, quarantine)
    _fsync_directory(target.parent)
    _fsync_directory(directory)
    _append_path_manifest(
        root,
        run_id,
        {
            "original_path": target.relative_to(root).as_posix(),
            "quarantine_path": quarantine.relative_to(root).as_posix(),
            "reason": "occupied_path_content_mismatch",
            "size": size,
            "sha256": digest,
        },
    )


def _relative_symlink(object_path: Path, target: Path, root: Path) -> None:
    relative_target = os.path.relpath(object_path, target.parent)
    if os.path.isabs(relative_target):
        raise StorageCapabilityError("symlink fallback must be relative")
    try:
        os.symlink(relative_target, target)
        if not _inside(root, target) or target.resolve() != object_path.resolve():
            raise StorageCapabilityError("symlink fallback escaped library root")
    except Exception:
        _unlink_if_present(target)
        raise


def _create_parent_directories(root: Path, parent: Path) -> list[Path]:
    _assert_safe_write_target(root, parent / ".materialize", "category path")
    missing: list[Path] = []
    current = parent
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    created: list[Path] = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
    except Exception:
        for directory in reversed(created):
            directory.rmdir()
        raise
    return created


def _cleanup_uncommitted(target: Path, created: list[Path]) -> None:
    _unlink_if_present(target)
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError:
            break


def materialize_membership(
    root: Path,
    membership: Membership,
    stored: StoredObject,
    *,
    run_id: str | None = None,
) -> MaterializationResult:
    root = Path(root)
    if run_id is not None:
        run_id = _safe_run_id(run_id)
    object_path = _stored_path(root, stored)
    target = _planned_target(root, membership.planned_local_path)
    existing = _existing_method(target, object_path)
    if existing is not None:
        return MaterializationResult(membership.membership_id, membership.planned_local_path, existing, stored.sha256)
    if target.exists() or target.is_symlink():
        if run_id is None:
            raise MaterializationConflictError("run_id is required to quarantine an occupied category path")
        _quarantine_occupied(root, target, run_id)
        raise MaterializationConflictError(f"occupied category path quarantined: {membership.planned_local_path}")

    capability = probe_storage_capability(root)
    created = _create_parent_directories(root, target.parent)
    try:
        if capability is StorageMethod.HARDLINK:
            try:
                os.link(object_path, target)
                if target.stat().st_dev != object_path.stat().st_dev or target.stat().st_ino != object_path.stat().st_ino:
                    raise StorageCapabilityError("unable to materialize a verified hardlink")
                method = StorageMethod.HARDLINK
            except OSError:
                _unlink_if_present(target)
                _relative_symlink(object_path, target, root)
                method = StorageMethod.RELATIVE_SYMLINK
        else:
            _relative_symlink(object_path, target, root)
            method = StorageMethod.RELATIVE_SYMLINK
        _fsync_directory(target.parent)
    except Exception as exc:
        _cleanup_uncommitted(target, created)
        if isinstance(exc, StorageCapabilityError):
            raise
        raise StorageCapabilityError("unable to materialize category path") from exc
    return MaterializationResult(membership.membership_id, membership.planned_local_path, method, stored.sha256)
