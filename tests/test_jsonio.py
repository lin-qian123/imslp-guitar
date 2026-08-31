from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from imslp_library import jsonio
from tests import model_helpers as h


def test_atomic_write_json_is_canonical_unicode_mapping(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = {"schema_version": 1, "z": "吉他", "a": {"é": 1}}
    jsonio.atomic_write_json(path, payload)
    expected = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    assert path.read_text(encoding="utf-8") == expected
    assert jsonio.read_json(path) == payload
    jsonio.atomic_write_json(path, payload)
    assert path.read_text(encoding="utf-8") == expected


def test_mapping_write_and_read_reject_lists_and_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        jsonio.atomic_write_json(tmp_path / "list.json", [])
    list_path = tmp_path / "read-list.json"
    list_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        jsonio.read_json(list_path)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        jsonio.read_json(invalid)


def test_mapping_and_manifest_schema_downgrade_are_rejected(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.json"
    jsonio.atomic_write_json(mapping, {"schema_version": 2, "value": "old"})
    with pytest.raises(ValueError, match="downgrade"):
        jsonio.atomic_write_json(mapping, {"schema_version": 1, "value": "new"})
    manifest = tmp_path / "manifest.json"
    jsonio.atomic_write_models(manifest, "MembershipManifest", (h.make_membership(),), schema_version=2)
    with pytest.raises(ValueError, match="downgrade"):
        jsonio.atomic_write_models(manifest, "MembershipManifest", (h.make_membership(),), schema_version=1)


def test_models_manifest_has_exact_envelope_and_typed_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "memberships.json"
    items = (h.make_membership(), h.make_membership(category="For 2 guitars (arr)"))
    jsonio.atomic_write_models(path, "MembershipManifest", items)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"schema_version", "model_type", "items"}
    assert raw["schema_version"] == 1
    assert raw["model_type"] == "MembershipManifest"
    assert [item["model_type"] for item in raw["items"]] == ["Membership", "Membership"]
    assert jsonio.read_models(path, "MembershipManifest") == items


@pytest.mark.parametrize("mutation", [
    lambda p: p.pop("items"), lambda p: p.__setitem__("extra", 1),
    lambda p: p.__setitem__("schema_version", "1"), lambda p: p.__setitem__("model_type", 1),
    lambda p: p.__setitem__("items", {}), lambda p: p.__setitem__("items", [{"model_type": "Unknown"}]),
])
def test_read_models_strictly_rejects_bad_envelopes(tmp_path: Path, mutation) -> None:
    path = tmp_path / "bad.json"
    payload = {"schema_version": 1, "model_type": "MembershipManifest", "items": [h.make_membership().to_dict()]}
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((TypeError, ValueError)):
        jsonio.read_models(path, "MembershipManifest")


def test_read_models_rejects_unexpected_manifest_type(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    jsonio.atomic_write_models(path, "MembershipManifest", (h.make_membership(),))
    with pytest.raises(ValueError, match="model_type"):
        jsonio.read_models(path, "ScoreManifest")


def test_replace_failure_preserves_old_target_and_cleans_only_own_temp(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    jsonio.atomic_write_json(path, {"schema_version": 1, "value": "old"})
    foreign = tmp_path / f".{path.name}.foreign.tmp"
    foreign.write_text("foreign", encoding="utf-8")
    monkeypatch.setattr(jsonio.os, "replace", Mock(side_effect=OSError("injected")))
    with pytest.raises(OSError, match="injected"):
        jsonio.atomic_write_json(path, {"schema_version": 1, "value": "new"})
    assert jsonio.read_json(path)["value"] == "old"
    assert foreign.read_text(encoding="utf-8") == "foreign"
    assert sorted(tmp_path.glob(f".{path.name}.*.tmp")) == [foreign]


def test_successful_write_fsyncs_file_and_parent_then_replaces(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    real_fsync = jsonio.os.fsync
    monkeypatch.setattr(jsonio.os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    path = tmp_path / "state.json"
    jsonio.atomic_write_json(path, {"value": "new"})
    assert jsonio.read_json(path) == {"value": "new"}
    assert len(calls) == 2
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))
