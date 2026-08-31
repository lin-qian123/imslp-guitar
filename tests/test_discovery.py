from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import imslp_library.discovery as discovery_module
from imslp_library.client import CategoryInfo, ImslpClient, ImslpClientError
from imslp_library.config import load_allowlist
from imslp_library.discovery import DiscoveryResult, discover_category_drift
from imslp_library.enums import RunStatus
from imslp_library.jsonio import atomic_write_json, read_json
from imslp_library.models import CategorySnapshot, RunSnapshot, RunState
from imslp_library.snapshot import SnapshotError
from tests.basic_helpers import ROOT
from tests.network_helpers import FakeClock, FakeTransport, json_response


FIXTURES = ROOT / "tests/fixtures/api"
RUN_ID = "run-20260831T120000Z"
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _fixture_pages() -> list[dict[str, object]]:
    payload = json.loads((FIXTURES / "allcategories-guitar.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict) and isinstance(payload.get("pages"), list)
    return payload["pages"]


def _categoryinfo(name: str, size: int, *, missing: bool = False) -> dict[str, object]:
    page: dict[str, object] = {"ns": 14, "title": f"Category:{name}"}
    if missing:
        page["missing"] = True
    else:
        page |= {"pageid": 900 + size, "categoryinfo": {"pages": size, "files": 0, "subcats": 0}}
    return {"query": {"pages": [page]}}


def _members(name: str, ids: tuple[int, ...]) -> dict[str, object]:
    return {
        "query": {
            "categorymembers": [
                {"pageid": page_id, "ns": 0, "title": f"{name} work {page_id}"}
                for page_id in ids
            ]
        }
    }


def _members_page(
    name: str, ids: tuple[int, ...], *, continuation: str | None = None
) -> dict[str, object]:
    payload = _members(name, ids)
    if continuation is not None:
        payload["continue"] = {"continue": "-||", "cmcontinue": continuation}
    return payload


def _write_allowlist(path: Path) -> bytes:
    payload = {
        "schema_version": 1,
        "version": "test-allowlist-1",
        "approved_at": "2026-08-30",
        "categories": [
            {
                "name": "For 3 guitars (arr)",
                "url": "https://imslp.org/wiki/Category:For_3_guitars_(arr)",
                "kind": "arrangement",
                "display_group": "multi_guitar",
                "guitar_count": 3,
                "extended_strings": None,
                "instrumentation_patterns": ["^3 guitars$"],
                "heading_patterns": ["^For 3 Guitars$"],
                "annotation_reject_tokens": ["bass", "voice"],
            },
            {
                "name": "For guitar",
                "url": "https://imslp.org/wiki/Category:For_guitar",
                "kind": "original",
                "display_group": "solo",
                "guitar_count": 1,
                "extended_strings": None,
                "instrumentation_patterns": ["^guitar$"],
                "heading_patterns": [],
                "annotation_reject_tokens": ["bass", "voice"],
            },
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path.read_bytes()


def _write_complete_run(root: Path, config_hash: str) -> tuple[Path, Path]:
    snapshot_path = root / f"metadata/runs/{RUN_ID}.json"
    state_path = root / f"metadata/runs/{RUN_ID}-state.json"
    snapshot = RunSnapshot(
        schema_version=1,
        run_id=RUN_ID,
        config_version="test-allowlist-1",
        config_sha256=config_hash,
        snapshot_started_at=NOW,
        snapshot_completed_at=NOW,
        status=RunStatus.SNAPSHOT_COMPLETE,
        categories=(
            CategorySnapshot("For 3 guitars (arr)", (1, 2, 3, 4, 5), 5),
            CategorySnapshot("For guitar", (20, 21), 2),
        ),
        pages=(),
        score_files=(),
    )
    atomic_write_json(snapshot_path, snapshot.to_dict())
    snapshot_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    state = RunState(
        schema_version=1,
        run_id=RUN_ID,
        snapshot_path=f"metadata/runs/{RUN_ID}.json",
        snapshot_sha256=snapshot_hash,
        start_drift_report_path=None,
        start_drift_report_sha256=None,
        end_drift_report_path=None,
        end_drift_report_sha256=None,
        download_attempt_manifest_path=None,
        updated_at=NOW,
    )
    atomic_write_json(state_path, state.to_dict())
    return snapshot_path, state_path


def _drift_report(phase: str, **overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at": NOW.isoformat(),
        "phase": phase,
        "compare_run_id": RUN_ID,
        "allowlist_version": "test-allowlist-1",
        "allowlist_sha256": "0" * 64,
        "new_candidates": [],
        "empty_categories": [],
        "deleted_categories": [],
        "member_count_changes": [],
        "possible_renames": [],
        "filtered_candidates": [],
    }
    report.update(overrides)
    return report


def _bind_existing_drift(
    root: Path,
    state_path: Path,
    config_hash: str,
    *,
    stem: str = "start",
    path_override: str | None = None,
    report_overrides: dict[str, object] | None = None,
    digest_override: str | None = None,
    symlink_leaf: bool = False,
    directory_leaf: bool = False,
) -> Path:
    relative = path_override or f"metadata/runs/{RUN_ID}-category-drift-{stem}.json"
    report_path = root / relative
    report = _drift_report(f"run_{stem}", allowlist_sha256=config_hash)
    report.update(report_overrides or {})
    if directory_leaf:
        report_path.mkdir()
        digest = "0" * 64
    elif symlink_leaf:
        outside = root.parent / f"outside-{stem}-drift.json"
        atomic_write_json(outside, report)
        report_path.symlink_to(outside)
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    else:
        atomic_write_json(report_path, report)
        digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    state = RunState.from_dict(read_json(state_path))
    state = replace(
        state,
        **{
            f"{stem}_drift_report_path": relative,
            f"{stem}_drift_report_sha256": digest_override or digest,
        },
    )
    atomic_write_json(state_path, state.to_dict())
    return report_path


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _base_responses() -> list[object]:
    first, second = _fixture_pages()
    return [
        json_response(first),
        json_response(second),
        json_response(_categoryinfo("For 3 guitars (arr)", 0)),
        json_response(_categoryinfo("For guitar", 3)),
    ]


def _run_responses() -> list[object]:
    first, second = _fixture_pages()
    return [
        json_response(first),
        json_response(second),
        json_response(_categoryinfo("For 3 guitars (arr)", 0)),
        json_response(_categoryinfo("For guitar", 3)),
        json_response(_members("For 3 guitars", (1, 2, 8, 9))),
        json_response(_members("For 3-guitars (arr)", (1, 2, 3, 4))),
        json_response(_members("For 4 guitars", (30, 31, 32, 33, 34, 35))),
    ]


def _strict_run_report(config_hash: str) -> dict[str, object]:
    return _drift_report(
        "run_start",
        allowlist_sha256=config_hash,
        new_candidates=[
            {
                "name": "For 3-guitars (arr)",
                "size": 4,
                "page_count": 4,
                "file_count": 0,
                "subcategory_count": 0,
            }
        ],
        empty_categories=[
            {"name": "For 3 guitars (arr)", "previous_member_count": 5}
        ],
        member_count_changes=[
            {
                "name": "For 3 guitars (arr)",
                "previous_member_count": 5,
                "current_member_count": 0,
            }
        ],
        possible_renames=[
            {
                "source_name": "For 3 guitars (arr)",
                "candidate_name": "For 3-guitars (arr)",
                "name_distance": 0,
                "jaccard_overlap": 0.8,
                "source_member_count": 5,
                "candidate_member_count": 4,
            }
        ],
    )


def _canonical_test_bytes(mapping: dict[str, object]) -> bytes:
    return (
        json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def test_collects_paginated_candidates_and_preserves_filter_evidence(tmp_path: Path) -> None:
    config_path = tmp_path / "config/categories.json"
    original_config = _write_allowlist(config_path)
    transport = FakeTransport(_base_responses())

    result = discover_category_drift(
        tmp_path,
        load_allowlist(config_path),
        ImslpClient(transport=transport, clock=FakeClock(NOW)),
        FakeClock(NOW),
    )

    assert isinstance(result, DiscoveryResult)
    assert result.report_path == "metadata/category_drift_report.json"
    assert result.report["new_candidates"] == [
        {
            "file_count": 0,
            "name": "For 3 guitars",
            "page_count": 4,
            "size": 4,
            "subcategory_count": 0,
        },
        {
            "file_count": 0,
            "name": "For 3-guitars (arr)",
            "page_count": 4,
            "size": 4,
            "subcategory_count": 0,
        },
        {
            "file_count": 0,
            "name": "For 4 guitars",
            "page_count": 6,
            "size": 6,
            "subcategory_count": 0,
        },
    ]
    filtered = {item["name"]: item["reason"] for item in result.report["filtered_candidates"]}
    assert filtered == {
        "For 2 electric guitars": "electric_guitar",
        "For bass guitar (arr)": "bass_guitar",
        "For guitar, voice": "mixed_instrumentation",
        "For guitar and piano": "mixed_instrumentation",
        "For guitar?search=bad": "malformed_name",
        "For guitar orchestra (arr)": "zero_members",
        "For lute": "not_guitar",
    }
    assert all(
        set(item) == {
            "name",
            "size",
            "page_count",
            "file_count",
            "subcategory_count",
            "reason",
        }
        for item in result.report["filtered_candidates"]
    )
    assert _query(transport.calls[0].url)["acprefix"] == ["For"]
    assert _query(transport.calls[0].url)["acprop"] == ["size"]
    assert _query(transport.calls[1].url)["accontinue"] == ["For 4 guitars"]
    assert [_query(call.url).get("titles") for call in transport.calls[2:]] == [
        ["Category:For 3 guitars (arr)"],
        ["Category:For guitar"],
    ]
    assert config_path.read_bytes() == original_config
    assert (
        hashlib.sha256((tmp_path / result.report_path).read_bytes()).hexdigest()
        == result.report_sha256
    )


def test_reports_empty_deleted_and_live_member_count_changes_without_fixed_counts(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _write_complete_run(tmp_path, config.config_hash)
    first, second = _fixture_pages()
    transport = FakeTransport([
        json_response(first),
        json_response(second),
        json_response(_categoryinfo("For 3 guitars (arr)", 0)),
        json_response(_categoryinfo("For guitar", 0, missing=True)),
        json_response(_members("For 3 guitars", (1, 2, 8, 9))),
        json_response(_members("For 3-guitars (arr)", (1, 2, 3, 4))),
        json_response(_members("For 4 guitars", (30, 31, 32, 33, 34, 35))),
    ])

    report = discover_category_drift(
        tmp_path,
        config,
        ImslpClient(transport=transport, clock=FakeClock(NOW)),
        FakeClock(NOW),
        phase="run_start",
        compare_run_id=RUN_ID,
    ).report

    assert report["empty_categories"] == [
        {"name": "For 3 guitars (arr)", "previous_member_count": 5}
    ]
    assert report["deleted_categories"] == [
        {"name": "For guitar", "previous_member_count": 2}
    ]
    assert report["member_count_changes"] == [
        {"current_member_count": 0, "name": "For 3 guitars (arr)", "previous_member_count": 5},
        {"current_member_count": 0, "name": "For guitar", "previous_member_count": 2},
    ]


def test_rename_requires_both_thresholds_and_fetches_every_new_candidate(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _write_complete_run(tmp_path, config.config_hash)
    first, second = _fixture_pages()
    transport = FakeTransport([
        json_response(first),
        json_response(second),
        json_response(_categoryinfo("For 3 guitars (arr)", 0)),
        json_response(_categoryinfo("For guitar", 3)),
        json_response(_members_page("For 3 guitars", (1, 2), continuation="next-members")),
        json_response(_members("For 3 guitars", (8, 9))),
        json_response(_members("For 3-guitars (arr)", (1, 2, 3, 4))),
        json_response(_members("For 4 guitars", (1, 2, 3, 4))),
    ])

    report = discover_category_drift(
        tmp_path,
        config,
        ImslpClient(transport=transport, clock=FakeClock(NOW)),
        FakeClock(NOW),
        phase="run_start",
        compare_run_id=RUN_ID,
    ).report

    assert report["possible_renames"] == [
        {
            "candidate_member_count": 4,
            "candidate_name": "For 3-guitars (arr)",
            "jaccard_overlap": 0.8,
            "name_distance": 0,
            "source_member_count": 5,
            "source_name": "For 3 guitars (arr)",
        }
    ]
    assert {item["name"] for item in report["new_candidates"]} == {
        "For 3 guitars",
        "For 3-guitars (arr)",
        "For 4 guitars",
    }
    categorymember_titles = [
        _query(call.url)["cmtitle"][0]
        for call in transport.calls
        if _query(call.url).get("list") == ["categorymembers"]
    ]
    assert categorymember_titles == [
        "Category:For 3 guitars",
        "Category:For 3 guitars",
        "Category:For 3-guitars (arr)",
        "Category:For 4 guitars",
    ]
    member_calls = [
        call
        for call in transport.calls
        if _query(call.url).get("list") == ["categorymembers"]
    ]
    assert "cmcontinue" not in _query(member_calls[0].url)
    assert _query(member_calls[1].url)["cmcontinue"] == ["next-members"]


def test_standalone_rejects_compare_run_and_run_phases_require_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    client = ImslpClient(transport=FakeTransport([]), clock=FakeClock(NOW))

    with pytest.raises(ValueError, match="compare_run_id"):
        discover_category_drift(tmp_path, config, client, FakeClock(NOW), compare_run_id=RUN_ID)
    with pytest.raises(ValueError, match="compare_run_id"):
        discover_category_drift(tmp_path, config, client, FakeClock(NOW), phase="run_start")
    with pytest.raises(ValueError, match="phase"):
        discover_category_drift(tmp_path, config, client, FakeClock(NOW), phase="start")


def test_run_reports_are_immutable_and_bound_atomically_to_state(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config/categories.json"
    config_bytes = _write_allowlist(config_path)
    config = load_allowlist(config_path)
    snapshot_path, state_path = _write_complete_run(tmp_path, config.config_hash)
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_mtime = snapshot_path.stat().st_mtime_ns

    def run_phase(phase: str) -> DiscoveryResult:
        first, second = _fixture_pages()
        responses = [
            json_response(first),
            json_response(second),
            json_response(_categoryinfo("For 3 guitars (arr)", 0)),
            json_response(_categoryinfo("For guitar", 3)),
            json_response(_members("For 3 guitars", (1, 2, 8, 9))),
            json_response(_members("For 3-guitars (arr)", (1, 2, 3, 4))),
            json_response(_members("For 4 guitars", (30, 31, 32, 33, 34, 35))),
        ]
        return discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=FakeTransport(responses), clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase=phase,
            compare_run_id=RUN_ID,
        )

    start = run_phase("run_start")
    end = run_phase("run_end")
    start_path = tmp_path / start.report_path
    end_path = tmp_path / end.report_path
    alias = read_json(tmp_path / "metadata/category_drift_report.json")
    state = RunState.from_dict(read_json(state_path))

    assert start.report["compare_run_id"] == end.report["compare_run_id"] == RUN_ID
    assert start.report["phase"] == "run_start" and end.report["phase"] == "run_end"
    assert start_path.is_file() and end_path.is_file() and start_path != end_path
    assert (
        hashlib.sha256(start_path.read_bytes()).hexdigest()
        == state.start_drift_report_sha256
        == start.report_sha256
    )
    assert (
        hashlib.sha256(end_path.read_bytes()).hexdigest()
        == state.end_drift_report_sha256
        == end.report_sha256
    )
    assert state.start_drift_report_path == start.report_path
    assert state.end_drift_report_path == end.report_path
    assert alias == end.report
    assert snapshot_path.read_bytes() == snapshot_bytes
    assert snapshot_path.stat().st_mtime_ns == snapshot_mtime
    assert config_path.read_bytes() == config_bytes
    assert state.snapshot_sha256 == hashlib.sha256(snapshot_bytes).hexdigest()

    with pytest.raises(FileExistsError):
        run_phase("run_start")
    assert start_path.read_bytes() == (tmp_path / start.report_path).read_bytes()


def test_report_has_only_the_exact_public_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    result = discover_category_drift(
        tmp_path,
        load_allowlist(config_path),
        ImslpClient(transport=FakeTransport(_base_responses()), clock=FakeClock(NOW)),
        FakeClock(NOW),
    )

    assert set(result.report) == {
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
    assert result.report["generated_at"] == NOW.isoformat()
    assert result.report["phase"] == "standalone"
    assert result.report["compare_run_id"] is None


def test_same_standalone_inputs_produce_identical_canonical_bytes(tmp_path: Path) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)

    first = discover_category_drift(
        tmp_path,
        config,
        ImslpClient(transport=FakeTransport(_base_responses()), clock=FakeClock(NOW)),
        FakeClock(NOW),
    )
    first_bytes = (tmp_path / first.report_path).read_bytes()
    second = discover_category_drift(
        tmp_path,
        config,
        ImslpClient(transport=FakeTransport(_base_responses()), clock=FakeClock(NOW)),
        FakeClock(NOW),
    )

    assert (tmp_path / second.report_path).read_bytes() == first_bytes
    assert second.report_sha256 == first.report_sha256


def test_malformed_api_item_fails_closed_without_writing_a_report(tmp_path: Path) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    malformed = {"query": {"allcategories": [{"category": "For guitar", "size": "7"}]}}
    client = ImslpClient(
        transport=FakeTransport([json_response(malformed)]),
        clock=FakeClock(NOW),
    )

    with pytest.raises(ImslpClientError, match="malformed"):
        discover_category_drift(
            tmp_path,
            load_allowlist(config_path),
            client,
            FakeClock(NOW),
        )
    assert not (tmp_path / "metadata/category_drift_report.json").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_path",
        "wrong_hash",
        "wrong_phase",
        "wrong_run",
        "extra_key",
        "symlink_leaf",
        "directory_leaf",
    ],
)
def test_run_end_rejects_forged_existing_start_pointer_before_network(
    tmp_path: Path,
    mutation: str,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _, state_path = _write_complete_run(tmp_path, config.config_hash)
    arguments: dict[str, object] = {}
    if mutation == "wrong_path":
        arguments["path_override"] = "metadata/runs/forged-start.json"
    elif mutation == "wrong_hash":
        arguments["digest_override"] = "f" * 64
    elif mutation == "wrong_phase":
        arguments["report_overrides"] = {"phase": "run_end"}
    elif mutation == "wrong_run":
        arguments["report_overrides"] = {"compare_run_id": "run-forged"}
    elif mutation == "extra_key":
        arguments["report_overrides"] = {"unexpected": True}
    elif mutation == "symlink_leaf":
        arguments["symlink_leaf"] = True
    elif mutation == "directory_leaf":
        arguments["directory_leaf"] = True
    _bind_existing_drift(tmp_path, state_path, config.config_hash, **arguments)
    transport = FakeTransport([])

    with pytest.raises(SnapshotError, match="drift|report|symlink"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_end",
            compare_run_id=RUN_ID,
        )
    assert transport.calls == []


@pytest.mark.parametrize("direct_size", [None, 0, 4])
def test_present_allowlisted_category_rejects_direct_info_conflict(
    tmp_path: Path,
    direct_size: int | None,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    first, second = _fixture_pages()
    assert isinstance(first["query"], dict)
    first["query"]["allcategories"].append(
        {"category": "For guitar", "size": 3, "pages": 3, "files": 0, "subcats": 0}
    )
    guitar_info = (
        _categoryinfo("For guitar", 0, missing=True)
        if direct_size is None
        else _categoryinfo("For guitar", direct_size)
    )
    transport = FakeTransport([
        json_response(first),
        json_response(second),
        json_response(_categoryinfo("For 3 guitars (arr)", 0)),
        json_response(guitar_info),
    ])

    with pytest.raises(ValueError, match="allcategories.*categoryinfo"):
        discover_category_drift(
            tmp_path,
            load_allowlist(config_path),
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
        )
    assert not (tmp_path / "metadata/category_drift_report.json").exists()


def test_present_allowlisted_category_with_matching_counts_is_not_empty(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    first, second = _fixture_pages()
    assert isinstance(first["query"], dict)
    first["query"]["allcategories"].append(
        {"category": "For guitar", "size": 3, "pages": 3, "files": 0, "subcats": 0}
    )
    transport = FakeTransport([
        json_response(first),
        json_response(second),
        json_response(_categoryinfo("For 3 guitars (arr)", 0)),
        json_response(_categoryinfo("For guitar", 3)),
    ])

    report = discover_category_drift(
        tmp_path,
        load_allowlist(config_path),
        ImslpClient(transport=transport, clock=FakeClock(NOW)),
        FakeClock(NOW),
    ).report

    assert report["empty_categories"] == [
        {"name": "For 3 guitars (arr)", "previous_member_count": None}
    ]
    assert all(item["name"] != "For guitar" for item in report["empty_categories"])


@pytest.mark.parametrize("phase", ["standalone", "run_start"])
def test_preexisting_alias_symlink_is_rejected_before_network(
    tmp_path: Path,
    phase: str,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    if phase == "run_start":
        _write_complete_run(tmp_path, config.config_hash)
    alias = tmp_path / "metadata/category_drift_report.json"
    alias.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path.parent / f"outside-alias-{phase}.json"
    outside.write_text('{"sentinel":true}\n', encoding="utf-8")
    alias.symlink_to(outside)
    transport = FakeTransport([])

    arguments = {"phase": phase}
    if phase == "run_start":
        arguments["compare_run_id"] = RUN_ID
    with pytest.raises(ValueError, match="alias.*symlink"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
            **arguments,
        )
    assert transport.calls == []
    assert outside.read_text(encoding="utf-8") == '{"sentinel":true}\n'


def test_alias_symlink_swap_during_discovery_is_rejected_before_write(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    alias = tmp_path / "metadata/category_drift_report.json"
    outside = tmp_path.parent / "outside-alias-swap.json"
    outside.write_text('{"sentinel":true}\n', encoding="utf-8")

    class AliasSwapClient:
        def allcategories(self, *, prefix: str = ""):
            assert prefix == "For"
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(outside)
            return ()

        def category_info(self, name: str) -> CategoryInfo:
            return CategoryInfo(name, 1, 0, 0)

    with pytest.raises(ValueError, match="alias.*symlink"):
        discover_category_drift(
            tmp_path,
            load_allowlist(config_path),
            AliasSwapClient(),  # type: ignore[arg-type]
            FakeClock(NOW),
        )
    assert outside.read_text(encoding="utf-8") == '{"sentinel":true}\n'


def test_existing_pointer_is_revalidated_under_lock_after_network(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _, state_path = _write_complete_run(tmp_path, config.config_hash)
    start_path = _bind_existing_drift(tmp_path, state_path, config.config_hash)

    class PointerTamperClient:
        def allcategories(self, *, prefix: str = ""):
            assert prefix == "For"
            report = read_json(start_path)
            report["generated_at"] = datetime(2026, 8, 31, 12, 1, tzinfo=timezone.utc).isoformat()
            atomic_write_json(start_path, report)
            return ()

        def category_info(self, name: str) -> CategoryInfo:
            return CategoryInfo(name, 1, 0, 0)

    with pytest.raises(SnapshotError, match="drift report SHA-256"):
        discover_category_drift(
            tmp_path,
            config,
            PointerTamperClient(),  # type: ignore[arg-type]
            FakeClock(NOW),
            phase="run_end",
            compare_run_id=RUN_ID,
        )
    assert not (tmp_path / f"metadata/runs/{RUN_ID}-category-drift-end.json").exists()
    state = RunState.from_dict(read_json(state_path))
    assert state.end_drift_report_path is None


@pytest.mark.parametrize(
    "crash_stage",
    ["after_wal", "after_immutable", "after_alias", "after_state", "before_cleanup"],
)
def test_run_transition_recovers_every_crash_window_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_stage: str,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _write_complete_run(tmp_path, config.config_hash)

    def fail_at(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError(f"injected crash at {stage}")

    monkeypatch.setattr(discovery_module, "_transition_stage", fail_at)
    with pytest.raises(RuntimeError, match="injected crash"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=FakeTransport(_run_responses()), clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    transition_path = (
        tmp_path / f"metadata/runs/{RUN_ID}-category-drift-start-transition.json"
    )
    assert transition_path.is_file()
    intent = read_json(transition_path)
    expected_report = intent["report"]
    expected_hash = intent["report_sha256"]

    monkeypatch.setattr(discovery_module, "_transition_stage", lambda stage: None)
    transport = FakeTransport([])
    result = discover_category_drift(
        tmp_path,
        config,
        ImslpClient(transport=transport, clock=FakeClock(NOW)),
        FakeClock(NOW),
        phase="run_start",
        compare_run_id=RUN_ID,
    )

    assert transport.calls == []
    assert result.report == expected_report
    assert result.report_sha256 == expected_hash
    assert result.report_path == f"metadata/runs/{RUN_ID}-category-drift-start.json"
    assert not transition_path.exists()
    state = RunState.from_dict(
        read_json(tmp_path / f"metadata/runs/{RUN_ID}-state.json")
    )
    assert state.start_drift_report_sha256 == expected_hash


def test_run_end_transition_recovers_with_existing_start_alias_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _write_complete_run(tmp_path, config.config_hash)
    start = discover_category_drift(
        tmp_path,
        config,
        ImslpClient(transport=FakeTransport(_run_responses()), clock=FakeClock(NOW)),
        FakeClock(NOW),
        phase="run_start",
        compare_run_id=RUN_ID,
    )

    def crash_after_alias(stage: str) -> None:
        if stage == "after_alias":
            raise RuntimeError("injected crash after end alias")

    monkeypatch.setattr(discovery_module, "_transition_stage", crash_after_alias)
    with pytest.raises(RuntimeError, match="injected crash after end alias"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=FakeTransport(_run_responses()), clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_end",
            compare_run_id=RUN_ID,
        )

    transition_path = (
        tmp_path / f"metadata/runs/{RUN_ID}-category-drift-end-transition.json"
    )
    intent = read_json(transition_path)
    monkeypatch.setattr(discovery_module, "_transition_stage", lambda stage: None)
    transport = FakeTransport([])
    end = discover_category_drift(
        tmp_path,
        config,
        ImslpClient(transport=transport, clock=FakeClock(NOW)),
        FakeClock(NOW),
        phase="run_end",
        compare_run_id=RUN_ID,
    )

    assert transport.calls == []
    assert end.report == intent["report"]
    assert end.report_sha256 == intent["report_sha256"]
    assert not transition_path.exists()
    state = RunState.from_dict(
        read_json(tmp_path / f"metadata/runs/{RUN_ID}-state.json")
    )
    assert state.start_drift_report_path == start.report_path
    assert state.start_drift_report_sha256 == start.report_sha256
    assert state.end_drift_report_path == end.report_path
    assert state.end_drift_report_sha256 == end.report_sha256
    assert read_json(tmp_path / "metadata/category_drift_report.json") == end.report


def test_transition_recovery_rejects_unknown_alias_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _write_complete_run(tmp_path, config.config_hash)

    def crash_after_immutable(stage: str) -> None:
        if stage == "after_immutable":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(discovery_module, "_transition_stage", crash_after_immutable)
    with pytest.raises(RuntimeError, match="injected crash"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=FakeTransport(_run_responses()), clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    alias = tmp_path / "metadata/category_drift_report.json"
    atomic_write_json(alias, _drift_report("standalone", compare_run_id=None))
    monkeypatch.setattr(discovery_module, "_transition_stage", lambda stage: None)
    transport = FakeTransport([])

    with pytest.raises(SnapshotError, match="alias.*predecessor|transition"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    assert transport.calls == []


def test_pending_run_transition_blocks_standalone_alias_update_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _write_complete_run(tmp_path, config.config_hash)

    def crash_after_wal(stage: str) -> None:
        if stage == "after_wal":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(discovery_module, "_transition_stage", crash_after_wal)
    with pytest.raises(RuntimeError, match="injected crash"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=FakeTransport(_run_responses()), clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    transport = FakeTransport([])

    with pytest.raises(SnapshotError, match="transition.*standalone"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
        )
    assert transport.calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        "new_negative",
        "new_duplicate",
        "new_extra_key",
        "new_unsorted",
        "filtered_bad_reason",
        "empty_deleted_overlap",
        "unchanged_count",
        "rename_candidate_missing",
        "rename_distance_high",
        "rename_overlap_low",
        "rename_count_mismatch",
    ],
)
def test_existing_pointer_rejects_invalid_group_schema_before_network(
    tmp_path: Path,
    mutation: str,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _, state_path = _write_complete_run(tmp_path, config.config_hash)
    report = _strict_run_report(config.config_hash)
    if mutation == "new_negative":
        report["new_candidates"][0]["size"] = -1
    elif mutation == "new_duplicate":
        report["new_candidates"].append(dict(report["new_candidates"][0]))
    elif mutation == "new_extra_key":
        report["new_candidates"][0]["unexpected"] = True
    elif mutation == "new_unsorted":
        report["new_candidates"].append(
            {
                "name": "For 2 guitars",
                "size": 1,
                "page_count": 1,
                "file_count": 0,
                "subcategory_count": 0,
            }
        )
    elif mutation == "filtered_bad_reason":
        report["filtered_candidates"] = [
            {
                "name": "For electric guitar",
                "size": 1,
                "page_count": 1,
                "file_count": 0,
                "subcategory_count": 0,
                "reason": "invented",
            }
        ]
    elif mutation == "empty_deleted_overlap":
        report["deleted_categories"] = [
            {"name": "For 3 guitars (arr)", "previous_member_count": 5}
        ]
    elif mutation == "unchanged_count":
        report["member_count_changes"][0]["current_member_count"] = 5
    elif mutation == "rename_candidate_missing":
        report["possible_renames"][0]["candidate_name"] = "For 5 guitars"
    elif mutation == "rename_distance_high":
        report["possible_renames"][0]["name_distance"] = 7
    elif mutation == "rename_overlap_low":
        report["possible_renames"][0]["jaccard_overlap"] = 0.79
    elif mutation == "rename_count_mismatch":
        report["possible_renames"][0]["candidate_member_count"] = 3
    _bind_existing_drift(
        tmp_path,
        state_path,
        config.config_hash,
        report_overrides=report,
    )
    transport = FakeTransport([])

    with pytest.raises(SnapshotError, match="drift report"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_end",
            compare_run_id=RUN_ID,
        )
    assert transport.calls == []


@pytest.mark.parametrize("leaf", ["snapshot", "state"])
def test_leaf_swap_during_network_is_rejected_without_following_symlink(
    tmp_path: Path,
    leaf: str,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    snapshot_path, state_path = _write_complete_run(tmp_path, config.config_hash)
    target = snapshot_path if leaf == "snapshot" else state_path
    outside = tmp_path.parent / f"outside-{leaf}-swap.json"
    outside.write_bytes(target.read_bytes())

    class LeafSwapClient:
        def allcategories(self, *, prefix: str = ""):
            assert prefix == "For"
            target.unlink()
            target.symlink_to(outside)
            return ()

        def category_info(self, name: str) -> CategoryInfo:
            return CategoryInfo(name, 1, 0, 0)

    with pytest.raises((SnapshotError, ValueError), match="symlink|unsafe|regular"):
        discover_category_drift(
            tmp_path,
            config,
            LeafSwapClient(),  # type: ignore[arg-type]
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    assert outside.read_bytes()


def test_full_validator_rejects_nonfinite_overlap() -> None:
    report = _strict_run_report("a" * 64)
    report["possible_renames"][0]["jaccard_overlap"] = float("nan")

    with pytest.raises(SnapshotError, match="rename overlap"):
        discovery_module._validate_drift_report(report)


def test_immutable_orphan_without_wal_fails_closed_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _write_complete_run(tmp_path, config.config_hash)

    def crash_after_immutable(stage: str) -> None:
        if stage == "after_immutable":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(discovery_module, "_transition_stage", crash_after_immutable)
    with pytest.raises(RuntimeError, match="injected crash"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=FakeTransport(_run_responses()), clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    transition = (
        tmp_path / f"metadata/runs/{RUN_ID}-category-drift-start-transition.json"
    )
    transition.unlink()
    monkeypatch.setattr(discovery_module, "_transition_stage", lambda stage: None)
    transport = FakeTransport([])

    with pytest.raises((SnapshotError, FileExistsError), match="report|transition|exists"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    assert transport.calls == []


@pytest.mark.parametrize(
    "mutation",
    ["intent_identity", "intent_symlink", "unknown_report", "unknown_state"],
)
def test_transition_recovery_rejects_unknown_or_forged_state_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    config_path = tmp_path / "config/categories.json"
    _write_allowlist(config_path)
    config = load_allowlist(config_path)
    _, state_path = _write_complete_run(tmp_path, config.config_hash)

    def crash_after_wal(stage: str) -> None:
        if stage == "after_wal":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(discovery_module, "_transition_stage", crash_after_wal)
    with pytest.raises(RuntimeError, match="injected crash"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=FakeTransport(_run_responses()), clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    transition = (
        tmp_path / f"metadata/runs/{RUN_ID}-category-drift-start-transition.json"
    )
    if mutation == "intent_identity":
        intent = read_json(transition)
        intent["report_sha256"] = "f" * 64
        unsigned = dict(intent)
        unsigned.pop("intent_sha256")
        intent["intent_sha256"] = hashlib.sha256(
            _canonical_test_bytes(unsigned)
        ).hexdigest()
        atomic_write_json(transition, intent)
    elif mutation == "intent_symlink":
        outside = tmp_path.parent / "outside-transition.json"
        outside.write_bytes(transition.read_bytes())
        transition.unlink()
        transition.symlink_to(outside)
    elif mutation == "unknown_report":
        report_path = tmp_path / f"metadata/runs/{RUN_ID}-category-drift-start.json"
        atomic_write_json(report_path, _drift_report("standalone", compare_run_id=None))
    elif mutation == "unknown_state":
        state = RunState.from_dict(read_json(state_path))
        atomic_write_json(
            state_path,
            replace(
                state,
                updated_at=datetime(2026, 8, 31, 12, 2, tzinfo=timezone.utc),
            ).to_dict(),
        )
    monkeypatch.setattr(discovery_module, "_transition_stage", lambda stage: None)
    transport = FakeTransport([])

    with pytest.raises((SnapshotError, ValueError), match="transition|symlink|report|RunState"):
        discover_category_drift(
            tmp_path,
            config,
            ImslpClient(transport=transport, clock=FakeClock(NOW)),
            FakeClock(NOW),
            phase="run_start",
            compare_run_id=RUN_ID,
        )
    assert transport.calls == []
