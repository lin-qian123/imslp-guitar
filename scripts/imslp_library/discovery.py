from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .client import AllCategory, Clock, ImslpClient
from .config import ConfigError, LibraryConfig, validate_library_config_binding
from .jsonio import atomic_write_json, read_json
from .models import CategorySnapshot, RunSnapshot, RunState
from .paths import _assert_safe_read_target, _assert_safe_write_target
from .snapshot import SnapshotError, load_complete_snapshot
from .storage import _file_lock, _run_lock_path


_PHASES = {"standalone", "run_start", "run_end"}
_REPORT_KEYS = {
    "schema_version",
    "generated_at",
    "phase",
    "compare_run_id",
    "allowlist_version",
    "allowlist_sha256",
    "new_candidates",
    "empty_categories",
    "deleted_categories",
    "member_count_changes",
    "possible_renames",
    "filtered_candidates",
}
_REPORT_GROUPS = (
    "new_candidates",
    "empty_categories",
    "deleted_categories",
    "member_count_changes",
    "possible_renames",
    "filtered_candidates",
)
_HYPHENS_RE = re.compile(r"[-‐‑‒–—―]+")
_SPACE_RE = re.compile(r"\s+")
_PURE_GUITAR_RE = re.compile(
    r"^For (?:"
    r"guitar(?: ensemble| orchestra)?|"
    r"[1-9][0-9]*(?: and [1-9][0-9]*)? guitars?|"
    r"[1-9][0-9]* string guitar"
    r")(?: \(arr\))?$",
    re.IGNORECASE,
)
_MALFORMED_CHARACTERS = frozenset("?&#=%\\/<>\x00")
_MIXED_MARKERS = (
    ",",
    " and voice",
    " and piano",
    " and violin",
    " and viola",
    " and cello",
    " and strings",
    " and woodwind",
    " and brass",
    " and percussion",
    " and mandolin",
    " and lute",
    " with voice",
    " with piano",
)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    report: dict[str, object]
    report_path: str
    report_sha256: str


