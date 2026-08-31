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
from .storage import _file_lock, _run_lock_path, _validate_pdf, hash_file_sha256, object_path_for_hash


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
    if target.is_symlink():
        raise ValueError("PathQuarantineManifest target cannot be a symlink")
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
    with _file_lock(root, _run_lock_path(root, run_id), "run manifest lock"):
        items = _path_items(root, run_id)
        if item not in items:
            items.append(item)
            atomic_write_json(
                target,
                {"schema_version": 1, "model_type": "PathQuarantineManifest", "items": items},
            )


_PATH_INTENT_KEYS = {
    "schema_version",
    "model_type",
    "run_id",
    "original_path",
    "quarantine_path",
    "reason",
    "size",
    "sha256",
}


def _path_intent_paths(root: Path, target: Path, run_id: str) -> tuple[Path, Path]:
    token = hashlib.sha256(target.relative_to(root).as_posix().encode("utf-8")).hexdigest()[:16]
    quarantine = root / "quarantine/paths" / run_id / f"{token}-{target.name}"
    intent = root / "quarantine/transactions" / f"path-{run_id}-{token}.json"
    return intent, quarantine


def _path_intent_payload(root: Path, target: Path, quarantine: Path, run_id: str, size: int, digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_type": "PathQuarantineIntent",
        "run_id": run_id,
        "original_path": target.relative_to(root).as_posix(),
        "quarantine_path": quarantine.relative_to(root).as_posix(),
        "reason": "occupied_path_content_mismatch",
        "size": size,
        "sha256": digest,
    }


