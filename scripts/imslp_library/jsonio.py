from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .models import Model, model_class_for_name, model_from_dict

_ENVELOPE_KEYS = {"schema_version", "model_type", "items"}


def _schema_version(mapping: Mapping[str, object]) -> int | None:
    if "schema_version" not in mapping:
        return None
    value = mapping["schema_version"]
    if type(value) is not int or value <= 0:
        raise ValueError("schema_version must be a positive integer")
    return value


def read_json(path: str | os.PathLike[str]) -> dict[str, object]:
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise
    if not isinstance(payload, dict):
        raise ValueError("JSON document must be a mapping")
    if not all(isinstance(key, str) for key in payload):
        raise ValueError("JSON mapping keys must be strings")
    return payload


def _reject_downgrade(path: Path, incoming: Mapping[str, object]) -> None:
    incoming_version = _schema_version(incoming)
    if not path.exists():
        return
    existing_version = _schema_version(read_json(path))
    if existing_version is not None and incoming_version is None:
        raise ValueError(f"schema downgrade rejected: existing={existing_version}, incoming=missing")
    if existing_version is not None and incoming_version is not None and existing_version > incoming_version:
        raise ValueError(f"schema downgrade rejected: existing={existing_version}, incoming={incoming_version}")


def _manifest_item_class(manifest_type: str) -> type[Model]:
    if not isinstance(manifest_type, str) or not manifest_type.endswith("Manifest"):
        raise ValueError(f"unknown manifest model_type: {manifest_type}")
    item_type = manifest_type.removesuffix("Manifest")
    try:
        return model_class_for_name(item_type)
    except ValueError as exc:
        raise ValueError(f"unknown manifest model_type: {manifest_type}") from exc


def _read_models_envelope(path: str | os.PathLike[str]) -> tuple[str, tuple[Model, ...]]:
    envelope = read_json(path)
    if set(envelope) != _ENVELOPE_KEYS:
        missing = sorted(_ENVELOPE_KEYS - set(envelope))
        extra = sorted(set(envelope) - _ENVELOPE_KEYS)
        raise ValueError(f"invalid envelope keys; missing={missing}, extra={extra}")
    _schema_version(envelope)
    model_type = envelope["model_type"]
    if not isinstance(model_type, str):
        raise TypeError("model_type must be a string")
    expected_item_class = _manifest_item_class(model_type)
    raw_items = envelope["items"]
    if not isinstance(raw_items, list):
        raise TypeError("items must be an array")
    items = tuple(model_from_dict(item) for item in raw_items)
    if any(type(item) is not expected_item_class for item in items):
        raise ValueError(f"{model_type} item model must be {expected_item_class.__name__}")
    return model_type, items


def _canonical_bytes(mapping: Mapping[str, object]) -> bytes:
    if not all(isinstance(key, str) for key in mapping):
        raise TypeError("mapping keys must be strings")
    try:
        text = json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError("mapping must contain JSON-serializable values") from exc
    return (text + "\n").encode("utf-8")


def atomic_write_json(path: str | os.PathLike[str], mapping: Mapping[str, object]) -> None:
    if not isinstance(mapping, Mapping):
        raise TypeError("atomic_write_json requires a mapping")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _schema_version(mapping)
    _reject_downgrade(target, mapping)
    content = _canonical_bytes(mapping)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise


def atomic_write_models(
    path: str | os.PathLike[str],
    model_type: str,
    items: object,
    schema_version: int = 1,
) -> None:
    if not isinstance(model_type, str) or not model_type.strip():
        raise ValueError("model_type must be a nonempty string")
    expected_item_class = _manifest_item_class(model_type)
    if type(schema_version) is not int or schema_version <= 0:
        raise ValueError("schema_version must be a positive integer")
    if isinstance(items, (str, bytes, Mapping)):
        raise TypeError("items must be an iterable of typed models")
    try:
        values = tuple(items)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("items must be an iterable of typed models") from exc
    if not all(isinstance(item, Model) for item in values):
        raise TypeError("items must contain only typed models")
    if any(type(item) is not expected_item_class for item in values):
        raise TypeError(f"{model_type} item model must be {expected_item_class.__name__}")
    envelope: dict[str, object] = {
        "schema_version": schema_version,
        "model_type": model_type,
        "items": [item.to_dict() for item in values],
    }
    target = Path(path)
    if target.exists():
        existing_model_type, _ = _read_models_envelope(target)
        if existing_model_type != model_type:
            raise ValueError(f"existing model_type {existing_model_type} does not match incoming {model_type}")
    atomic_write_json(path, envelope)


def read_models(path: str | os.PathLike[str], expected_model_type: str) -> tuple[Model, ...]:
    if not isinstance(expected_model_type, str) or not expected_model_type.strip():
        raise ValueError("expected_model_type must be a nonempty string")
    _manifest_item_class(expected_model_type)
    model_type, items = _read_models_envelope(path)
    if model_type != expected_model_type:
        raise ValueError(f"model_type must be {expected_model_type}")
    return items
