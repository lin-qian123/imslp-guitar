from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .jsonio import atomic_write_json, read_json

MAX_COMPONENT_BYTES = 180
_KINDS = {"composer", "work", "file"}


def _assert_safe_write_target(root: Path, target: Path, label: str) -> None:
    if root.is_symlink():
        raise ValueError("library root is a symlink")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the library root") from exc
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} has a symlink ancestor")
        if current.exists() and not current.is_dir():
            raise ValueError(f"{label} ancestor is not a directory")
    try:
        target.parent.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the library root") from exc


@dataclass(frozen=True, slots=True)
class PathSource:
    kind: str
    original: str
    stable_id: str

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unsupported path source kind: {self.kind}")
        if not isinstance(self.original, str):
            raise TypeError("original must be a string")
        if not isinstance(self.stable_id, str) or not self.stable_id.strip():
            raise ValueError("stable_id must be a nonempty string")


@dataclass(frozen=True, slots=True)
class PathMapping:
    mapped: str
    collision_reason: str | None


def _truncate_utf8(value: str, byte_limit: int) -> str:
    if byte_limit < 0:
        raise ValueError("byte limit cannot be negative")
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _clean_component(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    cleaned = "".join("_" if char == "/" or unicodedata.category(char) == "Cc" else char for char in normalized)
    cleaned = cleaned.rstrip(" .")
    return cleaned or "Untitled"


def normalize_component(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("path component must be a string")
    return _truncate_utf8(_clean_component(value), MAX_COMPONENT_BYTES).rstrip(" .") or "Untitled"


def _extension(source: PathSource, cleaned: str) -> tuple[str, str]:
    if source.kind == "file" and cleaned.casefold().endswith(".pdf"):
        return cleaned[:-4], cleaned[-4:]
    return cleaned, ""


def _suffix(source: PathSource) -> str:
    if source.kind == "composer":
        attribution = unicodedata.normalize("NFC", source.original)
        return "__c" + hashlib.sha256(attribution.encode("utf-8")).hexdigest()[:10]
    prefix = "p" if source.kind == "work" else "f"
    identifier = source.stable_id
    if identifier.startswith(prefix):
        identifier = identifier[1:]
    if re.fullmatch(r"[1-9][0-9]*", identifier) is None:
        raise ValueError(f"{source.kind} stable_id must contain a canonical positive numeric decimal identifier")
    return f"__{prefix}{identifier}"


def _preliminary(source: PathSource, cleaned: str) -> str:
    stem, extension = _extension(source, cleaned)
    if not extension:
        return _truncate_utf8(cleaned, MAX_COMPONENT_BYTES).rstrip(" .") or "Untitled"
    stem_limit = MAX_COMPONENT_BYTES - len(extension.encode("utf-8"))
    readable = _truncate_utf8(stem, stem_limit).rstrip(" .") or "Untitled"
    return readable + extension


def _mapped_with_suffix(source: PathSource, cleaned: str) -> str:
    stem, extension = _extension(source, cleaned)
    suffix = _suffix(source)
    reserved = len((suffix + extension).encode("utf-8"))
    if reserved >= MAX_COMPONENT_BYTES:
        raise ValueError("stable suffix is too long for a path component")
    readable = _truncate_utf8(stem, MAX_COMPONENT_BYTES - reserved).rstrip(" .") or "Untitled"
    mapped = readable + suffix + extension
    if len(mapped.encode("utf-8")) > MAX_COMPONENT_BYTES:
        raise AssertionError("mapped path component exceeds byte limit")
    return mapped


def build_path_map(sources: list[PathSource] | tuple[PathSource, ...]) -> dict[PathSource, PathMapping]:
    values = tuple(sources)
    if len(set(values)) != len(values):
        raise ValueError("duplicate PathSource")
    for source in values:
        if source.kind in {"work", "file"}:
            _suffix(source)
    cleaned = {source: _clean_component(source.original) for source in values}
    preliminary = {source: _preliminary(source, cleaned[source]) for source in values}
    groups: dict[tuple[str, str], list[PathSource]] = {}
    for source in values:
        groups.setdefault((source.kind, preliminary[source].casefold()), []).append(source)

    def logical_key(source: PathSource) -> tuple[str, str]:
        if source.kind == "composer":
            return source.kind, unicodedata.normalize("NFC", source.original)
        return source.kind, f"{source.stable_id}\0{source.original}"

    reasons: dict[PathSource, str] = {}
    for group in groups.values():
        if len({logical_key(item) for item in group}) > 1:
            full_keys = {cleaned[item].casefold() for item in group}
            reason = "clean_or_casefold_collision" if len(full_keys) == 1 else "truncation_collision"
            reasons.update((item, reason) for item in group)

    while True:
        mapped = {
            source: _mapped_with_suffix(source, cleaned[source]) if source in reasons else preliminary[source]
            for source in values
        }
        output_groups: dict[tuple[str, str], list[PathSource]] = {}
        for source in values:
            output_groups.setdefault((source.kind, mapped[source].casefold()), []).append(source)
        collisions = [group for group in output_groups.values() if len({logical_key(item) for item in group}) > 1]
        if not collisions:
            return {source: PathMapping(mapped[source], reasons.get(source)) for source in values}
        changed = False
        for group in collisions:
            for source in group:
                if source not in reasons:
                    reasons[source] = "suffix_collision"
                    changed = True
        if not changed:
            raise ValueError("duplicate stable suffix cannot resolve path collision")


def quote_path_components(path: str | Path | PurePosixPath) -> str:
    raw = path.as_posix() if isinstance(path, Path) else str(path)
    components = raw.split("/")
    if (
        not raw
        or raw.startswith("/")
        or "\\" in raw
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError("URL path must be an unambiguous relative POSIX path")
    return "/".join(quote(component, safe="") for component in components)


def _validate_path_map_manifest(payload: dict[str, object]) -> None:
    if set(payload) != {"schema_version", "model_type", "items"}:
        raise ValueError("invalid PathMapManifest envelope")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["model_type"] != "PathMapManifest":
        raise ValueError("invalid PathMapManifest identity")
    items = payload["items"]
    if not isinstance(items, list):
        raise TypeError("PathMapManifest items must be an array")
    keys = {"kind", "stable_id", "original", "mapped", "collision_reason"}
    for item in items:
        valid = (
            isinstance(item, dict)
            and set(item) == keys
            and item["kind"] in _KINDS
            and isinstance(item["original"], str)
            and all(isinstance(item[name], str) and item[name] for name in ("stable_id", "mapped"))
            and item["collision_reason"] in {None, "clean_or_casefold_collision", "truncation_collision", "suffix_collision"}
            and len(item["mapped"].encode("utf-8")) <= MAX_COMPONENT_BYTES
        )
        if not valid:
            raise ValueError("invalid PathMapManifest item")


def write_path_map(root: Path, mappings: dict[PathSource, PathMapping]) -> Path:
    items = [
        {
            "kind": source.kind,
            "stable_id": source.stable_id,
            "original": source.original,
            "mapped": mapping.mapped,
            "collision_reason": mapping.collision_reason,
        }
        for source, mapping in mappings.items()
    ]
    items.sort(key=lambda item: (item["kind"], item["stable_id"], item["original"]))
    target = root / "metadata/path_map.json"
    _assert_safe_write_target(root, target, "path_map target")
    if target.is_symlink():
        raise ValueError("PathMapManifest target cannot be a symlink")
    if target.exists():
        _validate_path_map_manifest(read_json(target))
    payload: dict[str, object] = {"schema_version": 1, "model_type": "PathMapManifest", "items": items}
    _validate_path_map_manifest(payload)
    atomic_write_json(target, payload)
    return target
