from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import INVALID_SPAN

from docchat_api.http.queue import (
    MESSAGE_TIME_TO_LIVE_SECONDS,
    SUBMIT_SPAN,
    RunSubmission,
    ingestion_queue,
    send_run_message,
)
from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.models import IngestionQueueMessage, RunKind, TraceContext
from docchat_testing.fakes.queue import RecordingQueue

INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
RUN = UUID("9c4d5e6f-7a8b-4c9d-8e1f-3a4b5c6d7e63")
TRACE = TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "a=1")


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


async def test_the_message_names_the_run_and_its_trace_context() -> None:
    queue = RecordingQueue()

    assert await send_run_message(queue, RUN, TRACE) is True

    (sent,) = queue.sent
    assert sent.time_to_live == MESSAGE_TIME_TO_LIVE_SECONDS == 86_400
    assert sent.decoded() == IngestionQueueMessage(
        run_id=RUN, traceparent=TRACE.traceparent, tracestate="a=1"
    )


async def test_an_empty_tracestate_is_left_out() -> None:
    queue = RecordingQueue()

    await send_run_message(queue, RUN, TraceContext(TRACE.traceparent))

    assert "tracestate" not in queue.sent[0].content


async def test_a_failed_send_is_reported_and_answers_false(captured: Captured) -> None:
    queue = RecordingQueue(failing=True)

    assert await send_run_message(queue, RUN, TRACE) is False

    assert captured.events("ingestion_queue_send_failed") == [{"runId": str(RUN)}]


async def test_without_a_trace_context_nothing_is_sent(captured: Captured) -> None:
    queue = RecordingQueue()

    assert await send_run_message(queue, RUN, None) is False

    assert queue.sent == []
    assert captured.events("ingestion_queue_send_failed") == []


async def test_the_queue_comes_from_the_client_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = RecordingQueue()

    class Registry:
        def queue(self, name: str) -> RecordingQueue:
            assert name == "ingestion-requests"
            return queue

    monkeypatch.setattr(ClientRegistry, "current", classmethod(lambda cls: Registry()))

    assert ingestion_queue() is queue


async def test_the_submission_span_parents_the_message_and_carries_the_run(
    captured: Captured,
) -> None:
    queue = RecordingQueue()

    with RunSubmission() as submission:
        assert submission.trace is not None
        submission.record(index_id=INDEX, run_id=RUN, kind=RunKind.UPLOAD, item_count=2)
        await submission.send(queue, RUN)

    (span,) = captured.finished_spans(SUBMIT_SPAN)
    assert span.attributes == {
        "docchat.index_id": str(INDEX),
        "docchat.run_id": str(RUN),
        "docchat.ingestion.run_kind": "upload",
        "docchat.ingestion.item_count": 2,
    }
    context = span.get_span_context()
    assert context is not None
    expected = f"00-{context.trace_id:032x}-{context.span_id:016x}-"
    assert queue.messages()[0].traceparent.startswith(expected)


def test_a_refusal_ends_the_span_without_the_exception(captured: Captured) -> None:
    with pytest.raises(LookupError), RunSubmission():
        raise LookupError("cnry-instruction-5e1f0a77")

    (span,) = captured.finished_spans(SUBMIT_SPAN)
    assert span.events == ()
    assert span.status.is_ok


def test_before_entering_the_submission_has_no_span() -> None:
    submission = RunSubmission()

    assert submission.span is INVALID_SPAN
    assert submission.trace is None
    submission.__exit__(None, None, None)
