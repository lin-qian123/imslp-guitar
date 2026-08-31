from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from dataclasses import dataclass
from pathlib import Path
from typing import Pattern

from .enums import CategoryKind


class ConfigError(ValueError):
    """A deterministic, code-prefixed configuration validation error."""


TOP_LEVEL_KEYS = {"schema_version", "version", "approved_at", "categories"}
CATEGORY_KEYS = {
    "name", "url", "kind", "display_group", "guitar_count", "extended_strings",
    "instrumentation_patterns", "heading_patterns", "annotation_reject_tokens",
}
REJECTED_CATEGORY_TERMS = ("electric", "bass")


def _error(code: str, detail: str = "") -> ConfigError:
    return ConfigError(f"{code}: {detail}".rstrip())


def _compile(pattern: str) -> Pattern[str]:
    if not isinstance(pattern, str) or not pattern.startswith("^") or not pattern.endswith("$"):
        raise _error("invalid_pattern", "patterns must be anchored with ^ and $")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise _error("invalid_regex", str(exc)) from exc


@dataclass(frozen=True)
class CategoryConfig:
    name: str
    url: str
    kind: CategoryKind
    display_group: str
    guitar_count: int | tuple[int, int] | str
    extended_strings: int | None
    instrumentation_patterns: tuple[Pattern[str], ...]
    heading_patterns: tuple[Pattern[str], ...]
    annotation_reject_tokens: frozenset[str]

    def matches_instrumentation(self, text: str) -> bool:
        return any(pattern.fullmatch(text.strip()) is not None for pattern in self.instrumentation_patterns)

    def matches_heading(self, text: str) -> bool:
        for pattern in self.heading_patterns:
            match = pattern.fullmatch(text.strip())
            if match is None:
                continue
            annotation = match.groupdict().get("annotation")
            if annotation is None:
                return True
            tokens = {token.casefold() for token in re.findall(r"\w+", annotation, flags=re.UNICODE)}
            return not bool(tokens & self.annotation_reject_tokens)
        return False


@dataclass(frozen=True)
class LibraryConfig:
    schema_version: int
    version: str
    approved_at: str
    categories: tuple[CategoryConfig, ...]
    config_hash: str
    canonical_json: str


def _strict_keys(value: dict[str, object], expected: set[str], scope: str) -> None:
    unknown, missing = set(value) - expected, expected - set(value)
    if unknown:
        raise _error("unknown_key", f"{scope}: {sorted(unknown)[0]}")
    if missing:
        raise _error("missing_key", f"{scope}: {sorted(missing)[0]}")


def _guitar_count(value: object) -> int | tuple[int, int] | str:
    if type(value) is int and 1 <= value <= 16:
        return value
    if value == "ensemble":
        return value
    if isinstance(value, list) and value == [2, 3]:
        return (2, 3)
    raise _error("invalid_guitar_count")


def _category(value: object) -> CategoryConfig:
    if not isinstance(value, dict):
        raise _error("invalid_category")
    _strict_keys(value, CATEGORY_KEYS, "category")
    name = value["name"]
    if not isinstance(name, str) or not name.strip():
        raise _error("invalid_name")
    if any(term in name.casefold() for term in REJECTED_CATEGORY_TERMS):
        raise _error("excluded_instrument", name)
    expected_url = "https://imslp.org/wiki/Category:" + name.replace(" ", "_")
    if value["url"] != expected_url:
        raise _error("url_name_mismatch", name)
    try:
        kind = CategoryKind(value["kind"])
    except (ValueError, TypeError) as exc:
        raise _error("invalid_kind") from exc
    raw_instrumentation = value["instrumentation_patterns"]
    raw_headings = value["heading_patterns"]
    if not isinstance(raw_instrumentation, list) or not raw_instrumentation:
        raise _error("missing_instrumentation_patterns")
    if kind is CategoryKind.ARRANGEMENT and (not isinstance(raw_headings, list) or not raw_headings):
        raise _error("missing_heading_patterns")
    if kind is CategoryKind.ORIGINAL and raw_headings != []:
        raise _error("invalid_heading_patterns")
    if not isinstance(raw_headings, list):
        raise _error("invalid_heading_patterns")
    reject_tokens = value["annotation_reject_tokens"]
    if not isinstance(reject_tokens, list) or not reject_tokens or not all(isinstance(token, str) and token.strip() for token in reject_tokens):
        raise _error("invalid_annotation_reject_tokens")
    if len({token.strip().casefold() for token in reject_tokens}) != len(reject_tokens):
        raise _error("duplicate_annotation_token")
    if not isinstance(value["display_group"], str) or not value["display_group"].strip():
        raise _error("invalid_display_group")
    extended = value["extended_strings"]
    if extended is not None and (type(extended) is not int or extended < 1):
        raise _error("invalid_extended_strings")
    count = _guitar_count(value["guitar_count"])
    if value["display_group"] == "extended_solo":
        if extended is None or count != 1:
            raise _error("invalid_extended_solo")
    elif extended is not None:
        raise _error("unexpected_extended_strings")
    return CategoryConfig(
        name=name, url=expected_url, kind=kind, display_group=value["display_group"],
        guitar_count=count, extended_strings=extended,
        instrumentation_patterns=tuple(_compile(pattern) for pattern in raw_instrumentation),
        heading_patterns=tuple(_compile(pattern) for pattern in raw_headings),
        annotation_reject_tokens=frozenset(token.strip().casefold() for token in reject_tokens),
    )


def _library_config_from_payload(payload: object) -> LibraryConfig:
    if not isinstance(payload, dict):
        raise _error("invalid_config")
    _strict_keys(payload, TOP_LEVEL_KEYS, "top-level")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise _error("invalid_metadata")
    if not isinstance(payload["version"], str) or not payload["version"].strip():
        raise _error("invalid_version")
    if not isinstance(payload["approved_at"], str):
        raise _error("invalid_approved_at")
    try:
        date.fromisoformat(payload["approved_at"])
    except ValueError as exc:
        raise _error("invalid_approved_at") from exc
    raw_categories = payload["categories"]
    if not isinstance(raw_categories, list) or not raw_categories:
        raise _error("invalid_categories")
    categories = tuple(_category(item) for item in raw_categories)
    names = [category.name for category in categories]
    if len(names) != len(set(names)):
        raise _error("duplicate_name")
    canonical_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    normalized = canonical_json.encode("utf-8")
    return LibraryConfig(
        payload["schema_version"],
        payload["version"],
        payload["approved_at"],
        categories,
        hashlib.sha256(normalized).hexdigest(),
        canonical_json,
    )


def validate_library_config_binding(config: LibraryConfig) -> None:
    """Verify that compiled rules still equal the exact JSON loaded as their source."""

    if not isinstance(config, LibraryConfig):
        raise TypeError("config must be a LibraryConfig")
    try:
        payload = json.loads(config.canonical_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _error("config_binding_mismatch") from exc
    canonical_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if canonical_json != config.canonical_json:
        raise _error("config_binding_mismatch")
    try:
        rebound = _library_config_from_payload(payload)
    except (ConfigError, TypeError, ValueError) as exc:
        raise _error("config_binding_mismatch") from exc
    if (
        rebound.schema_version != config.schema_version
        or rebound.version != config.version
        or rebound.approved_at != config.approved_at
        or rebound.categories != config.categories
        or rebound.config_hash != config.config_hash
    ):
        raise _error("config_binding_mismatch")


def load_allowlist(path: Path) -> LibraryConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _library_config_from_payload(payload)
