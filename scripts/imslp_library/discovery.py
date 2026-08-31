from __future__ import annotations

import hashlib
import json
import math
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
from .jsonio import atomic_write_json
from .models import CategorySnapshot, RunSnapshot, RunState
from .paths import _assert_safe_read_target, _assert_safe_write_target
from .snapshot import SnapshotError, load_complete_snapshot
from .storage import _file_lock, _run_lock_path


_PHASES = {"standalone", "run_start", "run_end"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
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
_CANDIDATE_KEYS = {
    "name", "size", "page_count", "file_count", "subcategory_count"
}
_FILTERED_KEYS = _CANDIDATE_KEYS | {"reason"}
_CATEGORY_DRIFT_KEYS = {"name", "previous_member_count"}
_COUNT_CHANGE_KEYS = {
    "name", "previous_member_count", "current_member_count"
}
_RENAME_KEYS = {
    "source_name",
    "candidate_name",
    "name_distance",
    "jaccard_overlap",
    "source_member_count",
    "candidate_member_count",
}
_FILTER_REASONS = {
    "malformed_name",
    "not_guitar",
    "electric_guitar",
    "bass_guitar",
    "mixed_instrumentation",
    "unsupported_guitar_structure",
    "zero_members",
}
_TRANSITION_KEYS = {
    "schema_version",
    "model_type",
    "run_id",
    "config_version",
    "config_sha256",
    "phase",
    "report_path",
    "report_sha256",
    "report",
    "predecessor_state",
    "predecessor_state_sha256",
    "target_state",
    "target_state_sha256",
    "alias_path",
    "alias_predecessor_exists",
    "alias_predecessor",
    "alias_predecessor_sha256",
    "alias_target_sha256",
    "intent_sha256",
}
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
    run_id: str | None = None,
    phase: str | None = None,
    config: LibraryConfig | None = None,
) -> None:
    if not isinstance(payload, dict) or set(payload) != _REPORT_KEYS:
        raise SnapshotError("drift report has invalid top-level fields")
    generated_at = payload["generated_at"]
    try:
        parsed = datetime.fromisoformat(generated_at) if isinstance(generated_at, str) else None
    except ValueError:
        parsed = None
    actual_phase = payload["phase"]
    actual_run_id = payload["compare_run_id"]
    valid_identity = (
        type(payload["schema_version"]) is int
        and payload["schema_version"] == 1
        and parsed is not None
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and isinstance(actual_phase, str)
        and actual_phase in _PHASES
        and isinstance(payload["allowlist_version"], str)
        and bool(payload["allowlist_version"].strip())
        and isinstance(payload["allowlist_sha256"], str)
        and _SHA256_RE.fullmatch(payload["allowlist_sha256"]) is not None
        and all(isinstance(payload[group], list) for group in _REPORT_GROUPS)
    )
    if not valid_identity:
        raise SnapshotError("drift report identity or schema is invalid")
    if actual_phase == "standalone":
        if actual_run_id is not None:
            raise SnapshotError("standalone drift report cannot name a compare run")
    elif not isinstance(actual_run_id, str) or not actual_run_id:
        raise SnapshotError("run drift report requires a compare run")
    if phase is not None and actual_phase != phase:
        raise SnapshotError("drift report phase is invalid")
    if run_id is not None and actual_run_id != run_id:
        raise SnapshotError("drift report compare run is invalid")
    if config is not None and (
        payload["allowlist_version"] != config.version
        or payload["allowlist_sha256"] != config.config_hash
    ):
        raise SnapshotError("drift report allowlist identity is invalid")

    def items(group: str, keys: set[str]) -> list[dict[str, object]]:
        values = payload[group]
        if not isinstance(values, list) or any(
            not isinstance(item, dict) or set(item) != keys for item in values
        ):
            raise SnapshotError(f"drift report {group} item schema is invalid")
        return values

    def name(value: object, group: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SnapshotError(f"drift report {group} name is invalid")
        return value

    def count(value: object, group: str) -> int:
        if type(value) is not int or value < 0:
            raise SnapshotError(f"drift report {group} count is invalid")
        return value

    new_items = items("new_candidates", _CANDIDATE_KEYS)
    filtered_items = items("filtered_candidates", _FILTERED_KEYS)
    empty_items = items("empty_categories", _CATEGORY_DRIFT_KEYS)
    deleted_items = items("deleted_categories", _CATEGORY_DRIFT_KEYS)
    change_items = items("member_count_changes", _COUNT_CHANGE_KEYS)
    rename_items = items("possible_renames", _RENAME_KEYS)

    def validate_candidate(item: dict[str, object], group: str) -> str:
        candidate_name = name(item["name"], group)
        size = count(item["size"], group)
        pages = count(item["page_count"], group)
        files = count(item["file_count"], group)
        subcategories = count(item["subcategory_count"], group)
        if size != pages + files + subcategories:
            raise SnapshotError(f"drift report {group} counts are inconsistent")
        return candidate_name

    new_names = [validate_candidate(item, "new_candidates") for item in new_items]
    filtered_names = [
        validate_candidate(item, "filtered_candidates") for item in filtered_items
    ]
    if any(
        not isinstance(item["reason"], str)
        or item["reason"] not in _FILTER_REASONS
        for item in filtered_items
    ):
        raise SnapshotError("drift report filtered_candidates reason is invalid")

    def category_names(
        values: list[dict[str, object]], group: str
    ) -> tuple[list[str], dict[str, int | None]]:
        names: list[str] = []
        counts: dict[str, int | None] = {}
        for item in values:
            item_name = name(item["name"], group)
            previous = item["previous_member_count"]
            if actual_phase == "standalone":
                if previous is not None:
                    raise SnapshotError(f"standalone {group} cannot have a prior count")
            else:
                previous = count(previous, group)
            names.append(item_name)
            counts[item_name] = previous
        return names, counts

    empty_names, empty_counts = category_names(empty_items, "empty_categories")
    deleted_names, deleted_counts = category_names(deleted_items, "deleted_categories")
    change_names: list[str] = []
    for item in change_items:
        item_name = name(item["name"], "member_count_changes")
        previous = count(item["previous_member_count"], "member_count_changes")
        current = count(item["current_member_count"], "member_count_changes")
        if previous == current:
            raise SnapshotError("drift report member_count_changes must actually change")
        if item_name in empty_counts | deleted_counts and current != 0:
            raise SnapshotError("drift report empty or deleted count must change to zero")
        if item_name in empty_counts and empty_counts[item_name] != previous:
            raise SnapshotError("drift report empty prior count is inconsistent")
        if item_name in deleted_counts and deleted_counts[item_name] != previous:
            raise SnapshotError("drift report deleted prior count is inconsistent")
        change_names.append(item_name)

    sequences: tuple[tuple[str, list[object]], ...] = (
        ("new_candidates", new_names),
        ("filtered_candidates", filtered_names),
        ("empty_categories", empty_names),
        ("deleted_categories", deleted_names),
        ("member_count_changes", change_names),
    )
    for group, values in sequences:
        if values != sorted(values) or len(values) != len(set(values)):
            raise SnapshotError(f"drift report {group} must be sorted and unique")
    if set(new_names) & set(filtered_names):
        raise SnapshotError("drift report candidate groups overlap")
    if set(empty_names) & set(deleted_names):
        raise SnapshotError("drift report empty and deleted groups overlap")
    if actual_phase == "standalone" and (change_items or rename_items):
        raise SnapshotError("standalone drift report cannot compare counts or renames")
    if config is not None:
        approved = {category.name for category in config.categories}
        if not (set(empty_names) | set(deleted_names) | set(change_names)) <= approved:
            raise SnapshotError("drift report allowlisted category evidence is invalid")
        if set(new_names) & approved:
            raise SnapshotError("drift report new candidate is already allowlisted")

    new_counts = {
        item["name"]: item["page_count"]
        for item in new_items
    }
    source_counts = empty_counts | deleted_counts
    rename_order: list[tuple[str, str]] = []
    for item in rename_items:
        source_name = name(item["source_name"], "possible_renames")
        candidate_name = name(item["candidate_name"], "possible_renames")
        distance = count(item["name_distance"], "possible_renames")
        overlap = item["jaccard_overlap"]
        source_count = count(item["source_member_count"], "possible_renames")
        candidate_count = count(item["candidate_member_count"], "possible_renames")
        if distance > 6:
            raise SnapshotError("drift report rename distance exceeds threshold")
        if type(overlap) is not float or not math.isfinite(overlap) or not 0.80 <= overlap <= 1.0:
            raise SnapshotError("drift report rename overlap is invalid")
        if source_name not in source_counts or candidate_name not in new_counts:
            raise SnapshotError("drift report rename association is invalid")
        if source_counts[source_name] != source_count or new_counts[candidate_name] != candidate_count:
            raise SnapshotError("drift report rename counts are inconsistent")
        rename_order.append((source_name, candidate_name))
    if rename_order != sorted(rename_order) or len(rename_order) != len(set(rename_order)):
        raise SnapshotError("drift report possible_renames must be sorted and unique")


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
        payload = _mapping_from_bytes(content, f"{stem} drift report")
        _validate_drift_report(
            payload,
            run_id=run_id,
            phase=f"run_{stem}",
            config=config,
        )


def _transition_path(root: Path, run_id: str, phase: str) -> Path:
    stem = "start" if phase == "run_start" else "end"
    return root / "metadata/runs" / f"{run_id}-category-drift-{stem}-transition.json"


def _alias_lock_path(root: Path) -> Path:
    return root / "metadata/.locks/category-drift-alias.lock"


def _pending_transition_paths(root: Path) -> tuple[Path, ...]:
    runs = root / "metadata/runs"
    try:
        _assert_safe_read_target(root, runs, "run metadata directory")
    except ValueError as exc:
        raise SnapshotError("run metadata directory is unsafe") from exc
    if runs.is_symlink() or (runs.exists() and not runs.is_dir()):
        raise SnapshotError("run metadata directory is unsafe")
    if not runs.exists():
        return ()
    pattern = re.compile(r".+-category-drift-(?:start|end)-transition\.json")
    with os.scandir(runs) as entries:
        return tuple(
            sorted(
                (runs / entry.name for entry in entries if pattern.fullmatch(entry.name)),
                key=lambda path: path.name,
            )
        )


def _transition_stage(stage: str) -> None:
    """Failure-injection seam for crash-consistency tests."""


def _mapping_from_bytes(content: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"{label} is not UTF-8 JSON") from exc
    try:
        canonical = _canonical_bytes(payload) if isinstance(payload, dict) else None
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"{label} is not canonical JSON") from exc
    if canonical != content:
        raise SnapshotError(f"{label} is not canonical JSON")
    return payload


