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
from .config import ConfigError, LibraryConfig, validate_library_config_binding
from .enums import RunStatus
from .headings import parse_heading_tree
from .jsonio import _canonical_bytes, atomic_write_json, read_json
from .models import CategorySnapshot, FrozenPage, RunSnapshot, RunState, ScoreFile
from .paths import _assert_safe_read_target, _assert_safe_write_target


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


def _initialization_intent_path(root: Path, run_id: str) -> Path:
    return root / "metadata/runs" / f"{run_id}-initialization.json"


def _completion_intent_path(root: Path, run_id: str) -> Path:
    return root / "metadata/runs" / f"{run_id}-completion.json"


def _checkpoint_authority_path(root: Path, run_id: str) -> Path:
    return root / "metadata/runs" / f"{run_id}-checkpoint.json"


def _checkpoint_transition_path(root: Path, run_id: str) -> Path:
    return root / "metadata/runs" / f"{run_id}-checkpoint-transition.json"


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


def _unlink_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _strict_intent(
    root: Path,
    path: Path,
    model_type: str,
    keys: set[str],
) -> dict[str, object]:
    _assert_safe_read_target(root, path, model_type)
    if path.is_symlink():
        raise SnapshotError(f"{model_type} cannot be a symlink")
    try:
        payload = read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SnapshotError(f"invalid {model_type}") from exc
    if set(payload) != keys or payload.get("schema_version") != 1 or payload.get("model_type") != model_type:
        raise SnapshotError(f"invalid {model_type} envelope")
    return payload


def _snapshot_from_path(root: Path, path: Path) -> RunSnapshot:
    _assert_safe_read_target(root, path, "run snapshot")
    try:
        return RunSnapshot.from_dict(read_json(path))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SnapshotError(f"invalid run snapshot: {path}") from exc


def _state_from_path(root: Path, path: Path) -> RunState:
    _assert_safe_read_target(root, path, "run state")
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


def _checkpoint_authority_payload(
    snapshot: RunSnapshot,
    state: RunState,
    generation: int,
) -> dict[str, object]:
    if snapshot.status is not RunStatus.SNAPSHOT_INCOMPLETE or state.snapshot_sha256 is not None:
        raise SnapshotError("checkpoint authority requires incomplete snapshot models")
    snapshot_mapping = snapshot.to_dict()
    state_mapping = state.to_dict()
    return {
        "schema_version": 1,
        "model_type": "SnapshotCheckpointAuthority",
        "run_id": snapshot.run_id,
        "generation": generation,
        "config_version": snapshot.config_version,
        "config_sha256": snapshot.config_sha256,
        "snapshot": snapshot_mapping,
        "snapshot_sha256": _sha256_bytes(_canonical_bytes(snapshot_mapping)),
        "state": state_mapping,
        "state_sha256": _sha256_bytes(_canonical_bytes(state_mapping)),
    }