def _canonical_bytes(mapping: Mapping[str, object]) -> bytes:
    return (
        json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("discovery path escapes the library root") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(root: Path, path: Path, mapping: Mapping[str, object]) -> None:
    """Create a canonical JSON file without any overwrite window."""

    _assert_safe_write_target(root, path, "immutable category drift report")
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_write_target(root, path, "immutable category drift report")
    content = _canonical_bytes(mapping)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        temporary = None
        _fsync_directory(path.parent)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def _normalized_name(name: str) -> str:
    folded = _HYPHENS_RE.sub(" ", name.casefold())
    return _SPACE_RE.sub(" ", folded).strip()


def _filter_reason(category: AllCategory) -> str | None:
    name = category.name
    folded = name.casefold()
    if (
        any(character in name for character in _MALFORMED_CHARACTERS)
        or any(unicodedata.category(character) == "Cc" for character in name)
        or name != name.strip()
        or not name.startswith("For ")
        or _SPACE_RE.sub(" ", name) != name
    ):
        return "malformed_name"
    if "guitar" not in folded:
        return "not_guitar"
    if "electric guitar" in folded:
        return "electric_guitar"
    if "bass guitar" in folded:
        return "bass_guitar"
    normalized = _normalized_name(name)
    if not _PURE_GUITAR_RE.fullmatch(normalized):
        if any(marker in folded for marker in _MIXED_MARKERS):
            return "mixed_instrumentation"
        return "unsupported_guitar_structure"
    if category.size == 0 or category.page_count == 0:
        return "zero_members"
    return None


def _candidate_item(category: AllCategory) -> dict[str, object]:
    return {
        "name": category.name,
        "size": category.size,
        "page_count": category.page_count,
        "file_count": category.file_count,
        "subcategory_count": category.subcategory_count,
    }


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _jaccard(left: set[int], right: set[int]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _run_paths(root: Path, run_id: str) -> tuple[Path, Path]:
    return (
        root / "metadata/runs" / f"{run_id}.json",
        root / "metadata/runs" / f"{run_id}-state.json",
    )


def _drift_report_path(root: Path, run_id: str, stem: str) -> Path:
    return root / "metadata/runs" / f"{run_id}-category-drift-{stem}.json"


def _read_regular_bytes(root: Path, path: Path, label: str) -> bytes:
    try:
        _assert_safe_read_target(root, path, label)
    except ValueError as exc:
        raise SnapshotError(f"{label} path is unsafe or a symlink") from exc
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SnapshotError(f"{label} is missing, unsafe or a symlink") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise SnapshotError(f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def _validate_drift_report(
    payload: object,
    *,
    run_id: str,
    phase: str,
    config: LibraryConfig,
) -> None:
    if not isinstance(payload, dict) or set(payload) != _REPORT_KEYS:
        raise SnapshotError("drift report has invalid top-level fields")
    generated_at = payload["generated_at"]
    try:
        parsed = datetime.fromisoformat(generated_at) if isinstance(generated_at, str) else None
    except ValueError:
        parsed = None
    valid = (
        type(payload["schema_version"]) is int
        and payload["schema_version"] == 1
        and parsed is not None
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and payload["phase"] == phase
        and payload["compare_run_id"] == run_id
        and payload["allowlist_version"] == config.version
        and payload["allowlist_sha256"] == config.config_hash
        and all(isinstance(payload[group], list) for group in _REPORT_GROUPS)
    )
    if not valid:
        raise SnapshotError("drift report identity or schema is invalid")


def _validate_drift_pointers(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    state: RunState,
) -> None:
    for stem in ("start", "end"):
        relative = getattr(state, f"{stem}_drift_report_path")
        expected_digest = getattr(state, f"{stem}_drift_report_sha256")
        if relative is None:
            continue
        expected_path = _drift_report_path(root, run_id, stem)
        if relative != _relative(root, expected_path):
            raise SnapshotError(f"{stem} drift report path is not canonical")
        content = _read_regular_bytes(root, expected_path, f"{stem} drift report")
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise SnapshotError(f"{stem} drift report SHA-256 mismatch")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"{stem} drift report is not UTF-8 JSON") from exc
        if not isinstance(payload, dict) or _canonical_bytes(payload) != content:
            raise SnapshotError(f"{stem} drift report is not canonical JSON")
        _validate_drift_report(
            payload,
            run_id=run_id,
            phase=f"run_{stem}",
            config=config,
        )


def _load_run_context(
    root: Path, config: LibraryConfig, run_id: str
) -> tuple[RunSnapshot, RunState, Path, bytes]:
    snapshot = load_complete_snapshot(root, run_id)
    snapshot_path, state_path = _run_paths(root, run_id)
    _assert_safe_read_target(root, state_path, "run state")
    if state_path.is_symlink():
        raise SnapshotError("run state cannot be a symlink")
    state = RunState.from_dict(read_json(state_path))
    if snapshot.config_version != config.version or snapshot.config_sha256 != config.config_hash:
        raise ValueError("compare run does not use the supplied allowlist")
    if state.run_id != run_id or state.snapshot_path != _relative(root, snapshot_path):
        raise SnapshotError("run state identity does not match compare run")
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    if state.snapshot_sha256 != snapshot_digest:
        raise SnapshotError("run state snapshot digest does not match immutable snapshot")
    _validate_drift_pointers(root, run_id, config, state)
    return snapshot, state, state_path, snapshot_bytes


def _validate_phase(phase: str, compare_run_id: str | None) -> None:
    if phase not in _PHASES:
        raise ValueError("phase must be standalone, run_start or run_end")
    if phase == "standalone" and compare_run_id is not None:
        raise ValueError("standalone discovery cannot have compare_run_id")
    if phase != "standalone" and compare_run_id is None:
        raise ValueError("run discovery requires compare_run_id")


def _report_path(root: Path, phase: str, compare_run_id: str | None) -> Path:
    if phase == "standalone":
        return root / "metadata/category_drift_report.json"
    suffix = "start" if phase == "run_start" else "end"
    return _drift_report_path(root, compare_run_id, suffix)


def _assert_safe_alias(root: Path) -> Path:
    alias_path = root / "metadata/category_drift_report.json"
    _assert_safe_write_target(root, alias_path, "category drift alias")
    if alias_path.is_symlink():
        raise ValueError("category drift alias cannot be a symlink")
    return alias_path


def _persist_report(
    root: Path,
    report: dict[str, object],
    phase: str,
    compare_run_id: str | None,
    state: RunState | None,
    state_path: Path | None,
    config: LibraryConfig,
    generated_at: datetime,
) -> DiscoveryResult:
    alias_path = _assert_safe_alias(root)
    canonical_path = _report_path(root, phase, compare_run_id)
    if phase == "standalone":
        atomic_write_json(alias_path, report)
    else:
        assert state is not None and state_path is not None and compare_run_id is not None
        pointer = "start_drift_report" if phase == "run_start" else "end_drift_report"
        with _file_lock(root, _run_lock_path(root, compare_run_id), "run manifest lock"):
            if state_path.is_symlink():
                raise SnapshotError("run state cannot be a symlink")
            current_state = RunState.from_dict(read_json(state_path))
            if current_state != state:
                raise SnapshotError("RunState changed while category discovery was running")
            _validate_drift_pointers(root, compare_run_id, config, current_state)
            if getattr(current_state, f"{pointer}_path") is not None:
                raise FileExistsError(f"{pointer} is already recorded")
            _write_immutable_json(root, canonical_path, report)
            # The mutable alias is deliberately later than the fsynced immutable report.
            _assert_safe_alias(root)
            atomic_write_json(alias_path, report)
            digest = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
            updated = replace(
                current_state,
                **{
                    f"{pointer}_path": _relative(root, canonical_path),
                    f"{pointer}_sha256": digest,
                    "updated_at": generated_at,
                },
            )
            _assert_safe_write_target(root, state_path, "run state")
            atomic_write_json(state_path, updated.to_dict())
    digest = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
    return DiscoveryResult(report, _relative(root, canonical_path), digest)


def discover_category_drift(
    root: Path,
    config: LibraryConfig,
    client: ImslpClient,
    clock: Clock,
    *,
    phase: str = "standalone",
    compare_run_id: str | None = None,
) -> DiscoveryResult:
    """Discover pure-guitar category drift without changing the approved scope."""

    root = Path(root)
    _validate_phase(phase, compare_run_id)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("library root is a symlink or not a directory")
    if not isinstance(config, LibraryConfig):
        raise TypeError("config must be a LibraryConfig")
    try:
        validate_library_config_binding(config)
    except (ConfigError, TypeError, ValueError) as exc:
        raise ValueError("allowlist canonical source binding mismatch") from exc
    _assert_safe_alias(root)

    snapshot: RunSnapshot | None = None
    state: RunState | None = None
    state_path: Path | None = None
    snapshot_bytes: bytes | None = None
    if compare_run_id is not None:
        snapshot, state, state_path, snapshot_bytes = _load_run_context(root, config, compare_run_id)
        pointer = "start_drift_report" if phase == "run_start" else "end_drift_report"
        if getattr(state, f"{pointer}_path") is not None:
            raise FileExistsError(f"{pointer} is already recorded")
        immutable_path = _report_path(root, phase, compare_run_id)
        _assert_safe_write_target(root, immutable_path, "immutable category drift report")
        if immutable_path.exists() or immutable_path.is_symlink():
            raise FileExistsError(immutable_path)

    generated_at = clock.now()
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("clock.now() must be timezone-aware")

    all_categories = client.allcategories(prefix="For")
    all_by_name = {category.name: category for category in all_categories}
    approved_names = {category.name for category in config.categories}
    new_categories: list[AllCategory] = []
    filtered_candidates: list[dict[str, object]] = []
    for category in all_categories:
        reason = _filter_reason(category)
        if reason is not None:
            filtered_candidates.append({**_candidate_item(category), "reason": reason})
        elif category.name not in approved_names:
            new_categories.append(category)

    live_counts: dict[str, int] = {}
    empty_names: set[str] = set()
    deleted_names: set[str] = set()
    for category in config.categories:
        info = client.category_info(category.name)
        count = info.page_count + info.file_count + info.subcategory_count
        live_counts[category.name] = count
        listed = all_by_name.get(category.name)
        if listed is not None:
            consistent = (
                not info.missing
                and listed.size == count
                and listed.page_count == info.page_count
                and listed.file_count == info.file_count
                and listed.subcategory_count == info.subcategory_count
            )
            if not consistent:
                raise ValueError(
                    f"allcategories and categoryinfo disagree for {category.name}"
                )
        elif info.missing:
            deleted_names.add(category.name)
        elif count == 0:
            empty_names.add(category.name)

    frozen_categories: dict[str, CategorySnapshot] = {}
    if snapshot is not None:
        frozen_categories = {category.name: category for category in snapshot.categories}
        if set(frozen_categories) != approved_names:
            raise ValueError("compare snapshot categories do not match supplied allowlist")

    def previous_count(name: str) -> int | None:
        frozen = frozen_categories.get(name)
        return frozen.member_count if frozen is not None else None

    empty_categories = [
        {"name": name, "previous_member_count": previous_count(name)}
        for name in sorted(empty_names)
    ]
    deleted_categories = [
        {"name": name, "previous_member_count": previous_count(name)}
        for name in sorted(deleted_names)
    ]
    member_count_changes = [
        {
            "name": name,
            "previous_member_count": frozen_categories[name].member_count,
            "current_member_count": live_counts[name],
        }
        for name in sorted(frozen_categories)
        if frozen_categories[name].member_count != live_counts[name]
    ]

    possible_renames: list[dict[str, object]] = []
    if snapshot is not None:
        candidate_member_ids = {
            category.name: {member.page_id for member in client.category_members(category.name)}
            for category in new_categories
        }
        missing_or_empty = sorted(empty_names | deleted_names)
        for source_name in missing_or_empty:
            source = frozen_categories[source_name]
            source_ids = set(source.member_page_ids)
            for candidate in new_categories:
                distance = _levenshtein(
                    _normalized_name(source_name), _normalized_name(candidate.name)
                )
                overlap = _jaccard(source_ids, candidate_member_ids[candidate.name])
                if distance <= 6 and overlap >= 0.80:
                    possible_renames.append(
                        {
                            "source_name": source_name,
                            "candidate_name": candidate.name,
                            "name_distance": distance,
                            "jaccard_overlap": round(overlap, 6),
                            "source_member_count": len(source_ids),
                            "candidate_member_count": len(candidate_member_ids[candidate.name]),
                        }
                    )
    possible_renames.sort(key=lambda item: (item["source_name"], item["candidate_name"]))

    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "phase": phase,
        "compare_run_id": compare_run_id,
        "allowlist_version": config.version,
        "allowlist_sha256": config.config_hash,
        "new_candidates": [
            _candidate_item(item)
            for item in sorted(new_categories, key=lambda item: item.name)
        ],
        "empty_categories": empty_categories,
        "deleted_categories": deleted_categories,
        "member_count_changes": member_count_changes,
        "possible_renames": possible_renames,
        "filtered_candidates": sorted(filtered_candidates, key=lambda item: item["name"]),
    }
    if snapshot_bytes is not None:
        snapshot_path, _ = _run_paths(root, compare_run_id)
        if snapshot_path.read_bytes() != snapshot_bytes:
            raise SnapshotError("immutable run snapshot changed during category discovery")
    result = _persist_report(
        root, report, phase, compare_run_id, state, state_path, config, generated_at
    )
    if snapshot_bytes is not None:
        snapshot_path, _ = _run_paths(root, compare_run_id)
        if snapshot_path.read_bytes() != snapshot_bytes:
            raise SnapshotError("discovery mutated the immutable run snapshot")
    return result


__all__ = ["DiscoveryResult", "discover_category_drift"]
