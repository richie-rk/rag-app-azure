"""The layout call of the recursive method and its extract file (`14-ingestion.md` sections
6.14 item 2, 11.1 and 11.7; ADR 0048 items 1, 2 and 10).

The activity process sends the version's PDF to `prebuilt-layout` (API 2024-11-30) and handles
only the JSON result: Markdown content with offsets in Unicode code points, so every offset is a
Python `str` index. It polls every 3 seconds, or after the service's `retry-after` when that is
longer, for at most 40 minutes, then deletes the result (best effort; the service drops it after
24 hours). Each failure maps to its item code; no error is swallowed and no partial result is
written.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import IO, Any, Final, Protocol, override

from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeResult,
    DocumentContentFormat,
    StringIndexType,
)
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.core.polling.async_base_polling import AsyncLROBasePolling
from azure.core.rest import AsyncHttpResponse, HttpRequest

from docchat_core.ingestion.methods import MethodFailedError
from docchat_core.models import Clock
from docchat_ingestion import telemetry
from docchat_ingestion.methods.retry import retry_after_seconds
from docchat_ingestion.workfiles import ExtractDocument, PageOffset

MODEL_ID: Final = "prebuilt-layout"
CONTENT_TYPE: Final = "application/octet-stream"
POLL_SECONDS: Final = 3
POLL_LIMIT_SECONDS: Final = 40 * 60
BAD_REQUEST: Final = 400
THROTTLED_STATUS: Final = 429
SERVER_ERROR: Final = 500
LOCKED_FILE_CODE_FRAGMENT: Final = "password"
INVALID_CONTENT: Final = "InvalidContent"


class LayoutAnalyzer(Protocol):
    """The layout service, the port the recursive method's tests fake: the analysed result of
    one PDF, or `MethodFailedError` with the item code of the failure."""

    async def analyze(self, pdf: IO[bytes]) -> AnalyzeResult: ...


class _PollingTimeoutError(Exception):
    def __init__(self) -> None:
        super().__init__("docintel_timeout")


type Sleep = Callable[[float], Awaitable[None]]


class _LayoutPolling(AsyncLROBasePolling[AnalyzeResult, HttpRequest, AsyncHttpResponse]):
    """azure-core's polling with the method's cadence: every `interval` seconds, or after the
    service's `retry-after` when that is longer, and no wait that would end past the bound."""

    def __init__(self, *, interval: float, limit: float, clock: Clock, sleep: Sleep) -> None:
        super().__init__(interval)
        self._limit = limit
        self._clock = clock
        self._wait = sleep
        self._started = clock.monotonic()

    @override
    def _extract_delay(self) -> float:
        return max(float(self._timeout), super()._extract_delay())

    @override
    async def _sleep(self, delay: float) -> None:
        if self._clock.monotonic() - self._started + delay > self._limit:
            raise _PollingTimeoutError
        await self._wait(delay)


def _error_codes(failure: HttpResponseError) -> list[str]:
    """The error code and every inner error code of a service error."""
    error = failure.error
    if error is None:
        return []
    codes = [error.code] if error.code else []
    inner: Any = error.innererror
    while isinstance(inner, dict) and inner:
        code = inner.get("code")
        if isinstance(code, str):
            codes.append(code)
        inner = inner.get("innererror")
    return codes


def layout_failure(failure: HttpResponseError) -> MethodFailedError:
    """The item code of a layout-service error (section 11.7). A status the table doesn't list
    is re-raised for the failure boundary."""
    status = failure.status_code or 0
    headers: Mapping[str, str] = getattr(failure.response, "headers", None) or {}
    if status == THROTTLED_STATUS:
        telemetry.record_throttled("docintel")
        return MethodFailedError(
            "docintel_throttled", retry_after_seconds=retry_after_seconds(headers)
        )
    if status >= SERVER_ERROR:
        return MethodFailedError(
            "docintel_unavailable", retry_after_seconds=retry_after_seconds(headers)
        )
    if status == BAD_REQUEST:
        codes = _error_codes(failure)
        if any(LOCKED_FILE_CODE_FRAGMENT in code.casefold() for code in codes):
            return MethodFailedError("encrypted_document")
        if INVALID_CONTENT in codes:
            return MethodFailedError("corrupt_document")
        return MethodFailedError("docintel_failed")
    # The polling of an analysis whose status is `failed` ends with the 200 of its last poll.
    if status < BAD_REQUEST:
        return MethodFailedError("docintel_failed")
    raise failure


class DocumentIntelligenceLayout:
    """`LayoutAnalyzer` over the process's async `DocumentIntelligenceClient` (built by
    `docchat_core.clients` with the ingestion identity, API 2024-11-30, connection timeout 10 s
    and read timeout 300 s)."""

    def __init__(
        self,
        client: DocumentIntelligenceClient,
        clock: Clock,
        sleep: Sleep,
        *,
        poll_seconds: float = POLL_SECONDS,
        poll_limit_seconds: float = POLL_LIMIT_SECONDS,
    ) -> None:
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._poll_limit_seconds = poll_limit_seconds

    async def analyze(self, pdf: IO[bytes]) -> AnalyzeResult:
        polling = _LayoutPolling(
            interval=self._poll_seconds,
            limit=self._poll_limit_seconds,
            clock=self._clock,
            sleep=self._sleep,
        )
        try:
            poller = await self._client.begin_analyze_document(
                MODEL_ID,
                pdf,
                content_type=CONTENT_TYPE,
                output_content_format=DocumentContentFormat.MARKDOWN,
                string_index_type=StringIndexType.UNICODE_CODE_POINT,
                polling=polling,
            )
            try:
                result = await poller.result()
            finally:
                await self._delete(poller.details.get("operation_id"))
        except _PollingTimeoutError:
            raise MethodFailedError("docintel_timeout") from None
        except HttpResponseError as failure:
            raise layout_failure(failure) from None
        except (ServiceRequestError, ServiceResponseError):
            raise MethodFailedError("docintel_unavailable") from None
        return result

    async def _delete(self, result_id: object) -> None:
        """`delete_analyze_result()`, best effort: the service drops the result after 24 hours
        anyway, so a failure here never fails the item."""
        if not isinstance(result_id, str):
            return
        # The SDK decodes an error body as JSON, so an empty one raises `ValueError`.
        with contextlib.suppress(AzureError, ValueError):
            await self._client.delete_analyze_result(MODEL_ID, result_id)


def extract_document(result: AnalyzeResult, page_count: int) -> ExtractDocument:
    """The extract file of an analysis: its Markdown and one `pageOffsets` entry per span of
    `pages[].spans`."""
    pages = result.pages or []
    return ExtractDocument(
        page_count=page_count,
        markdown=result.content or "",
        page_offsets=[
            PageOffset(page_number=page.page_number, offset=span.offset, length=span.length)
            for page in pages
            for span in page.spans or []
        ],
        doc_intel_pages=len(pages),
    )