def _validate_checkpoint_authority_payload(
    payload: dict[str, object],
    run_id: str,
    config: LibraryConfig,
) -> tuple[RunSnapshot, RunState, int]:
    expected_keys = {
        "schema_version",
        "model_type",
        "run_id",
        "generation",
        "config_version",
        "config_sha256",
        "snapshot",
        "snapshot_sha256",
        "state",
        "state_sha256",
    }
    if (
        set(payload) != expected_keys
        or payload.get("schema_version") != 1
        or payload.get("model_type") != "SnapshotCheckpointAuthority"
    ):
        raise SnapshotError("invalid checkpoint authority envelope")
    try:
        snapshot = RunSnapshot.from_dict(payload["snapshot"])
        state = RunState.from_dict(payload["state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid checkpoint authority models") from exc
    generation = payload["generation"]
    if (
        type(generation) is not int
        or generation < 0
        or payload["run_id"] != run_id
        or snapshot.run_id != run_id
        or state.run_id != run_id
        or payload["config_version"] != config.version
        or payload["config_sha256"] != config.config_hash
        or snapshot.config_version != config.version
        or snapshot.config_sha256 != config.config_hash
        or snapshot.status is not RunStatus.SNAPSHOT_INCOMPLETE
        or state.snapshot_sha256 is not None
        or state.snapshot_path != f"metadata/runs/{run_id}.json"
    ):
        raise SnapshotError("checkpoint authority identity mismatch")
    snapshot_digest = _sha256_bytes(_canonical_bytes(snapshot.to_dict()))
    state_digest = _sha256_bytes(_canonical_bytes(state.to_dict()))
    if payload["snapshot_sha256"] != snapshot_digest or payload["state_sha256"] != state_digest:
        raise SnapshotError("checkpoint authority digest mismatch")
    return snapshot, state, generation


def _authority_digest(payload: dict[str, object]) -> str:
    return _sha256_bytes(_canonical_bytes(payload))


def _read_checkpoint_authority(
    root: Path,
    run_id: str,
    config: LibraryConfig,
) -> tuple[dict[str, object], RunSnapshot, RunState, int]:
    path = _checkpoint_authority_path(root, run_id)
    _assert_safe_read_target(root, path, "checkpoint authority")
    _assert_safe_write_target(root, path, "checkpoint authority")
    if path.is_symlink() or not path.is_file():
        raise SnapshotError("checkpoint authority is missing or a symlink")
    try:
        payload = read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid checkpoint authority") from exc
    snapshot, state, generation = _validate_checkpoint_authority_payload(payload, run_id, config)
    if _sha256_file(path) != _authority_digest(payload):
        raise SnapshotError("checkpoint authority bytes are not canonical")
    return payload, snapshot, state, generation


def _initialization_payload(
    snapshot: RunSnapshot,
    state: RunState,
    checkpoint_authority: dict[str, object],
) -> dict[str, object]:
    snapshot_mapping = snapshot.to_dict()
    state_mapping = state.to_dict()
    return {
        "schema_version": 1,
        "model_type": "SnapshotInitializationIntent",
        "run_id": snapshot.run_id,
        "snapshot": snapshot_mapping,
        "snapshot_sha256": _sha256_bytes(_canonical_bytes(snapshot_mapping)),
        "state": state_mapping,
        "state_sha256": _sha256_bytes(_canonical_bytes(state_mapping)),
        "checkpoint_authority": checkpoint_authority,
        "checkpoint_authority_sha256": _authority_digest(checkpoint_authority),
    }


def _validate_initialization_payload(
    payload: dict[str, object],
    run_id: str,
    config: LibraryConfig,
) -> tuple[RunSnapshot, RunState, dict[str, object]]:
    try:
        snapshot = RunSnapshot.from_dict(payload["snapshot"])
        state = RunState.from_dict(payload["state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid SnapshotInitializationIntent models") from exc
    try:
        authority = payload["checkpoint_authority"]
        if not isinstance(authority, dict):
            raise TypeError("checkpoint authority must be an object")
        authority_snapshot, authority_state, generation = _validate_checkpoint_authority_payload(
            authority, run_id, config
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid SnapshotInitializationIntent checkpoint authority") from exc
    if (
        payload["run_id"] != run_id
        or snapshot.run_id != run_id
        or state.run_id != run_id
        or snapshot.status is not RunStatus.SNAPSHOT_INCOMPLETE
        or snapshot.categories
        or snapshot.pages
        or snapshot.score_files
        or state.snapshot_sha256 is not None
        or state.snapshot_path != f"metadata/runs/{run_id}.json"
        or snapshot.config_version != config.version
        or snapshot.config_sha256 != config.config_hash
        or generation != 0
        or authority_snapshot != snapshot
        or authority_state != state
    ):
        raise SnapshotError("SnapshotInitializationIntent identity mismatch")
    expected_snapshot_hash = _sha256_bytes(_canonical_bytes(snapshot.to_dict()))
    expected_state_hash = _sha256_bytes(_canonical_bytes(state.to_dict()))
    if (
        payload["snapshot_sha256"] != expected_snapshot_hash
        or payload["state_sha256"] != expected_state_hash
        or payload["checkpoint_authority_sha256"] != _authority_digest(authority)
    ):
        raise SnapshotError("SnapshotInitializationIntent digest mismatch")
    return snapshot, state, authority


def _reconcile_initialization(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    snapshot_path: Path,
    state_path: Path,
) -> None:
    intent_path = _initialization_intent_path(root, run_id)
    authority_path = _checkpoint_authority_path(root, run_id)
    for path, label in (
        (intent_path, "snapshot initialization intent"),
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
        (authority_path, "checkpoint authority"),
    ):
        _assert_safe_write_target(root, path, label)
    if not intent_path.exists():
        return
    payload = _strict_intent(
        root,
        intent_path,
        "SnapshotInitializationIntent",
        {
            "schema_version", "model_type", "run_id", "snapshot", "snapshot_sha256",
            "state", "state_sha256", "checkpoint_authority", "checkpoint_authority_sha256",
        },
    )
    snapshot, state, authority = _validate_initialization_payload(payload, run_id, config)
    for path, mapping, digest, label in (
        (snapshot_path, snapshot.to_dict(), payload["snapshot_sha256"], "run snapshot"),
        (state_path, state.to_dict(), payload["state_sha256"], "run state"),
        (authority_path, authority, payload["checkpoint_authority_sha256"], "checkpoint authority"),
    ):
        _assert_safe_write_target(root, path, label)
        if path.exists():
            if path.is_symlink() or _sha256_file(path) != digest:
                raise SnapshotError(f"{label} does not match initialization intent")
        else:
            atomic_write_json(path, mapping)
            if _sha256_file(path) != digest:
                raise SnapshotError(f"{label} write does not match initialization intent")
    _unlink_durable(intent_path)


def _write_initial_pair(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    snapshot_path: Path,
    state_path: Path,
    snapshot: RunSnapshot,
    state: RunState,
) -> None:
    intent_path = _initialization_intent_path(root, run_id)
    authority_path = _checkpoint_authority_path(root, run_id)
    for path, label in (
        (intent_path, "snapshot initialization intent"),
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
        (authority_path, "checkpoint authority"),
    ):
        _assert_safe_write_target(root, path, label)
    if any(
        path.exists() or path.is_symlink()
        for path in (intent_path, snapshot_path, state_path, authority_path)
    ):
        raise SnapshotError("snapshot initialization targets must be absent")
    authority = _checkpoint_authority_payload(snapshot, state, 0)
    payload = _initialization_payload(snapshot, state, authority)
    _validate_initialization_payload(payload, run_id, config)
    atomic_write_json(intent_path, payload)
    atomic_write_json(snapshot_path, snapshot.to_dict())
    atomic_write_json(state_path, state.to_dict())
    atomic_write_json(authority_path, authority)
    _reconcile_initialization(root, run_id, config, snapshot_path, state_path)


def _checkpoint_pair_kind(
    snapshot: RunSnapshot,
    state: RunState,
    predecessor_snapshot: RunSnapshot,
    predecessor_state: RunState,
    target_snapshot: RunSnapshot,
    target_state: RunState,
) -> tuple[str, str]:
    snapshot_kind = "predecessor" if snapshot == predecessor_snapshot else "target" if snapshot == target_snapshot else "invalid"
    state_kind = "predecessor" if state == predecessor_state else "target" if state == target_state else "invalid"
    return snapshot_kind, state_kind


def _checkpoint_transition_payload(
    run_id: str,
    predecessor_authority: dict[str, object],
    target_authority: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_type": "SnapshotCheckpointTransition",
        "run_id": run_id,
        "predecessor_authority": predecessor_authority,
        "predecessor_authority_sha256": _authority_digest(predecessor_authority),
        "target_authority": target_authority,
        "target_authority_sha256": _authority_digest(target_authority),
    }


def _validate_checkpoint_transition_payload(
    payload: dict[str, object],
    run_id: str,
    config: LibraryConfig,
) -> tuple[
    dict[str, object], RunSnapshot, RunState, int,
    dict[str, object], RunSnapshot, RunState, int,
]:
    expected_keys = {
        "schema_version", "model_type", "run_id",
        "predecessor_authority", "predecessor_authority_sha256",
        "target_authority", "target_authority_sha256",
    }
    if (
        set(payload) != expected_keys
        or payload.get("schema_version") != 1
        or payload.get("model_type") != "SnapshotCheckpointTransition"
        or payload.get("run_id") != run_id
    ):
        raise SnapshotError("invalid checkpoint transition envelope")
    predecessor = payload["predecessor_authority"]
    target = payload["target_authority"]
    if not isinstance(predecessor, dict) or not isinstance(target, dict):
        raise SnapshotError("invalid checkpoint transition authorities")
    predecessor_snapshot, predecessor_state, predecessor_generation = _validate_checkpoint_authority_payload(
        predecessor, run_id, config
    )
    target_snapshot, target_state, target_generation = _validate_checkpoint_authority_payload(
        target, run_id, config
    )
    if (
        payload["predecessor_authority_sha256"] != _authority_digest(predecessor)
        or payload["target_authority_sha256"] != _authority_digest(target)
        or target_generation != predecessor_generation + 1
    ):
        raise SnapshotError("checkpoint transition digest or generation mismatch")
    return (
        predecessor, predecessor_snapshot, predecessor_state, predecessor_generation,
        target, target_snapshot, target_state, target_generation,
    )


def _reconcile_checkpoint_transition(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    snapshot_path: Path,
    state_path: Path,
) -> tuple[RunSnapshot, RunState, dict[str, object], int]:
    authority_path = _checkpoint_authority_path(root, run_id)
    transition_path = _checkpoint_transition_path(root, run_id)
    for path, label in (
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
        (authority_path, "checkpoint authority"),
        (transition_path, "checkpoint transition"),
    ):
        _assert_safe_write_target(root, path, label)
    authority, authority_snapshot, authority_state, authority_generation = _read_checkpoint_authority(
        root, run_id, config
    )
    try:
        snapshot = _snapshot_from_path(root, snapshot_path)
        state = _state_from_path(root, state_path)
    except SnapshotError as exc:
        raise SnapshotError("checkpoint authority cannot validate current models") from exc
    if not transition_path.exists():
        if transition_path.is_symlink():
            raise SnapshotError("checkpoint transition cannot be a symlink")
        if snapshot != authority_snapshot or state != authority_state:
            raise SnapshotError("checkpoint authority does not match incomplete snapshot/state")
        if (
            _sha256_file(snapshot_path) != authority["snapshot_sha256"]
            or _sha256_file(state_path) != authority["state_sha256"]
        ):
            raise SnapshotError("checkpoint authority byte digest mismatch")
        return snapshot, state, authority, authority_generation
    transition = _strict_intent(
        root,
        transition_path,
        "SnapshotCheckpointTransition",
        {
            "schema_version", "model_type", "run_id",
            "predecessor_authority", "predecessor_authority_sha256",
            "target_authority", "target_authority_sha256",
        },
    )
    (
        predecessor, predecessor_snapshot, predecessor_state, predecessor_generation,
        target, target_snapshot, target_state, target_generation,
    ) = _validate_checkpoint_transition_payload(transition, run_id, config)
    authority_digest = _authority_digest(authority)
    if authority_digest == _authority_digest(predecessor):
        if authority != predecessor or authority_generation != predecessor_generation:
            raise SnapshotError("checkpoint authority predecessor mismatch")
        authority_kind = "predecessor"
    elif authority_digest == _authority_digest(target):
        if authority != target or authority_generation != target_generation:
            raise SnapshotError("checkpoint authority target mismatch")
        authority_kind = "target"
    else:
        raise SnapshotError("checkpoint authority is outside pending transition")
    snapshot_is_predecessor = snapshot == predecessor_snapshot
    snapshot_is_target = snapshot == target_snapshot
    state_is_predecessor = state == predecessor_state
    state_is_target = state == target_state
    if (
        not (snapshot_is_predecessor or snapshot_is_target)
        or not (state_is_predecessor or state_is_target)
        or (
            snapshot_is_predecessor
            and not snapshot_is_target
            and state_is_target
            and not state_is_predecessor
        )
    ):
        raise SnapshotError("checkpoint authority found an invalid transition write order")
    valid_snapshot_digests = {
        digest
        for matches, digest in (
            (snapshot_is_predecessor, predecessor["snapshot_sha256"]),
            (snapshot_is_target, target["snapshot_sha256"]),
        )
        if matches
    }
    valid_state_digests = {
        digest
        for matches, digest in (
            (state_is_predecessor, predecessor["state_sha256"]),
            (state_is_target, target["state_sha256"]),
        )
        if matches
    }
    if (
        _sha256_file(snapshot_path) not in valid_snapshot_digests
        or _sha256_file(state_path) not in valid_state_digests
    ):
        raise SnapshotError("checkpoint transition current model byte digest mismatch")
    if authority_kind == "target" and not (snapshot_is_target and state_is_target):
        raise SnapshotError("checkpoint authority advanced before snapshot/state")
    if not snapshot_is_target:
        atomic_write_json(snapshot_path, target_snapshot.to_dict())
    if not state_is_target:
        atomic_write_json(state_path, target_state.to_dict())
    if authority_kind == "predecessor":
        atomic_write_json(authority_path, target)
    if (
        _sha256_file(snapshot_path) != target["snapshot_sha256"]
        or _sha256_file(state_path) != target["state_sha256"]
        or _sha256_file(authority_path) != _authority_digest(target)
    ):
        raise SnapshotError("checkpoint authority reconciliation digest mismatch")
    _unlink_durable(transition_path)
    return target_snapshot, target_state, target, target_generation


def _checkpoint(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    snapshot_path: Path,
    state_path: Path,
    snapshot: RunSnapshot,
    state: RunState,
    now: datetime,
) -> RunState:
    if snapshot.status is not RunStatus.SNAPSHOT_INCOMPLETE:
        raise SnapshotError("only an incomplete snapshot may be checkpointed")
    current_snapshot, current_state, predecessor, generation = _reconcile_checkpoint_transition(
        root, run_id, config, snapshot_path, state_path
    )
    if state != current_state:
        raise SnapshotError("checkpoint caller state does not match checkpoint authority")
    if (
        snapshot.run_id != run_id
        or snapshot.config_version != config.version
        or snapshot.config_sha256 != config.config_hash
        or snapshot.snapshot_started_at != current_snapshot.snapshot_started_at
    ):
        raise SnapshotError("checkpoint target identity mismatch")
    updated = replace(state, snapshot_sha256=None, updated_at=now)
    target = _checkpoint_authority_payload(snapshot, updated, generation + 1)
    transition = _checkpoint_transition_payload(run_id, predecessor, target)
    transition_path = _checkpoint_transition_path(root, run_id)
    authority_path = _checkpoint_authority_path(root, run_id)
    for path, label in (
        (transition_path, "checkpoint transition"),
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
        (authority_path, "checkpoint authority"),
    ):
        _assert_safe_write_target(root, path, label)
    if transition_path.exists() or transition_path.is_symlink():
        raise SnapshotError("checkpoint transition already exists")
    atomic_write_json(transition_path, transition)
    atomic_write_json(snapshot_path, snapshot.to_dict())
    atomic_write_json(state_path, updated.to_dict())
    atomic_write_json(authority_path, target)
    reconciled_snapshot, reconciled_state, _, _ = _reconcile_checkpoint_transition(
        root, run_id, config, snapshot_path, state_path
    )
    if reconciled_snapshot != snapshot or reconciled_state != updated:
        raise SnapshotError("checkpoint reconciliation changed intended models")
    return reconciled_state


def _completion_payload(
    snapshot: RunSnapshot,
    state: RunState,
    checkpoint_authority: dict[str, object],
) -> dict[str, object]:
    if snapshot.status is not RunStatus.SNAPSHOT_COMPLETE or state.snapshot_sha256 is None:
        raise SnapshotError("completion intent requires complete snapshot and state")
    snapshot_mapping = snapshot.to_dict()
    expected = _sha256_bytes(_canonical_bytes(snapshot_mapping))
    if state.snapshot_sha256 != expected:
        raise SnapshotError("completion state hash does not bind expected snapshot")
    return {
        "schema_version": 1,
        "model_type": "SnapshotCompletionIntent",
        "run_id": snapshot.run_id,
        "snapshot": snapshot_mapping,
        "snapshot_sha256": expected,
        "state": state.to_dict(),
        "checkpoint_authority": checkpoint_authority,
        "checkpoint_authority_sha256": _authority_digest(checkpoint_authority),
    }


def _validate_completion_payload(
    payload: dict[str, object],
    run_id: str,
    config: LibraryConfig,
) -> tuple[RunSnapshot, RunState, dict[str, object], RunSnapshot, RunState]:
    try:
        expected_snapshot = RunSnapshot.from_dict(payload["snapshot"])
        expected_state = RunState.from_dict(payload["state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("invalid SnapshotCompletionIntent models") from exc
    checkpoint_authority = payload.get("checkpoint_authority")
    if not isinstance(checkpoint_authority, dict):
        raise SnapshotError("invalid SnapshotCompletionIntent checkpoint authority")
    predecessor_snapshot, predecessor_state, _ = _validate_checkpoint_authority_payload(
        checkpoint_authority, run_id, config
    )
    digest = _sha256_bytes(_canonical_bytes(expected_snapshot.to_dict()))
    expected_predecessor = replace(
        expected_snapshot,
        status=RunStatus.SNAPSHOT_INCOMPLETE,
        snapshot_completed_at=None,
    )
    if (
        payload["run_id"] != run_id
        or expected_snapshot.run_id != run_id
        or expected_state.run_id != run_id
        or expected_snapshot.status is not RunStatus.SNAPSHOT_COMPLETE
        or expected_state.snapshot_sha256 != digest
        or expected_state.snapshot_path != f"metadata/runs/{run_id}.json"
        or payload["snapshot_sha256"] != digest
        or expected_snapshot.config_version != config.version
        or expected_snapshot.config_sha256 != config.config_hash
        or predecessor_snapshot != expected_predecessor
        or payload.get("checkpoint_authority_sha256") != _authority_digest(checkpoint_authority)
    ):
        raise SnapshotError("SnapshotCompletionIntent identity or digest mismatch")
    comparable_final_state = replace(
        expected_state,
        snapshot_sha256=None,
        updated_at=predecessor_state.updated_at,
    )
    if comparable_final_state != predecessor_state:
        raise SnapshotError("SnapshotCompletionIntent state does not extend checkpoint authority")
    return (
        expected_snapshot,
        expected_state,
        checkpoint_authority,
        predecessor_snapshot,
        predecessor_state,
    )


def _reconcile_completion(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    snapshot_path: Path,
    state_path: Path,
    snapshot: RunSnapshot,
    state: RunState,
) -> tuple[RunSnapshot, RunState]:
    intent_path = _completion_intent_path(root, run_id)
    authority_path = _checkpoint_authority_path(root, run_id)
    transition_path = _checkpoint_transition_path(root, run_id)
    for path, label in (
        (intent_path, "snapshot completion intent"),
        (authority_path, "checkpoint authority"),
        (transition_path, "checkpoint transition"),
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
    ):
        _assert_safe_write_target(root, path, label)
    if not intent_path.exists():
        if intent_path.is_symlink():
            raise SnapshotError("snapshot completion intent cannot be a symlink")
        if snapshot.status is RunStatus.SNAPSHOT_COMPLETE and state.snapshot_sha256 is None:
            raise SnapshotError("complete snapshot with missing digest has no completion intent")
        return snapshot, state
    if transition_path.exists() or transition_path.is_symlink():
        raise SnapshotError("completion intent cannot coexist with checkpoint transition")
    payload = _strict_intent(
        root,
        intent_path,
        "SnapshotCompletionIntent",
        {
            "schema_version", "model_type", "run_id", "snapshot", "snapshot_sha256", "state",
            "checkpoint_authority", "checkpoint_authority_sha256",
        },
    )
    (
        expected_snapshot,
        expected_state,
        predecessor_authority,
        predecessor_snapshot,
        predecessor_state,
    ) = _validate_completion_payload(payload, run_id, config)
    expected_digest = expected_state.snapshot_sha256
    snapshot_kind, state_kind = _checkpoint_pair_kind(
        snapshot,
        state,
        predecessor_snapshot,
        predecessor_state,
        expected_snapshot,
        expected_state,
    )
    if (snapshot_kind, state_kind) not in {
        ("predecessor", "predecessor"),
        ("target", "predecessor"),
        ("target", "target"),
    }:
        raise SnapshotError("snapshot/state do not match completion intent transition")
    current_snapshot_digest = (
        predecessor_authority["snapshot_sha256"]
        if snapshot_kind == "predecessor"
        else payload["snapshot_sha256"]
    )
    current_state_digest = (
        predecessor_authority["state_sha256"]
        if state_kind == "predecessor"
        else _sha256_bytes(_canonical_bytes(expected_state.to_dict()))
    )
    if (
        _sha256_file(snapshot_path) != current_snapshot_digest
        or _sha256_file(state_path) != current_state_digest
    ):
        raise SnapshotError("completion transition current model byte digest mismatch")
    authority_present = authority_path.exists() or authority_path.is_symlink()
    if authority_present:
        _assert_safe_read_target(root, authority_path, "checkpoint authority")
        if authority_path.is_symlink() or not authority_path.is_file():
            raise SnapshotError("checkpoint authority cannot be a symlink during completion")
        try:
            current_authority = read_json(authority_path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SnapshotError("invalid checkpoint authority during completion") from exc
        _validate_checkpoint_authority_payload(current_authority, run_id, config)
        if (
            current_authority != predecessor_authority
            or _sha256_file(authority_path) != payload["checkpoint_authority_sha256"]
        ):
            raise SnapshotError("checkpoint authority does not match completion intent predecessor")
    elif (snapshot_kind, state_kind) != ("target", "target"):
        raise SnapshotError("completion intent lost checkpoint authority before final models")
    if snapshot_kind == "predecessor":
        atomic_write_json(snapshot_path, expected_snapshot.to_dict())
        snapshot = expected_snapshot
    if _sha256_file(snapshot_path) != expected_digest:
        raise SnapshotError("complete snapshot bytes do not match completion intent")
    if state_kind == "predecessor":
        atomic_write_json(state_path, expected_state.to_dict())
        state = expected_state
    if _sha256_file(state_path) != _sha256_bytes(_canonical_bytes(expected_state.to_dict())):
        raise SnapshotError("RunState bytes do not match completion intent")
    if authority_present:
        _unlink_durable(authority_path)
    _unlink_durable(intent_path)
    try:
        snapshot_path.chmod(0o444)
    except OSError:
        pass
    return snapshot, state


def _commit_completion(
    root: Path,
    run_id: str,
    config: LibraryConfig,
    snapshot_path: Path,
    state_path: Path,
    completed: RunSnapshot,
    final_state: RunState,
) -> None:
    intent_path = _completion_intent_path(root, run_id)
    authority_path = _checkpoint_authority_path(root, run_id)
    transition_path = _checkpoint_transition_path(root, run_id)
    for path, label in (
        (intent_path, "snapshot completion intent"),
        (authority_path, "checkpoint authority"),
        (transition_path, "checkpoint transition"),
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
    ):
        _assert_safe_write_target(root, path, label)
    if intent_path.exists() or intent_path.is_symlink():
        raise SnapshotError("snapshot completion intent already exists")
    current_snapshot, current_state, authority, _ = _reconcile_checkpoint_transition(
        root, run_id, config, snapshot_path, state_path
    )
    predecessor_snapshot = replace(
        completed,
        status=RunStatus.SNAPSHOT_INCOMPLETE,
        snapshot_completed_at=None,
    )
    if current_snapshot != predecessor_snapshot:
        raise SnapshotError("completion target does not extend checkpoint authority snapshot")
    if replace(final_state, snapshot_sha256=None, updated_at=current_state.updated_at) != current_state:
        raise SnapshotError("completion target state does not extend checkpoint authority state")
    payload = _completion_payload(completed, final_state, authority)
    _validate_completion_payload(payload, run_id, config)
    atomic_write_json(intent_path, payload)
    atomic_write_json(snapshot_path, completed.to_dict())
    atomic_write_json(state_path, final_state.to_dict())
    reconciled_snapshot, reconciled_state = _reconcile_completion(
        root, run_id, config, snapshot_path, state_path, completed, final_state
    )
    if reconciled_snapshot != completed or reconciled_state != final_state:
        raise SnapshotError("completion reconciliation changed committed models")


def _cache_path(root: Path, page: FrozenPage) -> Path:
    expected = root / "metadata/.cache/pages" / str(page.page_id) / f"{page.revision_id}.wiki"
    if _relative(root, expected) != page.wikitext_path:
        raise SnapshotError("frozen page cache path is not canonical")
    _assert_safe_read_target(root, expected, "revision cache")
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
    *,
    require_quarantine_file: bool = True,
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
    _assert_safe_read_target(root, quarantine, "cache quarantine")
    _assert_safe_write_target(root, quarantine, "cache quarantine")
    if require_quarantine_file:
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
    _assert_safe_read_target(root, path, "cache quarantine manifest")
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


def _expected_cache_hashes(run_snapshot: RunSnapshot) -> dict[tuple[int, int], str]:
    return {
        (item.page_id, item.revision_id): item.wikitext_sha256
        for item in run_snapshot.pages
    }


def _cache_intent_path(root: Path, run_id: str, page_id: int, revision_id: int) -> Path:
    return root / "quarantine/transactions" / f"cache-{run_id}-{page_id}-{revision_id}.json"


def _validate_quarantine_file(root: Path, item: dict[str, object]) -> None:
    quarantine = root / str(item["quarantine_path"])
    _assert_safe_read_target(root, quarantine, "cache quarantine")
    if quarantine.is_symlink() or not quarantine.is_file():
        raise SnapshotError("cache quarantine intent destination is invalid")
    if quarantine.stat().st_size != item["size"] or _sha256_file(quarantine) != item["actual_sha256"]:
        raise SnapshotError("cache quarantine intent destination bytes mismatch")


def _validate_quarantine_source(root: Path, item: dict[str, object]) -> None:
    source = root / str(item["source_path"])
    _assert_safe_read_target(root, source, "revision cache")
    if source.is_symlink() or not source.is_file():
        raise SnapshotError("cache quarantine intent source is invalid")
    details = os.stat(source, follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise SnapshotError("cache quarantine intent source must be singly linked")
    if details.st_size != item["size"] or _sha256_file(source) != item["actual_sha256"]:
        raise SnapshotError("cache quarantine intent source bytes mismatch")


def _validate_quarantine_tree(root: Path, run_id: str, items: list[dict[str, object]]) -> None:
    tree_root = root / "quarantine/cache" / run_id
    _assert_safe_read_target(root, tree_root, "cache quarantine tree")
    _assert_safe_write_target(root, tree_root / ".probe", "cache quarantine tree")
    actual: set[str] = set()
    if tree_root.exists() or tree_root.is_symlink():
        if tree_root.is_symlink() or not tree_root.is_dir():
            raise SnapshotError("cache quarantine tree root is invalid")
        for path in tree_root.rglob("*"):
            if path.is_symlink():
                raise SnapshotError("cache quarantine tree contains a symlink")
            if path.is_file():
                actual.add(_relative(root, path))
            elif not path.is_dir():
                raise SnapshotError("cache quarantine tree contains a special file")
    expected = {str(item["quarantine_path"]) for item in items}
    if actual != expected:
        raise SnapshotError("cache quarantine manifest and tree are not equal")


def _reconcile_cache_quarantine(root: Path, run_id: str, run_snapshot: RunSnapshot) -> None:
    expected_hashes = {
        (item.page_id, item.revision_id): item.wikitext_sha256
        for item in run_snapshot.pages
    }
    manifest_path = root / "quarantine/manifests" / f"cache-{run_id}.json"
    _assert_safe_read_target(root, manifest_path, "cache quarantine manifest")
    _assert_safe_write_target(root, manifest_path, "cache quarantine manifest")
    items = _read_cache_manifest(root, manifest_path, run_id, expected_hashes)
    by_identity = {(item["page_id"], item["revision_id"]): item for item in items}
    transactions = root / "quarantine/transactions"
    _assert_safe_read_target(root, transactions, "cache quarantine transactions")
    _assert_safe_write_target(root, transactions / ".probe", "cache quarantine transactions")
    intent_paths = sorted(transactions.glob(f"cache-{run_id}-*.json")) if transactions.exists() else []
    for intent_path in intent_paths:
        payload = _strict_intent(
            root,
            intent_path,
            "CacheQuarantineIntent",
            {"schema_version", "model_type", "run_id", "item"},
        )
        if payload["run_id"] != run_id or not isinstance(payload["item"], dict):
            raise SnapshotError("cache quarantine intent identity mismatch")
        item = payload["item"]
        _validate_manifest_item(
            root, run_id, expected_hashes, item, require_quarantine_file=False
        )
        identity = (item["page_id"], item["revision_id"])
        if intent_path != _cache_intent_path(root, run_id, *identity):
            raise SnapshotError("cache quarantine intent filename mismatch")
        source = root / str(item["source_path"])
        quarantine = root / str(item["quarantine_path"])
        for path, label in ((source, "revision cache"), (quarantine, "cache quarantine")):
            _assert_safe_read_target(root, path, label)
            _assert_safe_write_target(root, path, label)
        existing = by_identity.get(identity)
        if existing is not None:
            if existing != item or source.exists() or source.is_symlink():
                raise SnapshotError("cache quarantine intent conflicts with committed manifest")
            _validate_quarantine_file(root, item)
            _unlink_durable(intent_path)
            continue
        source_present = source.exists() or source.is_symlink()
        quarantine_present = quarantine.exists() or quarantine.is_symlink()
        if source_present and not quarantine_present:
            _validate_quarantine_source(root, item)
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, quarantine)
            _fsync_directory(source.parent)
            _fsync_directory(quarantine.parent)
        elif not source_present and quarantine_present:
            _validate_quarantine_file(root, item)
        else:
            raise SnapshotError("cache quarantine intent has ambiguous source/destination state")
        atomic_write_json(
            manifest_path,
            {"schema_version": 1, "model_type": "CacheQuarantineManifest", "items": [*items, item]},
        )
        items = _read_cache_manifest(root, manifest_path, run_id, expected_hashes)
        by_identity[identity] = item
        _unlink_durable(intent_path)
    _validate_quarantine_tree(root, run_id, items)


def _quarantine_corrupt_cache(root: Path, run_id: str, page: FrozenPage, clock: Clock) -> None:
    source = _cache_path(root, page)
    quarantine = root / "quarantine/cache" / run_id / str(page.page_id) / f"{page.revision_id}.wiki"
    intent_path = _cache_intent_path(root, run_id, page.page_id, page.revision_id)
    for path, label in (
        (source, "revision cache"),
        (quarantine, "cache quarantine"),
        (intent_path, "cache quarantine intent"),
    ):
        _assert_safe_write_target(root, path, label)
    if source.is_symlink() or not source.is_file():
        raise SnapshotError("corrupt revision cache is not a regular file")
    details = os.stat(source, follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise SnapshotError("corrupt revision cache must be singly linked")
    if quarantine.exists() or quarantine.is_symlink() or intent_path.exists() or intent_path.is_symlink():
        raise SnapshotError("cache quarantine target or intent already exists")
    run_snapshot = _snapshot_from_path(root, root / "metadata/runs" / f"{run_id}.json")
    expected_hashes = _expected_cache_hashes(run_snapshot)
    if expected_hashes.get((page.page_id, page.revision_id)) != page.wikitext_sha256:
        raise SnapshotError("corrupt cache page is not in the frozen checkpoint")
    item: dict[str, object] = {
        "page_id": page.page_id,
        "revision_id": page.revision_id,
        "source_path": _relative(root, source),
        "quarantine_path": _relative(root, quarantine),
        "size": details.st_size,
        "expected_sha256": page.wikitext_sha256,
        "actual_sha256": _sha256_file(source),
        "reason": "cache_sha256_mismatch",
        "quarantined_at": clock.now().isoformat(),
    }
    _validate_manifest_item(
        root, run_id, expected_hashes, item, require_quarantine_file=False
    )
    atomic_write_json(
        intent_path,
        {"schema_version": 1, "model_type": "CacheQuarantineIntent", "run_id": run_id, "item": item},
    )
    _reconcile_cache_quarantine(root, run_id, run_snapshot)


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


def _page_file_chunks(root: Path, page: FrozenPage) -> tuple[object, ...]:
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
    return chunks


def _page_filenames(root: Path, page: FrozenPage) -> tuple[str, ...]:
    return tuple(sorted({chunk.filename for chunk in _page_file_chunks(root, page)}))


def _score_metadata_complete(root: Path, page: FrozenPage, scores: tuple[ScoreFile, ...]) -> bool:
    expected = set(_page_filenames(root, page))
    actual_scores = tuple(
        score for score in scores
        if score.page_id == page.page_id and score.page_revision_id == page.revision_id
    )
    actual = {score.filename for score in actual_scores}
    return actual == expected and len(actual_scores) == len(actual)


def _validate_score_bindings(root: Path, snapshot: RunSnapshot) -> None:
    pages = {(page.page_id, page.revision_id): page for page in snapshot.pages}
    if len(pages) != len(snapshot.pages):
        raise SnapshotError("snapshot contains duplicate frozen page identities")
    source_ids: set[str] = set()
    attachment_ids: set[tuple[int, int, str]] = set()
    file_ids: set[tuple[int, int, str]] = set()
    filenames_by_page: dict[tuple[int, int], set[str]] = {}
    chunks_by_page: dict[tuple[int, int], tuple[object, ...]] = {}
    manifest_path = root / "quarantine/manifests" / f"cache-{snapshot.run_id}.json"
    quarantined = {
        (item["page_id"], item["revision_id"])
        for item in _read_cache_manifest(
            root, manifest_path, snapshot.run_id, _expected_cache_hashes(snapshot)
        )
    }
    for score in snapshot.score_files:
        page_identity = (score.page_id, score.page_revision_id)
        page = pages.get(page_identity)
        if page is None:
            raise SnapshotError("score file does not bind an exact frozen page/revision")
        if score.source_id in source_ids:
            raise SnapshotError("score file source identity is not unique")
        source_ids.add(score.source_id)
        attachment_identity = (*page_identity, score.filename)
        file_identity = (*page_identity, score.file_id)
        if attachment_identity in attachment_ids or file_identity in file_ids:
            raise SnapshotError("score file attachment/file identity is not unique")
        attachment_ids.add(attachment_identity)
        file_ids.add(file_identity)
        cache = _cache_path(root, page)
        if cache.is_symlink():
            raise SnapshotError("checkpointed score file cache is a symlink")
        if not cache.exists():
            if page_identity in quarantined:
                continue
            raise SnapshotError("checkpointed score file has no exact revision cache")
        if not cache.is_file():
            raise SnapshotError("checkpointed score file cache is not a regular file")
        if _sha256_file(cache) == page.wikitext_sha256:
            if page_identity not in filenames_by_page:
                chunks = _page_file_chunks(root, page)
                chunks_by_page[page_identity] = chunks
                filenames_by_page[page_identity] = {chunk.filename for chunk in chunks}
            expected = filenames_by_page[page_identity]
            if score.filename not in expected:
                raise SnapshotError("score file filename is extra to exact cached revision")
            matching_chunks = tuple(
                chunk for chunk in chunks_by_page[page_identity]
                if chunk.filename == score.filename
            )
            explicit_ids = {chunk.file_id for chunk in matching_chunks if chunk.file_id is not None}
            if any(chunk.file_id_conflict for chunk in matching_chunks) or (
                explicit_ids and explicit_ids != {score.file_id}
            ):
                raise SnapshotError("score file ID conflicts with exact cached revision")


def _validate_complete_score_set(root: Path, snapshot: RunSnapshot) -> None:
    _validate_score_bindings(root, snapshot)
    for page in snapshot.pages:
        if not _score_metadata_complete(root, page, snapshot.score_files):
            raise SnapshotError("score files do not exactly equal cached revision attachments")


def _validate_category_page_bindings(snapshot: RunSnapshot, approved_names: set[str]) -> None:
    categories = {category.name: category for category in snapshot.categories}
    pages = {page.page_id: page for page in snapshot.pages}
    if len(categories) != len(snapshot.categories) or len(pages) != len(snapshot.pages):
        raise SnapshotError("snapshot category/page identities are not unique")
    if not set(categories).issubset(approved_names):
        raise SnapshotError("snapshot contains a category outside the config")
    expected_by_page: dict[int, set[str]] = {page_id: set() for page_id in pages}
    for category in categories.values():
        for page_id in category.member_page_ids:
            if page_id not in pages:
                raise SnapshotError("category snapshot references a missing frozen page")
            expected_by_page[page_id].add(category.name)
    for page_id, page in pages.items():
        if set(page.category_names) != expected_by_page[page_id]:
            raise SnapshotError("frozen page categories do not equal category snapshots")


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
    _assert_safe_read_target(root, snapshot_path, "run snapshot")
    _assert_safe_read_target(root, state_path, "run state")
    if snapshot_path.exists() != state_path.exists():
        raise SnapshotError("run snapshot and state must exist together")
    if not snapshot_path.exists():
        return None
    if snapshot_path.is_symlink() or state_path.is_symlink():
        raise SnapshotError("run snapshot and state cannot be symlinks")
    snapshot = _snapshot_from_path(root, snapshot_path)
    state = _state_from_path(root, state_path)
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
    authority_path = _checkpoint_authority_path(root, run_id)
    pending_paths = (
        (_initialization_intent_path(root, run_id), "snapshot initialization intent"),
        (_completion_intent_path(root, run_id), "snapshot completion intent"),
        (_checkpoint_transition_path(root, run_id), "checkpoint transition"),
    )
    for pending, label in pending_paths:
        _assert_safe_read_target(root, pending, label)
        if pending.exists() or pending.is_symlink():
            raise SnapshotError("unsettled snapshot lifecycle intent; call freeze_snapshot to recover")
    for path, label in (
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
        (authority_path, "checkpoint authority"),
    ):
        _assert_safe_read_target(root, path, label)
    if not snapshot_path.exists() or not state_path.exists():
        raise SnapshotError("run snapshot or state is missing")
    if snapshot_path.is_symlink() or state_path.is_symlink():
        raise SnapshotError("run snapshot and state cannot be symlinks")
    snapshot = _snapshot_from_path(root, snapshot_path)
    state = _state_from_path(root, state_path)
    if (
        snapshot.run_id != run_id
        or state.run_id != run_id
        or state.snapshot_path != _relative(root, snapshot_path)
    ):
        raise SnapshotError("run identity or snapshot path mismatch")
    if snapshot.status is not RunStatus.SNAPSHOT_COMPLETE or state.snapshot_sha256 is None:
        raise SnapshotError("snapshot is incomplete and cannot be used downstream")
    if authority_path.exists() or authority_path.is_symlink():
        raise SnapshotError("unsettled checkpoint authority on complete snapshot; call freeze_snapshot to recover")
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
    try:
        validate_library_config_binding(config)
    except ConfigError as exc:
        raise SnapshotError("config canonical source binding mismatch") from exc
    if not isinstance(config.config_hash, str) or _SHA256_RE.fullmatch(config.config_hash) is None:
        raise ValueError("config hash must be canonical SHA-256")
    if type(revision_batch_size) is not int or revision_batch_size <= 0:
        raise ValueError("revision_batch_size must be positive")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise SnapshotError("library root is a symlink or not a directory")
    root.mkdir(parents=True, exist_ok=True)
    snapshot_path, state_path = _paths(root, run_id)
    for path, label in (
        (_initialization_intent_path(root, run_id), "snapshot initialization intent"),
        (_completion_intent_path(root, run_id), "snapshot completion intent"),
        (_checkpoint_authority_path(root, run_id), "checkpoint authority"),
        (_checkpoint_transition_path(root, run_id), "checkpoint transition"),
        (snapshot_path, "run snapshot"),
        (state_path, "run state"),
    ):
        _assert_safe_write_target(root, path, label)
    _reconcile_initialization(root, run_id, config, snapshot_path, state_path)
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
        # The intent binds the initial models and checkpoint authority before
        # any of them can precede the first possible client request alone.
        _write_initial_pair(root, run_id, config, snapshot_path, state_path, snapshot, state)
    else:
        snapshot, state = existing
    snapshot, state = _reconcile_completion(
        root, run_id, config, snapshot_path, state_path, snapshot, state
    )
    if snapshot.status is RunStatus.SNAPSHOT_COMPLETE:
        for pending in (
            _checkpoint_authority_path(root, run_id),
            _checkpoint_transition_path(root, run_id),
        ):
            if pending.exists() or pending.is_symlink():
                raise SnapshotError("complete snapshot has unsettled checkpoint authority")
        actual = _sha256_file(snapshot_path)
        if state.snapshot_sha256 != actual:
            raise SnapshotError("completed snapshot SHA-256 does not match RunState")
        _reconcile_cache_quarantine(root, run_id, snapshot)
        _validate_complete_score_set(root, snapshot)
        return snapshot
    if snapshot.status is not RunStatus.SNAPSHOT_INCOMPLETE or state.snapshot_sha256 is not None:
        raise SnapshotError("run cannot resume from its current state")
    snapshot, state, _, _ = _reconcile_checkpoint_transition(
        root, run_id, config, snapshot_path, state_path
    )

    _reconcile_cache_quarantine(root, run_id, snapshot)
    _validate_score_bindings(root, snapshot)

    category_by_name = {category.name: category for category in snapshot.categories}
    pages_by_id = {page.page_id: page for page in snapshot.pages}
    if len(pages_by_id) != len(snapshot.pages):
        raise SnapshotError("snapshot contains duplicate page identities")
    approved_names = {category.name for category in config.categories}
    _validate_category_page_bindings(snapshot, approved_names)

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
        state = _checkpoint(
            root, run_id, config, snapshot_path, state_path, snapshot, state, clock.now()
        )

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
        state = _checkpoint(
            root, run_id, config, snapshot_path, state_path, snapshot, state, clock.now()
        )

    if set(category_by_name) != approved_names:
        raise SnapshotError("not all configured categories were checkpointed")
    for page in ordered_pages:
        if not _cache_is_valid(root, run_id, page, clock) or not _score_metadata_complete(root, page, snapshot.score_files):
            raise SnapshotError("exact revisions and file metadata are incomplete")

    _validate_complete_score_set(root, snapshot)

    completed = replace(
        snapshot,
        status=RunStatus.SNAPSHOT_COMPLETE,
        snapshot_completed_at=clock.now(),
    )
    snapshot_digest = _sha256_bytes(_canonical_bytes(completed.to_dict()))
    final_state = replace(state, snapshot_sha256=snapshot_digest, updated_at=clock.now())
    _commit_completion(root, run_id, config, snapshot_path, state_path, completed, final_state)
    return completed
