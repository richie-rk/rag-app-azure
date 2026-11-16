"""The ingestion queue after a run-creating commit (`14-ingestion.md` sections 2.8, 3.2, 3.3 and
18.4; `11-api.md` sections 3.10 and 3.11; ADR 0037).

Every API operation that creates a run commits the run and its items in one transaction, then
sends one `IngestionQueueMessage` `{runId, traceparent, tracestate}` with `send_run_message`. The
message is a hint: SQL holds the run, so a send that still fails after the Queue client's 3
retries emits the custom event `ingestion_queue_send_failed` (the run ID only) and the operation
answers its success status anyway; the dispatcher's next wake finds the queued items.

The operation runs inside the span `docchat.ingestion.submit` (`RunSubmission`). Its trace
context, taken once the span is current, is what the run stores and the message carries, so the
message's parent is a span of the request's trace, which continues the browser's `traceparent`.
A refusal leaves the span without recording the exception, because span events must never carry
exception text; the server span records the problem.

The queue client is the `ingestion-requests` client of the process's client registry (base64
message encoding, 3 retries waiting about 1, 3 and 5 seconds), taken through the `ingestion_queue`
dependency, which tests replace with `app.dependency_overrides`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Final, Protocol, Self, cast
from uuid import UUID

from azure.core.exceptions import AzureError
from opentelemetry.trace import INVALID_SPAN, Span

from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.models import IngestionQueueMessage, RunKind, TraceContext
from docchat_core.ingestion.names import INGESTION_QUEUE
from docchat_core.telemetry import emit_event, set_attributes, start_span

MESSAGE_TIME_TO_LIVE_SECONDS: Final = 86_400
"""After one day a message is worthless: the sweep finds every queued item anyway."""

SUBMIT_SPAN: Final = "docchat.ingestion.submit"
SEND_FAILED_EVENT: Final = "ingestion_queue_send_failed"


class IngestionQueuePort(Protocol):
    """The one call the API makes on the `ingestion-requests` queue."""

    async def send_message(self, content: str, *, time_to_live: int) -> object: ...


def ingestion_queue() -> IngestionQueuePort:
    """The process's `ingestion-requests` queue client, from the client registry the lifespan
    opened; endpoints take it with `Depends(ingestion_queue)`."""
    # The SDK's signature is wider than the port, which names the one call the API makes.
    return cast("IngestionQueuePort", ClientRegistry.current().queue(INGESTION_QUEUE))


async def send_run_message(
    queue: IngestionQueuePort, run_id: UUID, trace: TraceContext | None
) -> bool:
    """One `IngestionQueueMessage` for a committed run, sent with a time to live of one day;
    `False`, after `ingestion_queue_send_failed`, when the send failed after the client's
    retries. A request always runs in a valid trace context (the correlation middleware gives
    one); without one there is no message to send, and the sweep finds the run."""
    if trace is None:
        return False
    message = IngestionQueueMessage.for_run(run_id, trace)
    try:
        await queue.send_message(message.encode(), time_to_live=MESSAGE_TIME_TO_LIVE_SECONDS)
    except AzureError:
        emit_event(SEND_FAILED_EVENT, runId=str(run_id))
        return False
    return True


class RunSubmission:
    """The span `docchat.ingestion.submit` around a run-creating operation, and the trace
    context its run stores and its message carries.

        with RunSubmission() as submission:
            created = await create(..., trace=submission.trace)
            submission.record(index_id=..., run_id=..., kind=..., item_count=...)
            await submission.send(queue, created.run_id)
    """

    def __init__(self) -> None:
        self._scope: AbstractContextManager[Span] | None = None
        self.span: Span = INVALID_SPAN
        self.trace: TraceContext | None = None

    def __enter__(self) -> Self:
        self._scope = start_span(SUBMIT_SPAN)
        self.span = self._scope.__enter__()
        self.trace = TraceContext.current()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        scope, self._scope = self._scope, None
        if scope is not None:
            # The exception goes on to the problem handlers; the span ends without it.
            scope.__exit__(None, None, None)

    def record(self, *, index_id: UUID, run_id: UUID, kind: RunKind, item_count: int) -> None:
        """The span's attributes, once the run is committed."""
        set_attributes(
            self.span,
            **{
                "docchat.index_id": str(index_id),
                "docchat.run_id": str(run_id),
                "docchat.ingestion.run_kind": kind.value,
                "docchat.ingestion.item_count": item_count,
            },
        )

    async def send(self, queue: IngestionQueuePort, run_id: UUID) -> bool:
        """Send the run's message with the submission's trace context."""
        return await send_run_message(queue, run_id, self.trace)
