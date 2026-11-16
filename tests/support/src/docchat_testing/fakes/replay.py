"""`ReplayTransport`: an azure-core `AsyncHttpTransport` serving recorded JSON exchanges
(`84-testing.md` sections 1.2, 1.5 and 2.4).

An Azure SDK client built with `transport=ReplayTransport(...)` sends nothing over the network:
each request is answered by the first recorded exchange it matches, and a request no exchange
matches fails the call with `ReplayMismatchError`, so a request the code shouldn't make, or makes
in a different shape, fails the test. The exchanges of one service live in
`tests/fixtures/http/<service>/` as JSON files, each holding one exchange or a list of them:

```json
{"request": {"method": "POST", "path": "/indexes('docchat-…-g1')/docs/search.post.search",
             "query": {"api-version": "2026-04-01"}, "json": {"search": "…", "top": 50}},
 "response": {"status": 200, "headers": {"Content-Type": "application/json"},
              "json": {"value": []}}}
```

A request matches when its method and URL path are equal, every recorded `query` parameter has
the recorded value, and, when the exchange records `json`, its body is that JSON value (key order
and spacing aside). A response carries `json`, or `body` (text), or neither. Files are read when
the transport is built, which a test does in a synchronous fixture, never inside a coroutine.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, override
from urllib.parse import parse_qs, unquote, urlsplit

from azure.core.pipeline.transport import AsyncHttpTransport
from azure.core.rest import AsyncHttpResponse, HttpRequest
from azure.core.rest._http_response_impl_async import AsyncHttpResponseImpl
from azure.core.utils import case_insensitive_dict

FIXTURES_HTTP_ROOT: Final = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "http"
JSON_CONTENT_TYPE: Final = "application/json"


class ReplayMismatchError(AssertionError):
    """A request no recorded exchange matches; the message names its method, path and query."""


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """What a request must look like to match."""

    method: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    json: Any = None
    has_json: bool = False


@dataclass(frozen=True, slots=True)
class RecordedResponse:
    status: int
    headers: Mapping[str, str]
    content: bytes


@dataclass(frozen=True, slots=True)
class Exchange:
    """One recorded request and the response it gets."""

    request: RecordedRequest
    response: RecordedResponse
    source: str = "inline"

    @classmethod
    def from_json(cls, data: Mapping[str, Any], source: str = "inline") -> Exchange:
        request = data["request"]
        response = data["response"]
        headers = {str(name): str(value) for name, value in response.get("headers", {}).items()}
        if "json" in response:
            content = json.dumps(response["json"]).encode("utf-8")
            headers.setdefault("Content-Type", JSON_CONTENT_TYPE)
        elif "body" in response:
            content = str(response["body"]).encode("utf-8")
        else:
            content = b""
        return cls(
            request=RecordedRequest(
                method=str(request["method"]).upper(),
                path=str(request["path"]),
                query={str(name): str(value) for name, value in request.get("query", {}).items()},
                json=request.get("json"),
                has_json="json" in request,
            ),
            response=RecordedResponse(int(response["status"]), headers, content),
            source=source,
        )


def load_exchanges(directory: Path) -> list[Exchange]:
    """Every exchange of the JSON files in `directory`, in file-name order."""
    if not directory.is_dir():
        raise FileNotFoundError(f"no recorded exchanges at {directory}")
    exchanges: list[Exchange] = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else [data]
        exchanges.extend(Exchange.from_json(entry, source=path.name) for entry in entries)
    return exchanges


def _request_body(request: Any) -> bytes | None:
    for attribute in ("content", "body", "data"):
        try:
            value = getattr(request, attribute, None)
        except (TypeError, ValueError):
            continue
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
    return None


@dataclass(frozen=True, slots=True)
class SentRequest:
    """A request the transport received, kept for the test's assertions."""

    method: str
    path: str
    query: Mapping[str, list[str]]
    body: bytes | None

    def json(self) -> Any:
        return None if self.body is None else json.loads(self.body)


def _matches(exchange: Exchange, sent: SentRequest) -> bool:
    recorded = exchange.request
    if recorded.method != sent.method or recorded.path != sent.path:
        return False
    if any(sent.query.get(name) != [value] for name, value in recorded.query.items()):
        return False
    if recorded.has_json:
        try:
            return bool(sent.json() == recorded.json)
        except ValueError:
            return False
    return True


class _ReplayedBody:
    """The "internal response" of a replayed response: the body is already in memory."""

    async def close(self) -> None:
        return None


class ReplayResponse(AsyncHttpResponseImpl):
    """A complete in-memory response, as azure-core's own transports build them."""

    def __init__(self, request: Any, recorded: RecordedResponse) -> None:
        headers = case_insensitive_dict(recorded.headers)
        super().__init__(
            request=request,
            internal_response=_ReplayedBody(),
            status_code=recorded.status,
            reason=HTTPStatus(recorded.status).phrase,
            content_type=headers.get("Content-Type"),
            headers=headers,
            stream_download_generator=None,
        )
        self._content = recorded.content


class ReplayTransport(AsyncHttpTransport[HttpRequest, AsyncHttpResponse]):
    """Serves `exchanges`; `requests` lists every request received, matched or not."""

    def __init__(self, exchanges: Sequence[Exchange]) -> None:
        self._exchanges = tuple(exchanges)
        self.requests: list[SentRequest] = []

    @classmethod
    def for_service(cls, service: str, *, root: Path = FIXTURES_HTTP_ROOT) -> ReplayTransport:
        """The transport of `tests/fixtures/http/<service>/`."""
        return cls(load_exchanges(root / service))

    @override
    async def send(self, request: HttpRequest, **kwargs: Any) -> ReplayResponse:
        del kwargs
        url = urlsplit(request.url)
        sent = SentRequest(
            method=request.method.upper(),
            path=unquote(url.path),
            query=parse_qs(url.query, keep_blank_values=True),
            body=_request_body(request),
        )
        self.requests.append(sent)
        for exchange in self._exchanges:
            if _matches(exchange, sent):
                return ReplayResponse(request, exchange.response)
        raise ReplayMismatchError(
            f"no recorded exchange for {sent.method} {sent.path} {dict(sent.query)}"
        )

    @override
    async def open(self) -> None:
        return None

    @override
    async def close(self) -> None:
        return None

    @override
    async def __aenter__(self) -> Self:
        return self

    @override
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        return None
