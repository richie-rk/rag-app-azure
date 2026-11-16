"""Unit tests of the layout call and the recursive extract file (T-183 in part; `14-ingestion.md`
sections 6.14 item 2, 11.1 and 11.7; ADR 0048).

The real async `DocumentIntelligenceClient` runs over `ReplayTransport` with hand-written
exchanges of API 2024-11-30, served in order, so the request shape (model, Markdown output,
code-point offsets) is the one the service receives. Polling runs on a `FakeClock`.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any, override

import pytest
from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError, ServiceRequestError
from azure.core.rest import HttpRequest

from docchat_core.ingestion.methods import MethodFailedError
from docchat_ingestion.methods.recursive.layout import (
    DocumentIntelligenceLayout,
    extract_document,
    layout_failure,
)
from docchat_ingestion.workfiles import ExtractDocument
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.replay import Exchange, ReplayResponse, ReplayTransport

ENDPOINT = "https://docintel-test.cognitiveservices.azure.com"
ANALYZE = "/documentintelligence/documentModels/prebuilt-layout:analyze"
RESULT_PATH = "/documentintelligence/documentModels/prebuilt-layout/analyzeResults/result-1"
OPERATION = f"{ENDPOINT}{RESULT_PATH}?api-version=2024-11-30"

ANALYZE_RESULT: dict[str, Any] = {
    "apiVersion": "2024-11-30",
    "modelId": "prebuilt-layout",
    "stringIndexType": "unicodeCodePoint",
    "contentFormat": "markdown",
    "content": "# Title\n\nFirst page text.\n\n<!-- PageBreak -->\n\nSecond page text.",
    "pages": [
        {"pageNumber": 1, "angle": 0, "width": 8.5, "height": 11, "unit": "inch",
         "spans": [{"offset": 0, "length": 26}]},
        {"pageNumber": 2, "angle": 0, "width": 8.5, "height": 11, "unit": "inch",
         "spans": [{"offset": 48, "length": 10}, {"offset": 58, "length": 7}]},
    ],
}  # fmt: skip


class _Credential:
    async def get_token(self, *scopes: str, **options: Any) -> AccessToken:
        del scopes, options
        return AccessToken("token", 4_102_444_800)

    async def close(self) -> None:
        return None


class _SequenceTransport(ReplayTransport):
    """Serves its exchanges in order, one per request, so a poll can answer `running` first."""

    def __init__(self, exchanges: Sequence[Exchange | Exception]) -> None:
        super().__init__([exchange for exchange in exchanges if isinstance(exchange, Exchange)])
        self._queue = list(exchanges)
        self.sent: list[HttpRequest] = []

    @override
    async def send(self, request: HttpRequest, **kwargs: Any) -> ReplayResponse:
        del kwargs
        item = self._queue.pop(0)
        self.sent.append(request)
        if isinstance(item, Exception):
            raise item
        return ReplayResponse(request, item.response)


def exchange(method: str, path: str, status: int, json: Any = None, **headers: str) -> Exchange:
    response: dict[str, Any] = {"status": status, "headers": headers}
    if json is not None:
        response["json"] = json
    return Exchange.from_json({"request": {"method": method, "path": path}, "response": response})


def accepted() -> Exchange:
    return exchange("POST", ANALYZE, 202, **{"Operation-Location": OPERATION})


def poll(status: str, **headers: str) -> Exchange:
    body: dict[str, Any] = {"status": status, "createdDateTime": "2026-10-31T00:00:00Z",
                            "lastUpdatedDateTime": "2026-10-31T00:00:01Z"}  # fmt: skip
    if status == "succeeded":
        body["analyzeResult"] = ANALYZE_RESULT
    if status == "failed":
        body["error"] = {"code": "InternalServerError", "message": "analysis failed"}
    return exchange("GET", RESULT_PATH, 200, body, **headers)


def deleted() -> Exchange:
    return exchange("DELETE", RESULT_PATH, 204)


def error(status: int, code: str, inner: str | None = None, **headers: str) -> Exchange:
    body: dict[str, Any] = {"error": {"code": code, "message": "refused"}}
    if inner is not None:
        body["error"]["innererror"] = {"code": inner, "message": "detail"}
    return exchange("POST", ANALYZE, status, body, **headers)


class _Harness:
    def __init__(self, exchanges: Sequence[Exchange | Exception], **options: float) -> None:
        self.transport = _SequenceTransport(exchanges)
        self.clock = FakeClock()
        self.sleeps: list[float] = []
        self.client = DocumentIntelligenceClient(
            ENDPOINT,
            _Credential(),  # type: ignore[arg-type]
            transport=self.transport,
            retry_total=0,
        )
        self.layout = DocumentIntelligenceLayout(self.client, self.clock, self.sleep, **options)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    async def analyze(self) -> AnalyzeResult:
        return await self.layout.analyze(io.BytesIO(b"%PDF-1.7 bytes"))


async def test_t183_the_call_asks_for_markdown_with_code_point_offsets() -> None:
    harness = _Harness([accepted(), poll("running"), poll("succeeded"), deleted()])

    result = await harness.analyze()

    assert result.content == ANALYZE_RESULT["content"]
    post, first_poll, second_poll, delete = harness.transport.sent
    assert post.method == "POST"
    assert post.url.startswith(f"{ENDPOINT}{ANALYZE}?")
    query = post.url.split("?", 1)[1].split("&")
    assert sorted(query) == [
        "api-version=2024-11-30",
        "outputContentFormat=markdown",
        "stringIndexType=unicodeCodePoint",
    ]
    assert post.headers["Content-Type"] == "application/octet-stream"
    assert first_poll.method == second_poll.method == "GET"
    assert (delete.method, delete.url.split("?")[0]) == ("DELETE", f"{ENDPOINT}{RESULT_PATH}")
    # azure-core polls once at once, then waits before every further poll.
    assert harness.sleeps == [3]


async def test_polling_waits_for_a_longer_retry_after() -> None:
    harness = _Harness(
        [accepted(), poll("running", **{"Retry-After": "10"}), poll("succeeded"), deleted()]
    )

    await harness.analyze()

    assert harness.sleeps == [10]


async def test_polling_ends_at_the_40_minute_bound() -> None:
    harness = _Harness(
        [accepted(), *[poll("running") for _ in range(4)], deleted()], poll_limit_seconds=10
    )

    with pytest.raises(MethodFailedError) as failed:
        await harness.analyze()

    assert failed.value.code == "docintel_timeout"
    assert sum(harness.sleeps) <= 10
    assert harness.transport.sent[-1].method == "DELETE"


async def test_a_failed_analysis_is_docintel_failed() -> None:
    harness = _Harness([accepted(), poll("failed"), deleted()])

    with pytest.raises(MethodFailedError) as failed:
        await harness.analyze()

    assert failed.value.code == "docintel_failed"


@pytest.mark.parametrize(
    "response",
    [
        exchange("DELETE", RESULT_PATH, 500),
        exchange("DELETE", RESULT_PATH, 404, {"error": {"code": "NotFound", "message": "gone"}}),
    ],
)
async def test_a_failed_delete_is_ignored(response: Exchange) -> None:
    harness = _Harness([accepted(), poll("succeeded"), response])

    assert (await harness.analyze()).pages is not None


@pytest.mark.parametrize(
    ("response", "code", "retry_after"),
    [
        (error(429, "429", **{"Retry-After": "12"}), "docintel_throttled", 12),
        (error(503, "ServiceUnavailable", **{"Retry-After": "5"}), "docintel_unavailable", 5),
        (error(500, "InternalServerError"), "docintel_unavailable", None),
        (error(400, "InvalidRequest", "PasswordProtectedDocument"), "encrypted_document", None),
        (error(400, "passwordRequired"), "encrypted_document", None),
        (error(400, "InvalidRequest", "InvalidContent"), "corrupt_document", None),
        (error(400, "InvalidRequest", "InvalidContentLength"), "docintel_failed", None),
        (error(400, "InvalidArgument"), "docintel_failed", None),
    ],
)
async def test_t183_service_errors_map_to_their_codes(
    response: Exchange, code: str, retry_after: int | None, metric_points: Any
) -> None:
    harness = _Harness([response])

    with pytest.raises(MethodFailedError) as failed:
        await harness.analyze()

    assert (failed.value.code, failed.value.retry_after_seconds) == (code, retry_after)
    throttled = [
        point
        for point in metric_points("docchat.ingestion.throttled")
        if point.attributes.get("service") == "docintel"
    ]
    assert len(throttled) == (1 if code == "docintel_throttled" else 0)


async def test_a_connection_failure_is_unavailable() -> None:
    harness = _Harness([ServiceRequestError("no route")])

    with pytest.raises(MethodFailedError) as failed:
        await harness.analyze()

    assert failed.value.code == "docintel_unavailable"


async def test_an_unlisted_status_reaches_the_boundary() -> None:
    harness = _Harness([error(403, "AuthorizationFailed")])

    with pytest.raises(HttpResponseError):
        await harness.analyze()


def test_an_error_without_a_body_maps_by_status() -> None:
    failure = HttpResponseError(message="bad")
    failure.status_code = 400

    assert layout_failure(failure).code == "docintel_failed"


def test_the_extract_file_lists_every_span() -> None:
    document = extract_document(AnalyzeResult(ANALYZE_RESULT), 2)

    assert document.model_dump(mode="json") == {
        "formatVersion": 1,
        "method": "recursive",
        "pageCount": 2,
        "markdown": ANALYZE_RESULT["content"],
        "pageOffsets": [
            {"pageNumber": 1, "offset": 0, "length": 26},
            {"pageNumber": 2, "offset": 48, "length": 10},
            {"pageNumber": 2, "offset": 58, "length": 7},
        ],
        "docIntelPages": 2,
    }
    raw = document.model_dump_json()
    assert ExtractDocument.model_validate_json(raw, by_name=False) == document
    empty = extract_document(AnalyzeResult({"content": None, "pages": []}), 1)
    assert (empty.markdown, empty.page_offsets, empty.doc_intel_pages) == ("", [], 0)
