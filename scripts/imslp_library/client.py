from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


PROJECT_USER_AGENT = "imslp-guitar-library/1.0 (offline pure-guitar catalog)"
DEFAULT_API_URL = "https://imslp.org/api.php"


class ImslpClientError(RuntimeError):
    """A bounded, fail-closed IMSLP request or response error."""


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: tuple[float, float],
    ): ...


@dataclass(frozen=True, slots=True)
class CategoryMember:
    page_id: int
    title: str

    def __post_init__(self) -> None:
        if type(self.page_id) is not int or self.page_id < 0 or not isinstance(self.title, str) or not self.title:
            raise ValueError("invalid category member")


@dataclass(frozen=True, slots=True)
class CategoryInfo:
    name: str
    page_count: int
    file_count: int
    subcategory_count: int
    missing: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("invalid category name")
        if any(type(value) is not int or value < 0 for value in (self.page_count, self.file_count, self.subcategory_count)):
            raise ValueError("invalid category counts")
        if type(self.missing) is not bool:
            raise ValueError("invalid category missing flag")
        if self.missing and any((self.page_count, self.file_count, self.subcategory_count)):
            raise ValueError("a missing category cannot have counts")


@dataclass(frozen=True, slots=True)
class AllCategory:
    name: str
    size: int
    page_count: int
    file_count: int
    subcategory_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("invalid category name")
        if any(type(value) is not int or value < 0 for value in (self.size, self.page_count, self.file_count, self.subcategory_count)):
            raise ValueError("invalid category counts")


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    page_id: int
    revision_id: int
    title: str
    wikitext: str

    def __post_init__(self) -> None:
        if type(self.page_id) is not int or self.page_id < 0:
            raise ValueError("invalid revision page ID")
        if type(self.revision_id) is not int or self.revision_id < 0:
            raise ValueError("invalid revision ID")
        if not isinstance(self.title, str) or not self.title or not isinstance(self.wikitext, str):
            raise ValueError("invalid revision content")


@dataclass(frozen=True, slots=True)
class FileMetadata:
    file_id: str
    filename: str
    source_url: str
    expected_size: int | None
    sha1_imslp: str | None
    mime: str | None
    copyright_label: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.file_id, str) or not self.file_id.isascii() or not self.file_id.isdecimal() or int(self.file_id) <= 0:
            raise ValueError("invalid imageinfo file ID")
        if not isinstance(self.filename, str) or not self.filename:
            raise ValueError("invalid imageinfo filename")
        _validate_approved_url(self.source_url)
        if self.expected_size is not None and (type(self.expected_size) is not int or self.expected_size < 0):
            raise ValueError("invalid imageinfo size")
        if self.sha1_imslp is not None and (
            not isinstance(self.sha1_imslp, str)
            or len(self.sha1_imslp) != 40
            or any(character not in "0123456789abcdef" for character in self.sha1_imslp)
        ):
            raise ValueError("invalid imageinfo SHA-1")
        for value in (self.mime, self.copyright_label):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("invalid imageinfo optional string")


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _UrllibResponse:
    def __init__(self, response) -> None:
        self._response = response
        self.status = int(response.status)
        self.headers = {str(key): str(value) for key, value in response.headers.items()}
        self.final_url = str(response.geturl())

    def read(self) -> bytes:
        try:
            return self._response.read()
        finally:
            self._response.close()

    def iter_bytes(self):
        try:
            while chunk := self._response.read(64 * 1024):
                yield chunk
        finally:
            self._response.close()


