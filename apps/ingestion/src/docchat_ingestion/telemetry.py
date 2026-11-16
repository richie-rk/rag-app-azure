"""The ingestion app's instruments and its custom-event helpers (`14-ingestion.md` section 18.4;
`85-observability.md` sections 4.6, 4.11 and 4.12; ADR 0137).

Every metric of the ingestion area is declared once here from the attribute registry, so its
unit and dimension keys come from `docchat_core.telemetry.attributes.METRICS`; the instrument
factory returns the same instrument to every module that names it. Dimensions are closed-set
values only: an operation, a state, a stage, a method key, a code, a deployment name or a
service, never an identifier, a name or content. Metrics are never sampled.
"""

from __future__ import annotations

from typing import Final, Literal
from uuid import UUID

from docchat_core.ingestion.models import ItemOperation, ItemStage, ItemState
from docchat_core.telemetry import emit_event
from docchat_core.telemetry.instruments import counter, gauge, histogram

ACTIVITY_SPAN: Final = "docchat.ingestion.activity"
"""The span every item activity runs in, parented to its run's trace (section 4.9)."""

NO_CODE: Final = "none"
"""The `outcome_code` dimension of an item that succeeded, which stores no code."""

ITEMS_COMPLETED: Final = counter("docchat.ingestion.items.completed")
ITEM_DURATION: Final = histogram("docchat.ingestion.item.duration")
STAGE_DURATION: Final = histogram("docchat.ingestion.stage.duration")
ITEMS_QUEUED: Final = gauge("docchat.ingestion.items.queued")
ITEMS_RUNNING: Final = gauge("docchat.ingestion.items.running")
QUEUE_OLDEST_AGE: Final = gauge("docchat.ingestion.queue.oldest_age")
DISPATCHER_HEARTBEAT_AGE: Final = gauge("docchat.ingestion.dispatcher.heartbeat_age")
POISON_QUEUE_LENGTH: Final = gauge("docchat.ingestion.poison_queue.length")
TOKENS: Final = counter("docchat.ingestion.tokens")
DOCINTEL_PAGES: Final = counter("docchat.ingestion.docintel.pages")
THROTTLED: Final = counter("docchat.ingestion.throttled")
SCAN_RESULTS: Final = counter("docchat.ingestion.scan.results")
PARSE_PEAK_RSS: Final = histogram("docchat.ingestion.parse.peak_rss")
CONVERT_DURATION: Final = histogram("docchat.ingestion.convert.duration")
CONVERT_PAGES: Final = histogram("docchat.ingestion.convert.pages")

type TokenKind = Literal["embedding", "summary_input", "summary_output"]
type ThrottledService = Literal["openai", "docintel", "search"]
type ConvertOutcome = Literal["converted", "reused", "timeout", "memory", "failed"]


def record_stage(stage: ItemStage, method_key: str | None, seconds: float) -> None:
    """`docchat.ingestion.stage.duration` of one activity call; steps without a method (the
    document clean-up item, a re-render) record the stage alone."""
    dimensions: dict[str, object] = {"stage": stage.value}
    if method_key is not None:
        dimensions["method_key"] = method_key
    STAGE_DURATION.record(max(seconds, 0.0), **dimensions)


def record_item_completed(
    operation: ItemOperation, state: ItemState, code: str | None, seconds: float | None
) -> None:
    """`docchat.ingestion.items.completed` and, when the item's start is known,
    `docchat.ingestion.item.duration` (section 6.11)."""
    ITEMS_COMPLETED.add(
        1, operation=operation.value, state=state.value, outcome_code=code or NO_CODE
    )
    if seconds is not None:
        ITEM_DURATION.record(max(seconds, 0.0), operation=operation.value, state=state.value)


def record_tokens(deployment: str, kind: TokenKind, tokens: int) -> None:
    """`docchat.ingestion.tokens`, as billed: reasoning tokens count as output (section 12.10)."""
    if tokens > 0:
        TOKENS.add(tokens, deployment=deployment, kind=kind)


def record_throttled(service: ThrottledService, deployment: str | None = None) -> None:
    """`docchat.ingestion.throttled`: one count each time a service throttles an item activity
    (section 4.7 item 8). Search has no deployment."""
    dimensions: dict[str, object] = {"service": service}
    if deployment is not None:
        dimensions["deployment"] = deployment
    THROTTLED.add(1, **dimensions)


def record_docintel_pages(pages: int) -> None:
    if pages > 0:
        DOCINTEL_PAGES.add(pages)


def record_conversion(
    content_type: str, outcome: ConvertOutcome, seconds: float, pages: int | None = None
) -> None:
    """Extract's conversion call: `docchat.ingestion.convert.duration` and, for a PDF it
    converted or reused, `docchat.ingestion.convert.pages` (section 6.3 step 3)."""
    CONVERT_DURATION.record(max(seconds, 0.0), content_type=content_type, outcome=outcome)
    if pages is not None:
        CONVERT_PAGES.record(pages, content_type=content_type)


def item_failed(
    *, item_id: UUID, run_id: UUID, operation: ItemOperation, stage: ItemStage, code: str
) -> None:
    """The custom event `ingestion_item_failed` (section 6.11)."""
    emit_event(
        "ingestion_item_failed",
        itemId=str(item_id),
        runId=str(run_id),
        operation=operation.value,
        stage=stage.value,
        outcomeCode=code,
    )
