from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .jsonio import atomic_write_json

MAX_COMPONENT_BYTES = 180
_KINDS = {"composer", "work", "file"}


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
    if not identifier.isascii() or not identifier.isdigit():
        raise ValueError(f"{source.kind} stable_id must contain a numeric identifier")
    return f"__{prefix}{identifier}"


def _preliminary(source: PathSource, cleaned: str) -> str:
    stem, extension = _extension(source, cleaned)
    if not extension:
        return _truncate_utf8(cleaned, MAX_COMPONENT_BYTES).rstrip(" .") or "Untitled"
    stem_limit = MAX_COMPONENT_BYTES - len(extension.encode("utf-8"))
    readable = _truncate_utf8(stem, stem_limit).rstrip(" .") or "Untitled"
    return readable + extension


def build_path_map(sources: list[PathSource] | tuple[PathSource, ...]) -> dict[PathSource, PathMapping]:
    values = tuple(sources)
    if len(set(values)) != len(values):
        raise ValueError("duplicate PathSource")
    cleaned = {source: _clean_component(source.original) for source in values}
    preliminary = {source: _preliminary(source, cleaned[source]) for source in values}
    groups: dict[tuple[str, str], list[PathSource]] = {}
    for source in values:
        groups.setdefault((source.kind, preliminary[source].casefold()), []).append(source)

    result: dict[PathSource, PathMapping] = {}
    for source in values:
        group = groups[(source.kind, preliminary[source].casefold())]
        if len(group) == 1:
            result[source] = PathMapping(preliminary[source], None)
            continue
        full_keys = {cleaned[item].casefold() for item in group}
        reason = "clean_or_casefold_collision" if len(full_keys) == 1 else "truncation_collision"
        stem, extension = _extension(source, cleaned[source])
        suffix = _suffix(source)
        reserved = len((suffix + extension).encode("utf-8"))
        if reserved >= MAX_COMPONENT_BYTES:
            raise ValueError("stable suffix is too long for a path component")
        readable = _truncate_utf8(stem, MAX_COMPONENT_BYTES - reserved).rstrip(" .") or "Untitled"
        mapped = readable + suffix + extension
        if len(mapped.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise AssertionError("mapped path component exceeds byte limit")
        result[source] = PathMapping(mapped, reason)

    mapped_casefold = [(source.kind, result[source].mapped.casefold()) for source in values]
    if len(mapped_casefold) != len(set(mapped_casefold)):
        raise ValueError("stable suffixes did not resolve a path collision")
    return result


def quote_path_components(path: str | Path | PurePosixPath) -> str:
    raw = Path(path).as_posix() if isinstance(path, Path) else str(path)
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise ValueError("URL path must be relative")
    return "/".join(quote(part, safe="") for part in pure.parts)


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
    atomic_write_json(
        target,
        {"schema_version": 1, "model_type": "PathMapManifest", "items": items},
    )
    return target
