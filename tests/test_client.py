from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from imslp_library.client import (
    PROJECT_USER_AGENT,
    CategoryInfo,
    CategoryMember,
    FileMetadata,
    ImslpClient,
    ImslpClientError,
    RevisionRecord,
)
from tests.basic_helpers import ROOT
from tests.network_helpers import FakeClock, FakeTransport, json_response, stream_response


FIXTURES = ROOT / "tests/fixtures/api"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_category_members_accepts_modern_continuation_and_sets_identity_headers() -> None:
    transport = FakeTransport([
        json_response(_fixture("categorymembers-page-1.json")),
        json_response(_fixture("categorymembers-page-2.json")),
    ])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed())

    assert client.category_members("For 3 guitars") == (
        CategoryMember(page_id=101, title="Alpha (Composer, Test)"),
        CategoryMember(page_id=102, title="Beta (Composer, Test)"),
    )
    assert "cmcontinue" not in _query(transport.calls[0].url)
    assert _query(transport.calls[1].url)["cmcontinue"] == ["next"]
    assert _query(transport.calls[1].url)["continue"] == ["-||"]
    for call in transport.calls:
        headers = dict(call.headers)
        assert headers["Accept-Encoding"] == "identity"
        assert headers["User-Agent"] == PROJECT_USER_AGENT
        assert call.timeout == (5.0, 30.0)


def test_category_members_accepts_legacy_query_continue() -> None:
    first = _fixture("categorymembers-page-1.json")
    assert isinstance(first, dict)
    first.pop("continue")
    first["query-continue"] = {"categorymembers": {"cmcontinue": "legacy-next"}}
    transport = FakeTransport([
        json_response(first),
        json_response(_fixture("categorymembers-page-2.json")),
    ])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed())

    assert len(client.category_members("For 3 guitars")) == 2
    assert _query(transport.calls[1].url)["cmcontinue"] == ["legacy-next"]
    assert "continue" not in _query(transport.calls[1].url)


def test_modern_continuation_rejects_unknown_or_repeated_tokens_boundedly() -> None:
    unknown = _fixture("categorymembers-page-1.json")
    assert isinstance(unknown, dict)
    unknown["continue"]["unexpected"] = "injected"
    client = ImslpClient(transport=FakeTransport([json_response(unknown)]), clock=FakeClock.fixed())
    with pytest.raises(ImslpClientError, match="continuation"):
        client.category_members("For guitar")

    repeated = _fixture("categorymembers-page-1.json")
    repeated_with_changed_generic = _fixture("categorymembers-page-1.json")
    repeated_with_changed_generic["continue"]["continue"] = "different-generic-token"
    transport = FakeTransport([json_response(repeated), json_response(repeated_with_changed_generic)])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed())
    with pytest.raises(ImslpClientError, match="repeated continuation"):
        client.category_members("For guitar")
    assert len(transport.calls) == 2


def test_pagination_page_cap_fails_before_an_unbounded_followup() -> None:
    transport = FakeTransport([json_response(_fixture("categorymembers-page-1.json"))])
    client = ImslpClient(
        transport=transport,
        clock=FakeClock.fixed(),
        max_pagination_pages=1,
    )
    with pytest.raises(ImslpClientError, match="page cap"):
        client.category_members("For guitar")
    assert len(transport.calls) == 1


def test_revision_and_file_metadata_results_are_typed_and_exact() -> None:
    transport = FakeTransport([
        json_response(_fixture("revisions.json")),
        json_response(_fixture("revisions.json")),
        json_response(_fixture("imageinfo.json")),
    ])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed())

    current = client.current_revisions((102, 101))
    exact = client.exact_revisions((202, 201))
    files = client.imageinfo(("alpha.pdf",))

    assert all(isinstance(item, RevisionRecord) for item in current + exact)
    assert [(item.page_id, item.revision_id) for item in current] == [(101, 201), (102, 202)]
    assert exact == current
    assert _query(transport.calls[1].url)["revids"] == ["201|202"]
    assert "pageids" not in _query(transport.calls[1].url)
    assert files == (
        FileMetadata(
            file_id="301",
            filename="alpha.pdf",
            source_url="https://imslp.org/files/alpha.pdf",
            expected_size=123,
            sha1_imslp="a" * 40,
            mime="application/pdf",
            copyright_label="Public Domain",
        ),
    )


def test_revision_queries_are_split_at_the_mediawiki_request_bound() -> None:
    def response_for(page_ids: range) -> dict[str, object]:
        return {
            "query": {
                "pages": [
                    {
                        "pageid": page_id,
                        "title": f"Work {page_id}",
                        "revisions": [{"revid": page_id + 1000, "slots": {"main": {"content": ""}}}],
                    }
                    for page_id in page_ids
                ]
            }
        }

    transport = FakeTransport([
        json_response(response_for(range(1, 51))),
        json_response(response_for(range(51, 52))),
    ])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed())

    records = client.current_revisions(tuple(range(51, 0, -1)))
    assert len(records) == 51
    assert len(transport.calls) == 2
    assert len(_query(transport.calls[0].url)["pageids"][0].split("|")) == 50
    assert _query(transport.calls[1].url)["pageids"] == ["51"]


