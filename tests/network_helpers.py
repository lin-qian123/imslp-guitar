from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator


@dataclass(frozen=True, slots=True)
class FakeResponse:
    status: int
    headers: dict[str, str]
    chunks: tuple[bytes | Exception, ...]
    final_url: str
    _offset: int = field(default=0, init=False, repr=False, compare=False)

    def iter_bytes(self) -> Iterator[bytes]:
        while self._offset < len(self.chunks):
            item = self.chunks[self._offset]
            object.__setattr__(self, "_offset", self._offset + 1)
            if isinstance(item, Exception):
                raise item
            yield item

    def read(self) -> bytes:
        return b"".join(self.iter_bytes())


@dataclass(frozen=True, slots=True)
class FakeCall:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    timeout: object


@dataclass(slots=True)
class FakeClock:
    current: datetime
    sleeps: list[float] = field(default_factory=list)

    @classmethod
    def fixed(cls) -> "FakeClock":
        return cls(datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc))

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be nonnegative")
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


@dataclass(slots=True)
class FakeTransport:
    responses: list[FakeResponse | Exception]
    calls: list[FakeCall] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: object,
    ) -> FakeResponse:
        self.calls.append(FakeCall(method, url, tuple(sorted(headers.items())), timeout))
        if not self.responses:
            raise AssertionError("fake transport response queue is empty")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(
    payload: object,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> FakeResponse:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return bytes_response(body, "application/json", status=status, headers=headers)


def bytes_response(
    body: bytes,
    content_type: str,
    status: int = 200,
    final_url: str = "https://imslp.org/",
    headers: dict[str, str] | None = None,
) -> FakeResponse:
    response_headers = {"Content-Type": content_type}
    if headers:
        response_headers.update(headers)
    return FakeResponse(status, response_headers, (body,), final_url)


def stream_response(
    chunks: tuple[bytes | Exception, ...],
    content_type: str = "application/octet-stream",
    status: int = 200,
    final_url: str = "https://imslp.org/",
    headers: dict[str, str] | None = None,
) -> FakeResponse:
    response_headers = {"Content-Type": content_type}
    if headers:
        response_headers.update(headers)
    return FakeResponse(status, response_headers, chunks, final_url)