def _read_run_state(root: Path, path: Path) -> tuple[RunState, bytes]:
    content = _read_regular_bytes(root, path, "run state")
    try:
        state = RunState.from_dict(_mapping_from_bytes(content, "run state"))
    except (TypeError, ValueError) as exc:
        raise SnapshotError("run state model is invalid") from exc
    return state, content


def _state_bytes(state: RunState) -> bytes:
    return _canonical_bytes(state.to_dict())


def _build_transition(
    root: Path,
    run_id: str,
    phase: str,
    config: LibraryConfig,
    report: dict[str, object],
    report_path: Path,
    predecessor_state: RunState,
    target_state: RunState,
    alias_predecessor: dict[str, object] | None,
) -> dict[str, object]:
    report_sha256 = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    predecessor_bytes = _state_bytes(predecessor_state)
    target_bytes = _state_bytes(target_state)
    alias_predecessor_bytes = (
        _canonical_bytes(alias_predecessor) if alias_predecessor is not None else None
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "model_type": "CategoryDriftTransition",
        "run_id": run_id,
        "config_version": config.version,
        "config_sha256": config.config_hash,
        "phase": phase,
        "report_path": _relative(root, report_path),
        "report_sha256": report_sha256,
        "report": report,
        "predecessor_state": predecessor_state.to_dict(),
        "predecessor_state_sha256": hashlib.sha256(predecessor_bytes).hexdigest(),
        "target_state": target_state.to_dict(),
        "target_state_sha256": hashlib.sha256(target_bytes).hexdigest(),
        "alias_path": "metadata/category_drift_report.json",
        "alias_predecessor_exists": alias_predecessor is not None,
        "alias_predecessor": alias_predecessor,
        "alias_predecessor_sha256": (
            hashlib.sha256(alias_predecessor_bytes).hexdigest()
            if alias_predecessor_bytes is not None
            else None
        ),
        "alias_target_sha256": report_sha256,
    }
    payload["intent_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def _validate_transition(
    root: Path,
    payload: object,
    run_id: str,
    phase: str,
    config: LibraryConfig,
) -> tuple[
    dict[str, object], RunState, RunState, dict[str, object] | None,
]:
    if not isinstance(payload, dict) or set(payload) != _TRANSITION_KEYS:
        raise SnapshotError("category drift transition fields are invalid")
    unsigned = dict(payload)
    intent_sha256 = unsigned.pop("intent_sha256")
    identity_valid = (
        payload["schema_version"] == 1
        and type(payload["schema_version"]) is int
        and payload["model_type"] == "CategoryDriftTransition"
        and payload["run_id"] == run_id
        and payload["config_version"] == config.version
        and payload["config_sha256"] == config.config_hash
        and payload["phase"] == phase
        and isinstance(intent_sha256, str)
        and intent_sha256 == hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    )
    if not identity_valid:
        raise SnapshotError("category drift transition identity is invalid")
    report = payload["report"]
    _validate_drift_report(report, run_id=run_id, phase=phase, config=config)
    if not isinstance(report, dict):
        raise SnapshotError("category drift transition report is invalid")
    report_bytes = _canonical_bytes(report)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    stem = "start" if phase == "run_start" else "end"
    report_path = _drift_report_path(root, run_id, stem)
    if (
        payload["report_path"] != _relative(root, report_path)
        or payload["report_sha256"] != report_sha256
        or payload["alias_path"] != "metadata/category_drift_report.json"
        or payload["alias_target_sha256"] != report_sha256
    ):
        raise SnapshotError("category drift transition report binding is invalid")
    try:
        predecessor = RunState.from_dict(payload["predecessor_state"])
        target = RunState.from_dict(payload["target_state"])
    except (TypeError, ValueError) as exc:
        raise SnapshotError("category drift transition RunState is invalid") from exc
    predecessor_bytes = _state_bytes(predecessor)
    target_bytes = _state_bytes(target)
    if (
        payload["predecessor_state_sha256"]
        != hashlib.sha256(predecessor_bytes).hexdigest()
        or payload["target_state_sha256"] != hashlib.sha256(target_bytes).hexdigest()
        or predecessor.run_id != run_id
        or target.run_id != run_id
    ):
        raise SnapshotError("category drift transition RunState digest is invalid")
    pointer = "start_drift_report" if phase == "run_start" else "end_drift_report"
    generated = datetime.fromisoformat(report["generated_at"])
    expected_target = replace(
        predecessor,
        **{
            f"{pointer}_path": _relative(root, report_path),
            f"{pointer}_sha256": report_sha256,
            "updated_at": generated,
        },
    )
    if (
        getattr(predecessor, f"{pointer}_path") is not None
        or target != expected_target
    ):
        raise SnapshotError("category drift transition RunState relation is invalid")
    alias_exists = payload["alias_predecessor_exists"]
    alias_predecessor = payload["alias_predecessor"]
    alias_digest = payload["alias_predecessor_sha256"]
    if type(alias_exists) is not bool:
        raise SnapshotError("category drift transition alias identity is invalid")
    if alias_exists:
        if not isinstance(alias_predecessor, dict):
            raise SnapshotError("category drift transition alias predecessor is invalid")
        _validate_drift_report(alias_predecessor)
        predecessor_alias_bytes = _canonical_bytes(alias_predecessor)
        if alias_digest != hashlib.sha256(predecessor_alias_bytes).hexdigest():
            raise SnapshotError("category drift transition alias digest is invalid")
    elif alias_predecessor is not None or alias_digest is not None:
        raise SnapshotError("category drift transition absent alias is invalid")
    return report, predecessor, target, alias_predecessor