def test_category_info_and_allcategories_are_typed_and_paginated() -> None:
    transport = FakeTransport([
        json_response({"query": {"pages": [{"pageid": 9, "title": "Category:For guitar", "categoryinfo": {"pages": 7, "files": 0, "subcats": 0}}]}}),
        json_response({"continue": {"accontinue": "For 2 guitars", "continue": "-||"}, "query": {"allcategories": [{"category": "For guitar", "size": 7, "pages": 7, "files": 0, "subcats": 0}]}}),
        json_response({"query": {"allcategories": [{"category": "For 2 guitars", "size": 4, "pages": 4, "files": 0, "subcats": 0}]}}),
    ])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed())

    assert client.category_info("For guitar") == CategoryInfo("For guitar", 7, 0, 0)
    categories = client.allcategories(prefix="For ")
    assert [(item.name, item.page_count) for item in categories] == [("For 2 guitars", 4), ("For guitar", 7)]
    assert _query(transport.calls[2].url)["accontinue"] == ["For 2 guitars"]


def test_category_info_distinguishes_missing_from_present_empty() -> None:
    transport = FakeTransport([
        json_response({"query": {"pages": [{"ns": 14, "title": "Category:Missing", "missing": True}]}}),
        json_response({"query": {"pages": [{"pageid": 8, "title": "Category:Empty", "categoryinfo": {"pages": 0, "files": 0, "subcats": 0}}]}}),
    ])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed())

    assert client.category_info("Missing") == CategoryInfo("Missing", 0, 0, 0, missing=True)
    assert client.category_info("Empty") == CategoryInfo("Empty", 0, 0, 0, missing=False)


def test_permanent_404_is_attempted_once() -> None:
    transport = FakeTransport([json_response({"error": "missing"}, status=404)])
    client = ImslpClient(transport=transport, clock=FakeClock.fixed(), max_attempts=5)

    with pytest.raises(ImslpClientError, match="404"):
        client.category_members("For guitar")
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "responses",
    [
        [TimeoutError("timeout"), TimeoutError("timeout"), TimeoutError("timeout")],
        [json_response({}, status=429, headers={"Retry-After": "4"}) for _ in range(3)],
        [json_response({}, status=503) for _ in range(3)],
        [stream_response((b'{"query":', TimeoutError("read timeout")), "application/json") for _ in range(3)],
    ],
)
def test_retryable_failures_stop_at_attempt_cap_without_blocking(responses) -> None:
    clock = FakeClock.fixed()
    transport = FakeTransport(responses)
    client = ImslpClient(transport=transport, clock=clock, max_attempts=3, backoff_seconds=1.0)

    with pytest.raises(ImslpClientError, match="after 3 attempts"):
        client.category_members("For guitar")
    assert len(transport.calls) == 3
    assert len(clock.sleeps) == 2
    assert all(seconds >= 1 for seconds in clock.sleeps)


def test_exponential_backoff_has_an_explicit_delay_cap() -> None:
    clock = FakeClock.fixed()
    transport = FakeTransport([TimeoutError("timeout") for _ in range(5)])
    client = ImslpClient(
        transport=transport,
        clock=clock,
        max_attempts=5,
        backoff_seconds=2.0,
        max_backoff_seconds=3.0,
    )
    with pytest.raises(ImslpClientError, match="after 5 attempts"):
        client.category_members("For guitar")
    assert clock.sleeps == [2.0, 3.0, 3.0, 3.0]


def test_unapproved_endpoint_redirect_and_image_url_are_rejected() -> None:
    with pytest.raises(ValueError, match="approved IMSLP host"):
        ImslpClient(transport=FakeTransport([]), clock=FakeClock.fixed(), api_url="https://example.com/api.php")

    redirected = json_response(_fixture("categorymembers-page-2.json"))
    object.__setattr__(redirected, "final_url", "https://example.com/stolen")
    client = ImslpClient(transport=FakeTransport([redirected]), clock=FakeClock.fixed())
    with pytest.raises(ImslpClientError, match="approved IMSLP host"):
        client.category_members("For guitar")

    image = _fixture("imageinfo.json")
    assert isinstance(image, dict)
    image["query"]["pages"][0]["imageinfo"][0]["url"] = "https://example.com/score.pdf"
    client = ImslpClient(transport=FakeTransport([json_response(image)]), clock=FakeClock.fixed())
    with pytest.raises(ImslpClientError, match="approved IMSLP host"):
        client.imageinfo(("alpha.pdf",))


def test_invalid_json_and_incomplete_typed_payloads_fail_closed() -> None:
    client = ImslpClient(
        transport=FakeTransport([stream_response((b"not-json",), "application/json")]),
        clock=FakeClock.fixed(),
    )
    with pytest.raises(ImslpClientError, match="JSON"):
        client.category_members("For guitar")

    client = ImslpClient(
        transport=FakeTransport([json_response({"query": {"categorymembers": [{"pageid": "101"}]}})]),
        clock=FakeClock.fixed(),
    )
    with pytest.raises(ImslpClientError, match="category member"):
        client.category_members("For guitar")