def _reconcile_path_intent(root: Path, target: Path, run_id: str) -> bool:
    intent, quarantine = _path_intent_paths(root, target, run_id)
    if not intent.exists() and not intent.is_symlink():
        return False
    _assert_safe_write_target(root, intent, "path quarantine transaction")
    _assert_safe_write_target(root, quarantine, "path quarantine path")
    if intent.is_symlink():
        raise ValueError("path quarantine transaction cannot be a symlink")
    raw = read_json(intent)
    if not isinstance(raw.get("size"), int) or not isinstance(raw.get("sha256"), str):
        raise ValueError("invalid PathQuarantineIntent")
    expected = _path_intent_payload(root, target, quarantine, run_id, raw["size"], raw["sha256"])
    if (
        set(raw) != _PATH_INTENT_KEYS
        or raw != expected
        or type(raw["size"]) is not int
        or raw["size"] < 0
        or not isinstance(raw["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) is None
    ):
        raise ValueError("invalid PathQuarantineIntent")
    size, digest = raw["size"], raw["sha256"]
    if quarantine.exists() or quarantine.is_symlink():
        if _quarantine_identity(quarantine) != (size, digest):
            raise ValueError("path quarantine transaction destination mismatch")
    else:
        if not target.exists() and not target.is_symlink():
            raise ValueError("path quarantine transaction source is missing")
        if _quarantine_identity(target) != (size, digest):
            raise ValueError("path quarantine transaction source mismatch")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, quarantine)
        _fsync_directory(target.parent)
        _fsync_directory(quarantine.parent)
    item = {
        "original_path": raw["original_path"],
        "quarantine_path": raw["quarantine_path"],
        "reason": raw["reason"],
        "size": size,
        "sha256": digest,
    }
    _append_path_manifest(root, run_id, item)
    intent.unlink()
    _fsync_directory(intent.parent)
    return True


def _quarantine_occupied(root: Path, target: Path, run_id: str) -> None:
    intent, quarantine = _path_intent_paths(root, target, run_id)
    _assert_safe_write_target(root, intent, "path quarantine transaction")
    _assert_safe_write_target(root, quarantine, "path quarantine path")
    with _file_lock(root, _run_lock_path(root, run_id), "run manifest lock"):
        _path_items(root, run_id)
    size, digest = _quarantine_identity(target)
    payload = _path_intent_payload(root, target, quarantine, run_id, size, digest)
    if intent.exists() or intent.is_symlink():
        if intent.is_symlink() or read_json(intent) != payload:
            raise ValueError("invalid PathQuarantineIntent")
    else:
        atomic_write_json(intent, payload)
    _reconcile_path_intent(root, target, run_id)


def _relative_symlink(object_path: Path, target: Path, root: Path) -> None:
    relative_target = os.path.relpath(object_path, target.parent)
    if os.path.isabs(relative_target):
        raise StorageCapabilityError("symlink fallback must be relative")
    created = False
    try:
        os.symlink(relative_target, target)
        created = True
        if not _inside(root, target) or target.resolve() != object_path.resolve():
            raise StorageCapabilityError("symlink fallback escaped library root")
    except FileExistsError:
        raise
    except Exception:
        if created or target.exists() or target.is_symlink():
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
            try:
                directory.mkdir()
                created.append(directory)
            except FileExistsError:
                if directory.is_symlink() or not directory.is_dir():
                    raise ValueError("category path parent changed during creation")
    except Exception:
        for directory in reversed(created):
            directory.rmdir()
        raise
    return created


def _cleanup_created_directories(created: list[Path]) -> None:
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError:
            break


def _existing_or_conflict(
    root: Path,
    target: Path,
    object_path: Path,
    membership: Membership,
    stored: StoredObject,
    run_id: str | None,
) -> MaterializationResult | None:
    existing = _existing_method(target, object_path)
    if existing is not None:
        return MaterializationResult(membership.membership_id, membership.planned_local_path, existing, stored.sha256)
    if target.exists() or target.is_symlink():
        if run_id is None:
            raise MaterializationConflictError("run_id is required to quarantine an occupied category path")
        _quarantine_occupied(root, target, run_id)
        raise MaterializationConflictError(f"occupied category path quarantined: {membership.planned_local_path}")
    return None


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
    lock_identity = target.relative_to(root).as_posix()
    lock_hash = hashlib.sha256(lock_identity.encode("utf-8")).hexdigest()
    lock_path = root / "metadata/operations/locks" / f"materialize-{lock_hash}.lock"
    with _file_lock(root, lock_path, "materialization target lock"):
        if run_id is not None and _reconcile_path_intent(root, target, run_id):
            raise MaterializationConflictError(f"occupied category path quarantined: {membership.planned_local_path}")
        existing = _existing_or_conflict(root, target, object_path, membership, stored, run_id)
        if existing is not None:
            return existing
        capability = probe_storage_capability(root)
        created = _create_parent_directories(root, target.parent)
        created_target = False
        try:
            if capability is StorageMethod.HARDLINK:
                try:
                    os.link(object_path, target)
                    created_target = True
                    if target.stat().st_dev != object_path.stat().st_dev or target.stat().st_ino != object_path.stat().st_ino:
                        raise StorageCapabilityError("unable to materialize a verified hardlink")
                    method = StorageMethod.HARDLINK
                except FileExistsError:
                    existing = _existing_or_conflict(root, target, object_path, membership, stored, run_id)
                    if existing is None:
                        raise StorageCapabilityError("materialization target disappeared after EEXIST")
                    _cleanup_created_directories(created)
                    return existing
                except OSError:
                    if target.exists() or target.is_symlink():
                        _unlink_if_present(target)
                    _relative_symlink(object_path, target, root)
                    created_target = True
                    method = StorageMethod.RELATIVE_SYMLINK
            else:
                try:
                    _relative_symlink(object_path, target, root)
                    created_target = True
                    method = StorageMethod.RELATIVE_SYMLINK
                except FileExistsError:
                    existing = _existing_or_conflict(root, target, object_path, membership, stored, run_id)
                    if existing is None:
                        raise StorageCapabilityError("materialization target disappeared after EEXIST")
                    _cleanup_created_directories(created)
                    return existing
            _fsync_directory(target.parent)
        except Exception as exc:
            if created_target:
                _unlink_if_present(target)
            _cleanup_created_directories(created)
            if isinstance(exc, (StorageCapabilityError, MaterializationConflictError)):
                raise
            raise StorageCapabilityError("unable to materialize category path") from exc
        return MaterializationResult(membership.membership_id, membership.planned_local_path, method, stored.sha256)