def _unlink_and_fsync(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _load_run_context(
    root: Path, config: LibraryConfig, run_id: str
) -> tuple[RunSnapshot, RunState, Path, bytes]:
    snapshot = load_complete_snapshot(root, run_id)
    snapshot_path, state_path = _run_paths(root, run_id)
    _assert_safe_read_target(root, state_path, "run state")
    if state_path.is_symlink():
        raise SnapshotError("run state cannot be a symlink")
    state, _ = _read_run_state(root, state_path)
    if snapshot.config_version != config.version or snapshot.config_sha256 != config.config_hash:
        raise ValueError("compare run does not use the supplied allowlist")
    if state.run_id != run_id or state.snapshot_path != _relative(root, snapshot_path):
        raise SnapshotError("run state identity does not match compare run")
    snapshot_bytes = _read_regular_bytes(root, snapshot_path, "run snapshot")
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


def _current_bytes_or_absent(
    root: Path, path: Path, label: str
) -> bytes | None:
    if path.is_symlink():
        raise SnapshotError(f"{label} cannot be a symlink")
    if not path.exists():
        return None
    return _read_regular_bytes(root, path, label)


def _apply_transition(
    root: Path,
    run_id: str,
    phase: str,
    config: LibraryConfig,
    transition_path: Path,
    payload: dict[str, object],
    *,
    emit_stages: bool,
) -> DiscoveryResult:
    report, predecessor, target, alias_predecessor = _validate_transition(
        root, payload, run_id, phase, config
    )
    stem = "start" if phase == "run_start" else "end"
    report_path = _drift_report_path(root, run_id, stem)
    _, state_path = _run_paths(root, run_id)
    alias_path = _assert_safe_alias(root)
    report_bytes = _canonical_bytes(report)
    predecessor_state_bytes = _state_bytes(predecessor)
    target_state_bytes = _state_bytes(target)
    predecessor_alias_bytes = (
        _canonical_bytes(alias_predecessor) if alias_predecessor is not None else None
    )

    current_report = _current_bytes_or_absent(
        root, report_path, "immutable category drift report"
    )
    if current_report not in (None, report_bytes):
        raise SnapshotError("transition immutable report is neither predecessor nor target")
    current_alias = _current_bytes_or_absent(root, alias_path, "category drift alias")
    alias_predecessors = (
        (None,) if predecessor_alias_bytes is None else (predecessor_alias_bytes,)
    )
    if current_alias not in (*alias_predecessors, report_bytes):
        raise SnapshotError("transition alias is not its recorded predecessor or target")
    current_state = _read_regular_bytes(root, state_path, "run state")
    if current_state not in (predecessor_state_bytes, target_state_bytes):
        raise SnapshotError("transition RunState is neither predecessor nor target")

    report_is_target = current_report == report_bytes
    alias_is_target = (
        current_alias == report_bytes and current_alias != predecessor_alias_bytes
    )
    state_is_target = current_state == target_state_bytes
    if alias_is_target and not report_is_target:
        raise SnapshotError("transition alias advanced before immutable report")
    if state_is_target and not (report_is_target and current_alias == report_bytes):
        raise SnapshotError("transition RunState advanced before report artifacts")

    if not report_is_target:
        _write_immutable_json(root, report_path, report)
    if emit_stages:
        _transition_stage("after_immutable")
    if _read_regular_bytes(root, report_path, "immutable category drift report") != report_bytes:
        raise SnapshotError("transition immutable report changed unexpectedly")

    if current_alias != report_bytes:
        _assert_safe_alias(root)
        atomic_write_json(alias_path, report)
    if emit_stages:
        _transition_stage("after_alias")
    if _read_regular_bytes(root, alias_path, "category drift alias") != report_bytes:
        raise SnapshotError("transition alias changed unexpectedly")

    if not state_is_target:
        atomic_write_json(state_path, target.to_dict())
    if emit_stages:
        _transition_stage("after_state")
        _transition_stage("before_cleanup")
    if _read_regular_bytes(root, state_path, "run state") != target_state_bytes:
        raise SnapshotError("transition RunState changed unexpectedly")
    _unlink_and_fsync(transition_path)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    return DiscoveryResult(report, _relative(root, report_path), report_sha256)


def _recover_transition(
    root: Path,
    run_id: str,
    phase: str,
    config: LibraryConfig,
) -> DiscoveryResult | None:
    expected = _transition_path(root, run_id, phase)
    with _file_lock(root, _alias_lock_path(root), "category drift alias lock"):
        pending = _pending_transition_paths(root)
        if not pending:
            return None
        if pending != (expected,):
            raise SnapshotError("a different category drift transition is unsettled")
        with _file_lock(root, _run_lock_path(root, run_id), "run manifest lock"):
            content = _read_regular_bytes(root, expected, "category drift transition")
            payload = _mapping_from_bytes(content, "category drift transition")
            return _apply_transition(
                root,
                run_id,
                phase,
                config,
                expected,
                payload,
                emit_stages=False,
            )


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
        with _file_lock(root, _alias_lock_path(root), "category drift alias lock"):
            if _pending_transition_paths(root):
                raise SnapshotError("category drift transition blocks alias update")
            alias_path = _assert_safe_alias(root)
            atomic_write_json(alias_path, report)
            content = _read_regular_bytes(root, alias_path, "category drift alias")
        return DiscoveryResult(
            report,
            _relative(root, alias_path),
            hashlib.sha256(content).hexdigest(),
        )

    assert state is not None and state_path is not None and compare_run_id is not None
    pointer = "start_drift_report" if phase == "run_start" else "end_drift_report"
    transition_path = _transition_path(root, compare_run_id, phase)
    with _file_lock(root, _alias_lock_path(root), "category drift alias lock"):
        if _pending_transition_paths(root):
            raise SnapshotError("category drift transition already exists")
        with _file_lock(root, _run_lock_path(root, compare_run_id), "run manifest lock"):
            current_state, current_state_bytes = _read_run_state(root, state_path)
            if current_state != state or current_state_bytes != _state_bytes(state):
                raise SnapshotError("RunState changed while category discovery was running")
            _validate_drift_pointers(root, compare_run_id, config, current_state)
            if getattr(current_state, f"{pointer}_path") is not None:
                raise FileExistsError(f"{pointer} is already recorded")
            if canonical_path.exists() or canonical_path.is_symlink():
                raise SnapshotError("immutable drift report exists without a transition")
            if transition_path.exists() or transition_path.is_symlink():
                raise SnapshotError("category drift transition already exists")
            alias_path = _assert_safe_alias(root)
            alias_content = _current_bytes_or_absent(
                root, alias_path, "category drift alias"
            )
            alias_predecessor = (
                _mapping_from_bytes(alias_content, "category drift alias")
                if alias_content is not None
                else None
            )
            if alias_predecessor is not None:
                _validate_drift_report(alias_predecessor)
            digest = hashlib.sha256(_canonical_bytes(report)).hexdigest()
            target_state = replace(
                current_state,
                **{
                    f"{pointer}_path": _relative(root, canonical_path),
                    f"{pointer}_sha256": digest,
                    "updated_at": generated_at,
                },
            )
            intent = _build_transition(
                root,
                compare_run_id,
                phase,
                config,
                report,
                canonical_path,
                current_state,
                target_state,
                alias_predecessor,
            )
            _validate_transition(root, intent, compare_run_id, phase, config)
            _assert_safe_write_target(root, transition_path, "category drift transition")
            _write_immutable_json(root, transition_path, intent)
            if _mapping_from_bytes(
                _read_regular_bytes(root, transition_path, "category drift transition"),
                "category drift transition",
            ) != intent:
                raise SnapshotError("category drift transition changed unexpectedly")
            _transition_stage("after_wal")
            return _apply_transition(
                root,
                compare_run_id,
                phase,
                config,
                transition_path,
                intent,
                emit_stages=True,
            )


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
    if phase == "standalone":
        with _file_lock(root, _alias_lock_path(root), "category drift alias lock"):
            if _pending_transition_paths(root):
                raise SnapshotError("category drift transition blocks standalone discovery")

    snapshot: RunSnapshot | None = None
    state: RunState | None = None
    state_path: Path | None = None
    snapshot_bytes: bytes | None = None
    if compare_run_id is not None:
        snapshot, state, state_path, snapshot_bytes = _load_run_context(root, config, compare_run_id)
        recovered = _recover_transition(root, compare_run_id, phase, config)
        if recovered is not None:
            snapshot_path, _ = _run_paths(root, compare_run_id)
            if _read_regular_bytes(root, snapshot_path, "run snapshot") != snapshot_bytes:
                raise SnapshotError("transition recovery changed the immutable run snapshot")
            return recovered
        pointer = "start_drift_report" if phase == "run_start" else "end_drift_report"
        if getattr(state, f"{pointer}_path") is not None:
            raise FileExistsError(f"{pointer} is already recorded")
        immutable_path = _report_path(root, phase, compare_run_id)
        _assert_safe_write_target(root, immutable_path, "immutable category drift report")
        if immutable_path.exists() or immutable_path.is_symlink():
            raise SnapshotError("immutable drift report exists without a transition")

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
    _validate_drift_report(
        report,
        run_id=compare_run_id,
        phase=phase,
        config=config,
    )
    if snapshot_bytes is not None:
        snapshot_path, _ = _run_paths(root, compare_run_id)
        if _read_regular_bytes(root, snapshot_path, "run snapshot") != snapshot_bytes:
            raise SnapshotError("immutable run snapshot changed during category discovery")
    result = _persist_report(
        root, report, phase, compare_run_id, state, state_path, config, generated_at
    )
    if snapshot_bytes is not None:
        snapshot_path, _ = _run_paths(root, compare_run_id)
        if _read_regular_bytes(root, snapshot_path, "run snapshot") != snapshot_bytes:
            raise SnapshotError("discovery mutated the immutable run snapshot")
    return result


__all__ = ["DiscoveryResult", "discover_category_drift"]
