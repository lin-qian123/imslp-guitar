from __future__ import annotations

import hashlib
import fcntl
import os
import re
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from .jsonio import atomic_write_json, read_json
from .models import ScoreFile, StoredObject
from .paths import _assert_safe_write_target

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_KEYS = {
    "source_id",
    "part_path",
    "bytes_written",
    "status",
    "error",
    "attempted_at",
}


@contextmanager
def _file_lock(root: Path, path: Path, label: str):
    _assert_safe_write_target(root, path, label)
    if path.is_symlink():
        raise ValueError(f"{label} cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_write_target(root, path, label)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValueError(f"cannot open safe {label}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size != 0:
            raise ValueError(f"{label} must be an empty singly linked regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _run_lock_path(root: Path, run_id: str) -> Path:
    return root / "metadata/runs/.locks" / f"run-{run_id}.lock"


def hash_file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_file_sha1(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_path_for_hash(root: Path, sha256: str) -> Path:
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")
    return root / "objects" / sha256[:2] / f"{sha256}.pdf"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_to_part(source_path: Path, part_path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    with source_path.open("rb") as source:
        descriptor = os.open(part_path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())


def _validate_pdf(path: Path) -> None:
    with path.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise ValueError("source is not a PDF")
    try:
        reader = PdfReader(path)
        if not reader.pages:
            raise ValueError("PDF has no pages")
        _ = reader.pages[0].mediabox
    except Exception as exc:
        raise ValueError("PDF structure is invalid") from exc


def _validate_source(path: Path, score: ScoreFile) -> tuple[int, str]:
    if not path.is_file():
        raise ValueError("source PDF must be a regular file")
    size = path.stat().st_size
    if score.expected_size is not None and size != score.expected_size:
        raise ValueError("source PDF size does not match ScoreFile")
    if score.sha1_imslp is not None and _hash_file_sha1(path) != score.sha1_imslp:
        raise ValueError("source PDF SHA-1 does not match IMSLP metadata")
    _validate_pdf(path)
    return size, hash_file_sha256(path)


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _validate_attempt_item(item: dict[str, object]) -> None:
    attempted_at = item["attempted_at"]
    try:
        parsed = datetime.fromisoformat(attempted_at) if isinstance(attempted_at, str) else None
    except ValueError:
        parsed = None
    valid = (
        isinstance(item["source_id"], str)
        and re.fullmatch(r"source:f[1-9][0-9]*@r(?:0|[1-9][0-9]*)", item["source_id"]) is not None
        and _safe_relative(item["part_path"])
        and type(item["bytes_written"]) is int
        and item["bytes_written"] >= 0
        and item["status"] in {"object_write_interrupted", "object_write_complete"}
        and (item["error"] is None or isinstance(item["error"], str))
        and parsed is not None
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
    )
    if not valid:
        raise ValueError("invalid ObjectWriteAttemptManifest item")


def _validate_quarantine_item(item: dict[str, object]) -> None:
    valid = (
        _safe_relative(item["original_path"])
        and _safe_relative(item["quarantine_path"])
        and item["reason"] == "object_hash_mismatch"
        and type(item["size"]) is int
        and item["size"] >= 0
        and isinstance(item["sha256"], str)
        and _SHA256_RE.fullmatch(item["sha256"]) is not None
    )
    if not valid:
        raise ValueError("invalid ObjectQuarantineManifest item")


def _read_envelope(
    path: Path,
    model_type: str,
    item_keys: set[str],
    validator: Callable[[dict[str, object]], None],
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = read_json(path)
    if set(payload) != {"schema_version", "model_type", "items"}:
        raise ValueError(f"invalid {model_type} envelope keys")
    if payload["schema_version"] != 1 or payload["model_type"] != model_type:
        raise ValueError(f"invalid {model_type} envelope identity")
    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        raise TypeError(f"{model_type} items must be an array")
    items: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != item_keys:
            raise ValueError(f"invalid {model_type} item")
        validator(raw)
        items.append(raw)
    return items


def _append_manifest(
    path: Path,
    model_type: str,
    item: dict[str, object],
    item_keys: set[str],
    validator: Callable[[dict[str, object]], None],
) -> None:
    if path.is_symlink():
        raise ValueError(f"{model_type} target cannot be a symlink")
    if set(item) != item_keys:
        raise ValueError(f"invalid {model_type} item")
    validator(item)
    items = _read_envelope(path, model_type, item_keys, validator)
    if item in items:
        return
    items.append(item)
    atomic_write_json(path, {"schema_version": 1, "model_type": model_type, "items": items})


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("storage path must be inside library root") from exc


def _attempt_path(root: Path, run_id: str) -> Path:
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None
        or run_id in {".", ".."}
    ):
        raise ValueError("run_id must be a safe nonempty path component")
    return root / "metadata/runs" / f"{run_id}-object-writes.json"


def _record_attempt(
    root: Path,
    run_id: str,
    score: ScoreFile,
    part: Path,
    status: str,
    error: str | None,
    bytes_written: int | None = None,
) -> None:
    recorded_bytes = bytes_written or 0
    if bytes_written is None and part.exists() and not part.is_symlink():
        details = part.lstat()
        recorded_bytes = details.st_size if stat.S_ISREG(details.st_mode) else 0
    item: dict[str, object] = {
        "source_id": score.source_id,
        "part_path": _relative(root, part),
        "bytes_written": recorded_bytes,
        "status": status,
        "error": error,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }
    attempt_path = _attempt_path(root, run_id)
    _assert_safe_write_target(root, attempt_path, "object attempt manifest")
    with _file_lock(root, _run_lock_path(root, run_id), "run manifest lock"):
        _append_manifest(
            attempt_path,
            "ObjectWriteAttemptManifest",
            item,
            _ATTEMPT_KEYS,
            _validate_attempt_item,
        )


def _quarantine_corrupt_object(root: Path, run_id: str, target: Path) -> None:
    old_size = target.stat().st_size
    old_hash = hash_file_sha256(target)
    intent, quarantine = _object_intent_paths(root, run_id, target)
    _assert_safe_write_target(root, intent, "object quarantine transaction")
    _assert_safe_write_target(root, quarantine, "object quarantine path")
    manifest = root / "quarantine/manifests" / f"objects-{run_id}.json"
    item_keys = {"original_path", "quarantine_path", "reason", "size", "sha256"}
    with _file_lock(root, _run_lock_path(root, run_id), "run manifest lock"):
        if manifest.is_symlink():
            raise ValueError("ObjectQuarantineManifest target cannot be a symlink")
        _read_envelope(manifest, "ObjectQuarantineManifest", item_keys, _validate_quarantine_item)
    payload = _object_intent_payload(root, run_id, target, quarantine, old_size, old_hash)
    if intent.exists() or intent.is_symlink():
        _validate_object_intent(intent, payload)
    else:
        atomic_write_json(intent, payload)
    _reconcile_object_intent(root, run_id, target)


def _part_path(target: Path, score: ScoreFile, run_id: str) -> Path:
    identity = hashlib.sha256(f"{run_id}\0{score.source_id}".encode("utf-8")).hexdigest()[:16]
    return target.with_name(f".{target.name}.{identity}.part")


def _read_attempts(root: Path, run_id: str) -> list[dict[str, object]]:
    path = _attempt_path(root, run_id)
    with _file_lock(root, _run_lock_path(root, run_id), "run manifest lock"):
        if path.is_symlink():
            raise ValueError("ObjectWriteAttemptManifest target cannot be a symlink")
        return _read_envelope(path, "ObjectWriteAttemptManifest", _ATTEMPT_KEYS, _validate_attempt_item)


def _part_relative(root: Path, part: Path) -> str:
    return _relative(root, part)


def _validate_owned_part(part: Path) -> os.stat_result:
    details = part.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("object part must be a regular file")
    if details.st_nlink != 1:
        raise ValueError("object part must have link count 1")
    return details


def _prepare_part(root: Path, part: Path, score: ScoreFile, attempts: list[dict[str, object]]) -> None:
    if not part.exists() and not part.is_symlink():
        return
    details = _validate_owned_part(part)
    relative = _part_relative(root, part)
    authorized = any(
        item["source_id"] == score.source_id
        and item["part_path"] == relative
        and item["bytes_written"] == details.st_size
        and item["status"] == "object_write_interrupted"
        for item in attempts
    )
    if not authorized:
        raise ValueError("unowned part cannot be replaced")
    part.unlink()
    _fsync_directory(part.parent)


_OBJECT_INTENT_KEYS = {
    "schema_version",
    "model_type",
    "run_id",
    "original_path",
    "quarantine_path",
    "reason",
    "size",
    "sha256",
}


def _object_intent_paths(root: Path, run_id: str, target: Path) -> tuple[Path, Path]:
    quarantine = root / "quarantine/objects" / run_id / target.name
    intent = root / "quarantine/transactions" / f"object-{run_id}-{target.stem}.json"
    return intent, quarantine


def _validate_object_intent(path: Path, expected: dict[str, object]) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("object quarantine transaction cannot be a symlink")
    payload = read_json(path)
    valid = (
        set(payload) == _OBJECT_INTENT_KEYS
        and payload == expected
        and payload["schema_version"] == 1
        and payload["model_type"] == "ObjectQuarantineIntent"
        and type(payload["size"]) is int
        and payload["size"] >= 0
        and isinstance(payload["sha256"], str)
        and _SHA256_RE.fullmatch(payload["sha256"]) is not None
    )
    if not valid:
        raise ValueError("invalid ObjectQuarantineIntent")
    return payload


def _object_intent_payload(root: Path, run_id: str, target: Path, quarantine: Path, size: int, digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_type": "ObjectQuarantineIntent",
        "run_id": run_id,
        "original_path": _relative(root, target),
        "quarantine_path": _relative(root, quarantine),
        "reason": "object_hash_mismatch",
        "size": size,
        "sha256": digest,
    }


def _reconcile_object_intent(root: Path, run_id: str, target: Path) -> bool:
    intent, quarantine = _object_intent_paths(root, run_id, target)
    if not intent.exists() and not intent.is_symlink():
        return False
    _assert_safe_write_target(root, intent, "object quarantine transaction")
    _assert_safe_write_target(root, quarantine, "object quarantine path")
    if intent.is_symlink():
        raise ValueError("object quarantine transaction cannot be a symlink")
    raw = read_json(intent)
    if not isinstance(raw.get("size"), int) or not isinstance(raw.get("sha256"), str):
        raise ValueError("invalid ObjectQuarantineIntent")
    expected = _object_intent_payload(root, run_id, target, quarantine, raw["size"], raw["sha256"])
    payload = _validate_object_intent(intent, expected)
    size = payload["size"]
    digest = payload["sha256"]
    if quarantine.exists():
        if quarantine.is_symlink() or quarantine.stat().st_size != size or hash_file_sha256(quarantine) != digest:
            raise ValueError("object quarantine transaction destination mismatch")
    else:
        if not target.exists() or target.is_symlink() or target.stat().st_size != size or hash_file_sha256(target) != digest:
            raise ValueError("object quarantine transaction source mismatch")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, quarantine)
        _fsync_directory(target.parent)
        _fsync_directory(quarantine.parent)
    item = {
        "original_path": payload["original_path"],
        "quarantine_path": payload["quarantine_path"],
        "reason": payload["reason"],
        "size": size,
        "sha256": digest,
    }
    manifest = root / "quarantine/manifests" / f"objects-{run_id}.json"
    item_keys = {"original_path", "quarantine_path", "reason", "size", "sha256"}
    with _file_lock(root, _run_lock_path(root, run_id), "run manifest lock"):
        _append_manifest(manifest, "ObjectQuarantineManifest", item, item_keys, _validate_quarantine_item)
    intent.unlink()
    _fsync_directory(intent.parent)
    return True


def store_verified_pdf(root: Path, source: Path, score: ScoreFile, run_id: str) -> StoredObject:
    root = Path(root)
    source = Path(source)
    attempt_manifest = _attempt_path(root, run_id)
    size, source_hash = _validate_source(source, score)
    target = object_path_for_hash(root, source_hash)
    _assert_safe_write_target(root, attempt_manifest, "object attempt manifest")
    _assert_safe_write_target(root, target, "object path")
    lock_path = root / "objects/.locks" / f"{source_hash}.lock"
    with _file_lock(root, lock_path, "object hash lock"):
        _reconcile_object_intent(root, run_id, target)
        attempts = _read_attempts(root, run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("object address must be a regular file")
        part = _part_path(target, score, run_id)
        _prepare_part(root, part, score, attempts)
        part_relative = _part_relative(root, part)
        if target.exists():
            if hash_file_sha256(target) == source_hash:
                _validate_pdf(target)
                complete = any(
                    item["source_id"] == score.source_id
                    and item["part_path"] == part_relative
                    and item["bytes_written"] == size
                    and item["status"] == "object_write_complete"
                    for item in attempts
                )
                if not complete:
                    _record_attempt(root, run_id, score, part, "object_write_complete", None, size)
                return StoredObject(source_hash, size, _relative(root, target), score.sha1_imslp, datetime.now(timezone.utc))
            _quarantine_corrupt_object(root, run_id, target)
        try:
            _copy_to_part(source, part)
            details = _validate_owned_part(part)
            if details.st_size != size or hash_file_sha256(part) != source_hash:
                raise ValueError("copied PDF bytes do not match verified source")
            _validate_pdf(part)
            os.replace(part, target)
            _fsync_directory(target.parent)
        except BaseException as exc:
            _record_attempt(root, run_id, score, part, "object_write_interrupted", str(exc))
            raise
        _record_attempt(root, run_id, score, part, "object_write_complete", None, size)
        return StoredObject(source_hash, size, _relative(root, target), score.sha1_imslp, datetime.now(timezone.utc))