class UrllibTransport:
    """Small stdlib transport; injected transports keep tests fully offline."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: tuple[float, float],
    ) -> _UrllibResponse:
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=timeout[0])
        except urllib.error.HTTPError as exc:
            response = exc
        # urllib exposes one public timeout. Set the underlying read socket when
        # available so both configured limits remain explicit.
        try:
            response.fp.raw._sock.settimeout(timeout[1])
        except AttributeError:
            pass
        return _UrllibResponse(response)


def _validate_approved_url(url: str) -> None:
    if not isinstance(url, str):
        raise ValueError("URL must use an approved IMSLP host")
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or not (host == "imslp.org" or host.endswith(".imslp.org")):
        raise ValueError("URL must use an approved IMSLP host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must use an approved IMSLP host")


def _pages(payload: dict[str, object]) -> list[dict[str, object]]:
    query = payload.get("query")
    if not isinstance(query, dict):
        raise ImslpClientError("response has no typed query object")
    pages = query.get("pages")
    if isinstance(pages, dict):
        values = list(pages.values())
    elif isinstance(pages, list):
        values = pages
    else:
        raise ImslpClientError("response has no typed pages array")
    if not all(isinstance(item, dict) for item in values):
        raise ImslpClientError("response pages are malformed")
    return values


def _continuation(payload: dict[str, object], module: str, key: str) -> dict[str, str] | None:
    modern = payload.get("continue")
    legacy = payload.get("query-continue")
    if "continue" in payload:
        if "query-continue" in payload or not isinstance(modern, dict) or set(modern) != {"continue", key}:
            raise ImslpClientError(f"invalid {module} continuation mapping")
        if not all(isinstance(value, str) and value for value in modern.values()):
            raise ImslpClientError(f"invalid {module} continuation token")
        return {"continue": modern["continue"], key: modern[key]}
    if "query-continue" in payload:
        if not isinstance(legacy, dict) or set(legacy) != {module}:
            raise ImslpClientError(f"invalid legacy {module} continuation mapping")
        module_value = legacy[module]
        if not isinstance(module_value, dict) or set(module_value) != {key}:
            raise ImslpClientError(f"invalid legacy {module} continuation mapping")
        value = module_value[key]
        if not isinstance(value, str) or not value:
            raise ImslpClientError(f"invalid legacy {key} continuation")
        return {key: value}
    return None


class ImslpClient:
    def __init__(
        self,
        *,
        transport: Transport | None = None,
        clock: Clock | None = None,
        api_url: str = DEFAULT_API_URL,
        user_agent: str = PROJECT_USER_AGENT,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        max_attempts: int = 4,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        max_pagination_pages: int = 1000,
    ) -> None:
        _validate_approved_url(api_url)
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("user-agent must be nonblank")
        if type(max_attempts) is not int or max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        for name, value in (("connect_timeout", connect_timeout), ("read_timeout", read_timeout)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if (
            type(backoff_seconds) not in (int, float)
            or not math.isfinite(backoff_seconds)
            or backoff_seconds < 0
        ):
            raise ValueError("backoff_seconds must be nonnegative and finite")
        if (
            type(max_backoff_seconds) not in (int, float)
            or not math.isfinite(max_backoff_seconds)
            or max_backoff_seconds <= 0
        ):
            raise ValueError("max_backoff_seconds must be positive and finite")
        if type(max_pagination_pages) is not int or max_pagination_pages <= 0:
            raise ValueError("max_pagination_pages must be positive")
        self._transport = transport or UrllibTransport()
        self._clock = clock or _SystemClock()
        self._api_url = api_url
        self._user_agent = user_agent
        self._timeout = (float(connect_timeout), float(read_timeout))
        self._max_attempts = max_attempts
        self._backoff_seconds = float(backoff_seconds)
        self._max_backoff_seconds = float(max_backoff_seconds)
        self._max_pagination_pages = max_pagination_pages

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_seconds * (2 ** (attempt - 1)), self._max_backoff_seconds)

    def _url(self, parameters: dict[str, str]) -> str:
        common = {"action": "query", "format": "json", "formatversion": "2", **parameters}
        return f"{self._api_url}?{urllib.parse.urlencode(sorted(common.items()))}"

    def _request_json(self, parameters: dict[str, str]) -> dict[str, object]:
        url = self._url(parameters)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": self._user_agent,
        }
        last_detail = "request failed"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._transport.request("GET", url, headers=headers, timeout=self._timeout)
            except Exception as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                if attempt == self._max_attempts:
                    break
                self._clock.sleep(self._backoff(attempt))
                continue
            try:
                _validate_approved_url(response.final_url)
            except ValueError as exc:
                try:
                    response.read()
                except Exception:
                    pass
                raise ImslpClientError(str(exc)) from exc
            status = response.status
            if status != 200:
                try:
                    response.read()
                except Exception:
                    pass
                if status not in {429, 503}:
                    raise ImslpClientError(f"IMSLP API returned permanent HTTP {status}")
                last_detail = f"HTTP {status}"
                if attempt == self._max_attempts:
                    break
                retry_after = response.headers.get("Retry-After")
                delay = self._backoff(attempt)
                try:
                    candidate = float(retry_after) if retry_after is not None else delay
                except (TypeError, ValueError):
                    candidate = delay
                if math.isfinite(candidate) and candidate >= 0:
                    delay = candidate
                self._clock.sleep(min(max(delay, self._backoff_seconds), self._max_backoff_seconds))
                continue
            try:
                body = response.read()
            except Exception as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                if attempt == self._max_attempts:
                    break
                self._clock.sleep(self._backoff(attempt))
                continue
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ImslpClientError("IMSLP API response is not valid UTF-8 JSON") from exc
            if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
                raise ImslpClientError("IMSLP API JSON must be an object")
            if "error" in payload:
                raise ImslpClientError("IMSLP API returned an error object")
            return payload
        raise ImslpClientError(f"IMSLP API failed after {self._max_attempts} attempts: {last_detail}")

    def category_members(self, category_name: str) -> tuple[CategoryMember, ...]:
        if not isinstance(category_name, str) or not category_name.strip():
            raise ValueError("category_name must be nonblank")
        parameters = {
            "list": "categorymembers",
            "cmtitle": f"Category:{category_name}",
            "cmnamespace": "0",
            "cmlimit": "max",
            "cmprop": "ids|title",
        }
        members: dict[int, CategoryMember] = {}
        seen_continuations: set[str] = set()
        page_number = 0
        while True:
            page_number += 1
            payload = self._request_json(parameters)
            query = payload.get("query")
            raw = query.get("categorymembers") if isinstance(query, dict) else None
            if not isinstance(raw, list):
                raise ImslpClientError("category members payload is malformed")
            for item in raw:
                if not isinstance(item, dict):
                    raise ImslpClientError("category member is malformed")
                try:
                    member = CategoryMember(item["pageid"], item["title"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ImslpClientError("category member is malformed") from exc
                existing = members.get(member.page_id)
                if existing is not None and existing != member:
                    raise ImslpClientError("category member identity conflict")
                members[member.page_id] = member
            continuation = _continuation(payload, "categorymembers", "cmcontinue")
            if continuation is None:
                break
            identity = continuation["cmcontinue"]
            if identity in seen_continuations:
                raise ImslpClientError("repeated continuation token")
            seen_continuations.add(identity)
            if page_number >= self._max_pagination_pages:
                raise ImslpClientError("pagination page cap reached")
            parameters.update(continuation)
        return tuple(sorted(members.values(), key=lambda item: (item.page_id, item.title)))

    def category_info(self, category_name: str) -> CategoryInfo:
        if not isinstance(category_name, str) or not category_name.strip():
            raise ValueError("category_name must be nonblank")
        payload = self._request_json({"prop": "categoryinfo", "titles": f"Category:{category_name}"})
        pages = _pages(payload)
        if len(pages) != 1:
            raise ImslpClientError("category info payload is malformed")
        if pages[0].get("missing") is True:
            if "categoryinfo" in pages[0]:
                raise ImslpClientError("missing category unexpectedly has category counts")
            return CategoryInfo(category_name, 0, 0, 0, missing=True)
        if not isinstance(pages[0].get("categoryinfo"), dict):
            raise ImslpClientError("category info payload is malformed")
        raw = pages[0]["categoryinfo"]
        try:
            return CategoryInfo(category_name, raw["pages"], raw["files"], raw["subcats"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ImslpClientError("category info payload is malformed") from exc

    def _revisions(self, key: str, values: tuple[int, ...]) -> tuple[RevisionRecord, ...]:
        if not isinstance(values, tuple) or not values or any(type(value) is not int or value < 0 for value in values):
            raise ValueError(f"{key} must be a nonempty tuple of nonnegative integers")
        canonical_values = tuple(sorted(set(values)))
        records: dict[tuple[int, int], RevisionRecord] = {}
        for offset in range(0, len(canonical_values), 50):
            batch = canonical_values[offset:offset + 50]
            payload = self._request_json({
                "prop": "revisions",
                key: "|".join(str(value) for value in batch),
                "rvprop": "ids|content",
                "rvslots": "main",
            })
            batch_actual: set[int] = set()
            for page in _pages(payload):
                revisions = page.get("revisions")
                if not isinstance(revisions, list) or len(revisions) != 1:
                    raise ImslpClientError("revision payload is malformed")
                revision = revisions[0]
                if not isinstance(revision, dict):
                    raise ImslpClientError("revision payload is malformed")
                content: object = revision.get("*")
                slots = revision.get("slots")
                if isinstance(slots, dict) and isinstance(slots.get("main"), dict):
                    content = slots["main"].get("content", slots["main"].get("*"))
                try:
                    record = RevisionRecord(page["pageid"], revision["revid"], page["title"], content)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ImslpClientError("revision payload is malformed") from exc
                identity = (record.page_id, record.revision_id)
                if identity in records and records[identity] != record:
                    raise ImslpClientError("revision identity conflict")
                records[identity] = record
                batch_actual.add(record.page_id if key == "pageids" else record.revision_id)
            if batch_actual != set(batch):
                raise ImslpClientError(f"revision response does not exactly match requested {key}")
        result = tuple(sorted(records.values(), key=lambda item: (item.page_id, item.revision_id)))
        requested = set(canonical_values)
        actual = {item.page_id if key == "pageids" else item.revision_id for item in result}
        if actual != requested:
            raise ImslpClientError(f"revision response does not exactly match requested {key}")
        return result

    def current_revisions(self, page_ids: tuple[int, ...]) -> tuple[RevisionRecord, ...]:
        return self._revisions("pageids", page_ids)

    def exact_revisions(self, revision_ids: tuple[int, ...]) -> tuple[RevisionRecord, ...]:
        return self._revisions("revids", revision_ids)

    def imageinfo(self, filenames: tuple[str, ...]) -> tuple[FileMetadata, ...]:
        if not isinstance(filenames, tuple) or not filenames or any(not isinstance(value, str) or not value for value in filenames):
            raise ValueError("filenames must be a nonempty tuple of names")
        canonical = tuple(sorted(set(name.removeprefix("File:") for name in filenames)))
        results: dict[str, FileMetadata] = {}
        for offset in range(0, len(canonical), 50):
            batch = canonical[offset:offset + 50]
            payload = self._request_json({
                "prop": "imageinfo",
                "titles": "|".join(f"File:{name}" for name in batch),
                "iiprop": "url|size|sha1|mime|extmetadata|canonicaltitle",
            })
            batch_actual: set[str] = set()
            for page in _pages(payload):
                raw_infos = page.get("imageinfo")
                if not isinstance(raw_infos, list) or len(raw_infos) != 1:
                    raise ImslpClientError("imageinfo payload is malformed")
                raw = raw_infos[0]
                if not isinstance(raw, dict):
                    raise ImslpClientError("imageinfo payload is malformed")
                title = page.get("title")
                filename = title.removeprefix("File:") if isinstance(title, str) else None
                extmetadata = raw.get("extmetadata")
                license_value = extmetadata.get("LicenseShortName") if isinstance(extmetadata, dict) else None
                copyright_label = license_value.get("value") if isinstance(license_value, dict) else None
                try:
                    metadata = FileMetadata(
                        file_id=str(page["pageid"]),
                        filename=filename,
                        source_url=raw["url"],
                        expected_size=raw.get("size"),
                        sha1_imslp=raw.get("sha1"),
                        mime=raw.get("mime"),
                        copyright_label=copyright_label,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ImslpClientError(f"imageinfo payload is malformed or URL is not on an approved IMSLP host: {exc}") from exc
                if metadata.filename in results and results[metadata.filename] != metadata:
                    raise ImslpClientError("imageinfo filename conflict")
                results[metadata.filename] = metadata
                batch_actual.add(metadata.filename)
            if batch_actual != set(batch):
                raise ImslpClientError("imageinfo response does not exactly match requested filenames")
        if set(results) != set(canonical):
            raise ImslpClientError("imageinfo response does not exactly match requested filenames")
        return tuple(sorted(results.values(), key=lambda item: (int(item.file_id), item.filename)))

    def allcategories(self, *, prefix: str = "") -> tuple[AllCategory, ...]:
        if not isinstance(prefix, str):
            raise TypeError("prefix must be a string")
        parameters = {"list": "allcategories", "aclimit": "max", "acprop": "size", "acprefix": prefix}
        categories: dict[str, AllCategory] = {}
        seen_continuations: set[str] = set()
        page_number = 0
        while True:
            page_number += 1
            payload = self._request_json(parameters)
            query = payload.get("query")
            raw = query.get("allcategories") if isinstance(query, dict) else None
            if not isinstance(raw, list):
                raise ImslpClientError("allcategories payload is malformed")
            for item in raw:
                if not isinstance(item, dict):
                    raise ImslpClientError("allcategories item is malformed")
                raw_counts = [item.get(key) for key in ("size", "pages", "files", "subcats")]
                if any(type(value) is int and value < 0 for value in raw_counts):
                    # IMSLP uses -1 on a small number of deleted/tombstoned
                    # category rows.  They are not live categories and cannot
                    # be represented by AllCategory's nonnegative contract.
                    continue
                try:
                    # IMSLP currently exposes the category title through the
                    # legacy ``*`` key and encodes spaces as ``+`` even when
                    # formatversion=2 is requested.  Older responses used the
                    # standard ``category`` key.  Accept both documented
                    # shapes and normalize them to the real MediaWiki title.
                    encoded_name = item.get("category", item.get("*"))
                    if not isinstance(encoded_name, str) or not encoded_name:
                        raise ValueError("missing category title")
                    category = AllCategory(
                        urllib.parse.unquote_plus(encoded_name),
                        item["size"],
                        item["pages"],
                        item["files"],
                        item["subcats"],
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ImslpClientError("allcategories item is malformed") from exc
                existing = categories.get(category.name)
                if existing is not None and existing != category:
                    # IMSLP's legacy category feed contains URL-like junk
                    # titles whose ``+`` decoding can collide with a real
                    # category.  A zero-member collision is not a competing
                    # identity; retain the populated record.  Two populated
                    # records remain a hard conflict.
                    if existing.size == 0 and category.size > 0:
                        categories[category.name] = category
                    elif category.size == 0 and existing.size > 0:
                        pass
                    else:
                        raise ImslpClientError(
                            f"allcategories identity conflict: {category.name}"
                        )
                else:
                    categories[category.name] = category
            # IMSLP's live endpoint may return the legacy ``acfrom`` cursor;
            # stock MediaWiki returns modern ``accontinue``.  Do not feed one
            # cursor name back as the other because titles containing spaces
            # and punctuation can otherwise be skipped.
            if "continue" in payload:
                continuation = _continuation(payload, "allcategories", "accontinue")
            elif "query-continue" in payload:
                continuation = _continuation(payload, "allcategories", "acfrom")
            else:
                continuation = None
            if continuation is None:
                break
            identity = continuation.get("accontinue", continuation.get("acfrom"))
            if not isinstance(identity, str) or not identity:
                raise ImslpClientError("invalid allcategories continuation token")
            if identity in seen_continuations:
                raise ImslpClientError("repeated continuation token")
            seen_continuations.add(identity)
            if page_number >= self._max_pagination_pages:
                raise ImslpClientError("pagination page cap reached")
            parameters.update(continuation)
        return tuple(sorted(categories.values(), key=lambda item: item.name))
