"""`BlobTransport`: an in-memory Blob container behind a real Azure SDK client
(`84-testing.md` sections 1.2 and 2.4).

A `ContainerClient` (or `BlobServiceClient`) built with `transport=BlobTransport(...)` sends
nothing over the network: each `GET` of a blob is answered from `blobs`, keyed by the URL path
(`/renditions/{indexId}/{documentId}/{v}/pages/{n}.pdf`), honouring `x-ms-range` as the service
does (`206` with `Content-Range`), so `download_blob()` and `StorageStreamDownloader.chunks()` run
their real code: the first ranged read, the size from `Content-Range`, then one read per chunk with
`If-Match` on the first read's ETag. A blob that isn't held answers `404 BlobNotFound`. Every
request is recorded with its method, path and range, so a test can prove which blobs were read and
that no path outside a container was touched.

Faults: `failing` maps a path to the first byte offset from which every read answers
`500 InternalError`, so a download fails after its headers; `stalled` maps a path to the first
byte offset from which a read never answers, so a stuck storage read meets the caller's timeout.
Build the client with `retry_total=0`, or the Blob client's own retries wait between attempts.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from types import TracebackType
from typing import Any, Final, Self, override
from urllib.parse import unquote, urlsplit

from azure.core.pipeline.transport import AsyncHttpTransport
from azure.core.rest import AsyncHttpResponse, HttpRequest
from azure.core.rest._http_response_impl_async import AsyncHttpResponseImpl
from azure.core.utils import case_insensitive_dict

RANGE_HEADER: Final = "x-ms-range"
ETAG: Final = '"0x8DCF0B1C2D3E4F5"'
LAST_MODIFIED: Final = "Sat, 31 Oct 2026 09:15:02 GMT"
PDF_MEDIA_TYPE: Final = "application/pdf"
_RANGE_RE: Final = re.compile(r"^bytes=(\d+)-(\d*)$")


@dataclass(frozen=True, slots=True)
class BlobRequest:
    """A request the transport received."""

    method: str
    path: str
    range: str | None
    headers: Mapping[str, str]

    @property
    def start(self) -> int:
        """The first byte the request asks for (0 without a range)."""
        match = _RANGE_RE.match(self.range or "")
        return int(match.group(1)) if match else 0


class _Body:
    """The stream-download generator of an in-memory response: the payload in one piece."""

    def __init__(self, pipeline: Any, response: BlobResponse, **kwargs: Any) -> None:
        del pipeline, kwargs
        self.response = response
        self.content_length = len(response.payload)
        self._done = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return self.response.payload


class _Internal:
    """The "internal response" of an in-memory response; there is nothing to close."""

    async def close(self) -> None:
        return None


class BlobResponse(AsyncHttpResponseImpl):
    """A complete in-memory response that the SDK can stream as it streams a real one."""

    def __init__(
        self, request: HttpRequest, status: int, headers: Mapping[str, str], payload: bytes
    ) -> None:
        values = case_insensitive_dict(headers)
        super().__init__(
            request=request,
            internal_response=_Internal(),
            status_code=status,
            reason=HTTPStatus(status).phrase,
            content_type=values.get("Content-Type"),
            headers=values,
            stream_download_generator=_Body,
        )
        self.payload = payload


@dataclass(slots=True)
class BlobTransport(AsyncHttpTransport[HttpRequest, AsyncHttpResponse]):
    """Serves the blobs of `blobs` by URL path; `requests` lists every request received."""

    blobs: dict[str, bytes] = field(default_factory=dict)
    failing: dict[str, int] = field(default_factory=dict)
    stalled: dict[str, int] = field(default_factory=dict)
    requests: list[BlobRequest] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [request.path for request in self.requests]

    @staticmethod
    def _error(request: HttpRequest, status: int, code: str) -> BlobResponse:
        headers = {"x-ms-error-code": code, "Content-Length": "0"}
        return BlobResponse(request, status, headers, b"")

    @override
    async def send(self, request: HttpRequest, **kwargs: Any) -> BlobResponse:
        del kwargs
        path = unquote(urlsplit(request.url).path)
        sent = BlobRequest(
            method=request.method.upper(),
            path=path,
            range=request.headers.get(RANGE_HEADER),
            headers=dict(request.headers),
        )
        self.requests.append(sent)
        data = self.blobs.get(path)
        if sent.method != "GET" or data is None:
            return self._error(request, 404, "BlobNotFound")
        if path in self.stalled and sent.start >= self.stalled[path]:
            await asyncio.Event().wait()
        if path in self.failing and sent.start >= self.failing[path]:
            return self._error(request, 500, "InternalError")
        headers = {
            "Content-Type": PDF_MEDIA_TYPE,
            "ETag": ETAG,
            "Last-Modified": LAST_MODIFIED,
            "x-ms-blob-type": "BlockBlob",
        }
        match = _RANGE_RE.match(sent.range or "")
        if match is None:
            headers["Content-Length"] = str(len(data))
            return BlobResponse(request, 200, headers, data)
        start = int(match.group(1))
        end = min(int(match.group(2) or len(data) - 1), len(data) - 1)
        if start >= len(data):
            return self._error(request, 416, "InvalidRange")
        payload = data[start : end + 1]
        headers["Content-Length"] = str(len(payload))
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return BlobResponse(request, 206, headers, payload)

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
