"""What the activities share (`14-ingestion.md` sections 4.4, 4.7, 4.8 and 4.9): the fenced lease,
the item span parented to the run's trace, span links, `Retry-After`, and the wire models."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from docchat_core.ingestion.models import ActivityResult, ItemEnvelope, ItemStage
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import support

ITEM = UUID("7d3e4f50-6172-4c83-ad94-b5c6d7e8f9a0")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
SETTINGS = {
    "activityTimeBudgetSeconds": 1200,
    "maxPagesPerDocument": 2000,
    "pagewisePagesPerExtract": 40,
    "maxParseSecondsPerDocument": 7200,
    "embeddingBatchMaxInputs": 64,
    "embeddingBatchMaxTokens": 60000,
    "embeddingConcurrency": 2,
    "indexBatchMaxDocuments": 100,
    "indexBatchMaxBytes": 8000000,
    "tableSummaryConcurrency": 4,
    "tableSummaryMaxInputTokens": 6000,
    "tableSummaryMaxOutputTokens": 300,
}


def _envelope(traceparent: str | None = TRACEPARENT) -> ItemEnvelope:
    return support.parse_envelope(
        {
            "itemId": str(ITEM),
            "claimNumber": 2,
            "runId": str(RUN),
            "traceparent": traceparent,
            "tracestate": "a=1" if traceparent else None,
            "settings": SETTINGS,
        }
    )


class Probe(support.WireModel):
    some_value: int


def test_wire_models_read_camel_case_only() -> None:
    assert Probe.from_wire({"someValue": 1}).some_value == 1
    assert support.wire(Probe(some_value=2)) == {"someValue": 2}
    with pytest.raises(ValidationError):
        Probe.from_wire({"some_value": 1})


def test_the_fenced_lease_update(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows(rowcount=1), fakes.Rows(rowcount=0)).connection

    assert support.fence_lease(connection, ITEM, 2, ItemStage.CREATE_INDEX, 300) is True
    assert support.fence_lease(connection, ITEM, 2, ItemStage.CREATE_INDEX, 300) is False
    statement = connection.executed[0]
    assert "dateadd(second," in statement.sql
    assert "claim_count = " in statement.sql
    assert "state = N'running'" in statement.sql
    assert statement.params["dateadd_1"] == 900
    assert statement.params["claim_count_1"] == 2


def test_dateadd_takes_only_known_parts() -> None:
    with pytest.raises(ValueError, match="date part"):
        support.dateadd("week", 1)


def test_a_catalogue_message_is_the_safe_message() -> None:
    assert support.catalogue_message("processing_interrupted").startswith("Processing stopped")


def test_remote_context_needs_a_valid_traceparent() -> None:
    assert support.remote_context(None, None) is None
    assert support.remote_context("00-" + "0" * 32 + "-" + "0" * 16 + "-01", None) is None
    assert support.remote_context(TRACEPARENT, "a=1") is not None


def test_run_links_are_one_per_distinct_traceparent() -> None:
    links = support.run_links([TRACEPARENT, None, TRACEPARENT, "garbage"])

    (link,) = links
    assert format(link.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_item_span_continues_the_runs_trace(telemetry: TestExporters) -> None:
    with support.item_span(
        "delete_search_index", _envelope(), **{"docchat.physical_index_id": RUN}
    ):
        pass

    (span,) = telemetry.spans.get_finished_spans()
    assert format(span.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span.parent is not None
    attributes = dict(span.attributes or {})
    assert attributes["docchat.item_id"] == str(ITEM)
    assert attributes["docchat.ingestion.claim"] == 2
    assert attributes["docchat.physical_index_id"] == str(RUN)


def test_an_item_span_of_a_run_without_context_starts_a_trace(telemetry: TestExporters) -> None:
    with support.item_span("purge_index_blobs", _envelope(None)):
        pass

    (span,) = telemetry.spans.get_finished_spans()
    assert span.parent is None


def test_record_outcome_keeps_lease_lost_off_the_span(telemetry: TestExporters) -> None:
    with support.item_span("purge_index_blobs", _envelope(None)) as span:
        support.record_outcome(span, ActivityResult.failure("lease_lost"))
    with support.item_span("purge_index_blobs", _envelope(None)) as span:
        support.record_outcome(span, ActivityResult.failure("storage_unavailable"))

    lost, failed = (dict(span.attributes or {}) for span in telemetry.spans.get_finished_spans())
    assert lost["docchat.outcome"] == "failed"
    assert "docchat.error_code" not in lost
    assert failed["docchat.error_code"] == "storage_unavailable"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after-ms": "1001"}, 2),
        ({"retry-after": "30"}, 30),
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, 0),
        ({"retry-after": "soon"}, None),
        ({}, None),
    ],
)
def test_retry_after_from_the_last_response(headers: dict[str, str], expected: int | None) -> None:
    error = SimpleNamespace(response=SimpleNamespace(headers=headers))

    assert support.retry_after_seconds(error) == expected  # type: ignore[arg-type]


def test_an_error_without_a_response_has_no_retry_after() -> None:
    assert support.retry_after_seconds(ValueError("x")) is None


@pytest.mark.parametrize(
    ("status", "expected"), [(429, True), (503, True), (500, True), (409, False), (None, False)]
)
def test_service_unavailable(status: int | None, expected: bool) -> None:
    error: Any = ValueError("x")
    error.status_code = status
    assert support.service_unavailable(error) is expected


async def test_run_item_activity_refuses_a_bad_envelope(telemetry: TestExporters) -> None:
    async def body(envelope: ItemEnvelope, span: Any) -> ActivityResult:
        raise AssertionError("never called")

    result = await support.run_item_activity("create_search_index", 300, {"x": 1}, body)

    assert result["errorCode"] == "internal_error"
    assert telemetry.spans.get_finished_spans() == ()
