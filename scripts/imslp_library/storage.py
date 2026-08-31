from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from .jsonio import atomic_write_json, read_json
from .models import ScoreFile, StoredObject

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_KEYS = {
    "source_id",
    "part_path",
    "bytes_written",
    "status",
    "error",
    "attempted_at",
}


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
    with source_path.open("rb") as source, part_path.open("wb") as target:
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
    if set(item) != item_keys:
        raise ValueError(f"invalid {model_type} item")
    validator(item)
    items = _read_envelope(path, model_type, item_keys, validator)
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
    recorded_bytes = part.stat().st_size if bytes_written is None and part.exists() else (bytes_written or 0)
    item: dict[str, object] = {
        "source_id": score.source_id,
        "part_path": _relative(root, part),
        "bytes_written": recorded_bytes,
        "status": status,
        "error": error,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_manifest(
        _attempt_path(root, run_id),
        "ObjectWriteAttemptManifest",
        item,
        _ATTEMPT_KEYS,
        _validate_attempt_item,
    )


def _unique_quarantine_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{Path(name).stem}-{counter}{Path(name).suffix}"
        counter += 1
    return candidate


def _quarantine_corrupt_object(root: Path, run_id: str, target: Path) -> None:
    old_size = target.stat().st_size
    old_hash = hash_file_sha256(target)
    manifest = root / "quarantine/manifests" / f"objects-{run_id}.json"
    item_keys = {"original_path", "quarantine_path", "reason", "size", "sha256"}
    _read_envelope(manifest, "ObjectQuarantineManifest", item_keys, _validate_quarantine_item)
    directory = root / "quarantine/objects" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    quarantine = _unique_quarantine_path(directory, target.name)
    os.replace(target, quarantine)
    _fsync_directory(directory)
    _fsync_directory(target.parent)
    item: dict[str, object] = {
        "original_path": _relative(root, target),
        "quarantine_path": _relative(root, quarantine),
        "reason": "object_hash_mismatch",
        "size": old_size,
        "sha256": old_hash,
    }
    _append_manifest(
        manifest,
        "ObjectQuarantineManifest",
        item,
        item_keys,
        _validate_quarantine_item,
    )


def _part_path(target: Path, score: ScoreFile, run_id: str) -> Path:
    identity = hashlib.sha256(f"{run_id}\0{score.source_id}".encode("utf-8")).hexdigest()[:16]
    return target.with_name(f".{target.name}.{identity}.part")


def store_verified_pdf(root: Path, source: Path, score: ScoreFile, run_id: str) -> StoredObject:
    root = Path(root)
    source = Path(source)
    attempt_manifest = _attempt_path(root, run_id)
    _read_envelope(attempt_manifest, "ObjectWriteAttemptManifest", _ATTEMPT_KEYS, _validate_attempt_item)
    size, source_hash = _validate_source(source, score)
    target = object_path_for_hash(root, source_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("object path must remain inside library root") from exc
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("object address must be a regular file")
    if target.exists():
        if hash_file_sha256(target) == source_hash:
            _validate_pdf(target)
            return StoredObject(source_hash, size, _relative(root, target), score.sha1_imslp, datetime.now(timezone.utc))
        _quarantine_corrupt_object(root, run_id, target)

    part = _part_path(target, score, run_id)
    try:
        _copy_to_part(source, part)
        if part.stat().st_size != size or hash_file_sha256(part) != source_hash:
            raise ValueError("copied PDF bytes do not match verified source")
        _validate_pdf(part)
        os.replace(part, target)
        _fsync_directory(target.parent)
    except BaseException as exc:
        _record_attempt(root, run_id, score, part, "object_write_interrupted", str(exc))
        raise
    _record_attempt(root, run_id, score, part, "object_write_complete", None, size)
    return StoredObject(source_hash, size, _relative(root, target), score.sha1_imslp, datetime.now(timezone.utc))
