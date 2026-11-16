"""`record_item_outcome`, the last activity of both item sub-orchestrations (`14-ingestion.md`
sections 6.11, 6.13 and 4.8; ADR 0034).

One transaction fenced by the claim number: the item's state, code and safe message, its last
stage, its first 50 warnings and its usage counts, `completed_at`, and no lease. Zero rows means
the claim was lost and nothing is written. For a failure the item's build is retired when still
`building` or `ready`, and its version fails when it is `pending` or `ingesting` and the item
targets the logical index's active physical index (or, with no active one, its building one), so
a failed re-ingest or rebuild of a current version changes only the item and its chunk set, and
a served version never shows `failed`. A failed `rerender` item names a served chunk set of a
version that is neither, so neither update changes a row. Then the item counts in
`docchat.ingestion.items.completed` and `docchat.ingestion.item.duration`, and a failure emits
`ingestion_item_failed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from opentelemetry import context as otel_context
from sqlalchemy import Connection, update

from docchat_core.ingestion.models import (
    LEASE_LOST,
    ActivityResult,
    ItemOperation,
    ItemStage,
    encode_item_warnings,
)
from docchat_core.ingestion.runs import apply_failure_side_effects
from docchat_core.sql.engine import SqlUnavailable
from docchat_core.sql.tables.ingestion import ingestion_run_items
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_core.telemetry import start_span
from docchat_ingestion import telemetry
from docchat_ingestion.activities.documents import (
    DATABASE_UNAVAILABLE,
    DEADLINES,
    ActivityOutput,
    ServicesProvider,
    blueprint,
    document_services,
    parse_input,
)
from docchat_ingestion.activities.runtime import SqlRunner
from docchat_ingestion.activities.support import (
    catalogue_message,
    literal,
    record_outcome,
    remote_context,
    wire,
)
from docchat_ingestion.boundary import INTERNAL_ERROR, guard, run_activity
from docchat_ingestion.outcomes import ItemOutcome

NAME: Final = "record_item_outcome"
DEADLINE_SECONDS: Final = DEADLINES[NAME]

_items = ingestion_run_items


@dataclass(frozen=True, slots=True)
class RecordedItem:
    """What the fenced update returned about the item."""

    run_id: UUID
    operation: ItemOperation
    stage: ItemStage | None
    started_at: datetime | None
    completed_at: datetime | None


def _utc(value: datetime | None) -> datetime | None:
    # `DATETIME2(3)` columns hold UTC without an offset.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def record(connection: Connection, outcome: ItemOutcome) -> RecordedItem | None:
    """The outcome transaction; `None` when the claim was lost."""
    warnings = outcome.kept_warnings()
    usage = outcome.usage
    row = connection.execute(
        update(_items)
        .where(
            _items.c.id == outcome.item_id,
            _items.c.claim_count == outcome.claim_number,
            _items.c.state == literal("running"),
        )
        .values(
            state=literal(outcome.state),
            outcome_code=outcome.code,
            outcome_message=None if outcome.code is None else catalogue_message(outcome.code),
            stage=_items.c.stage if outcome.stage is None else outcome.stage.value,
            warnings=encode_item_warnings(warnings) if warnings else None,
            embedding_tokens=usage.embedding_tokens,
            summary_input_tokens=usage.summary_input_tokens,
            summary_output_tokens=usage.summary_output_tokens,
            docintel_pages=usage.doc_intel_pages,
            completed_at=SYSUTCDATETIME,
            lease_expires_at=None,
        )
        .returning(
            _items.c.run_id,
            _items.c.operation,
            _items.c.stage,
            _items.c.started_at,
            _items.c.completed_at,
        )
    ).first()
    if row is None:
        return None
    if outcome.state == "failed" and outcome.code is not None:
        apply_failure_side_effects(connection, outcome.item_id, outcome.code)
    return RecordedItem(
        run_id=row.run_id,
        operation=ItemOperation(row.operation),
        stage=None if row.stage is None else ItemStage(row.stage),
        started_at=_utc(row.started_at),
        completed_at=_utc(row.completed_at),
    )


def report(outcome: ItemOutcome, recorded: RecordedItem) -> None:
    """The item's metrics and, for a failure, `ingestion_item_failed`."""
    seconds = None
    if recorded.started_at is not None and recorded.completed_at is not None:
        seconds = (recorded.completed_at - recorded.started_at).total_seconds()
    telemetry.record_item_completed(recorded.operation, outcome.item_state, outcome.code, seconds)
    if outcome.state == "failed" and outcome.code is not None and recorded.stage is not None:
        telemetry.item_failed(
            item_id=outcome.item_id,
            run_id=recorded.run_id,
            operation=recorded.operation,
            stage=recorded.stage,
            code=outcome.code,
        )


async def record_outcome_of(sql: SqlRunner, outcome: ItemOutcome) -> ActivityResult:
    recorded = await sql.run(
        lambda connection: record(connection, outcome),
        name="record_item_outcome",
        retry="transient",
    )
    if recorded is None:
        return ActivityResult.failure(LEASE_LOST)
    report(outcome, recorded)
    return ActivityResult.success()


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    """The activity inside its failure boundary, deadline and span; its input carries the run's
    trace context when the sub-orchestration sends the item envelope."""
    parsed: ItemOutcome | None = None
    async with guard(NAME):
        parsed = parse_input(ItemOutcome, payload)
    if parsed is None:
        return wire(ActivityResult.failure(INTERNAL_ERROR))
    outcome = parsed
    parent = remote_context(outcome.traceparent, outcome.tracestate)
    token = None if parent is None else otel_context.attach(parent)
    try:
        attributes: dict[str, object] = {
            "docchat.activity_name": NAME,
            "docchat.item_id": str(outcome.item_id),
            "docchat.ingestion.claim": outcome.claim_number,
        }
        if outcome.run_id is not None:
            attributes["docchat.run_id"] = str(outcome.run_id)
        with start_span(telemetry.ACTIVITY_SPAN, **attributes) as span:

            async def body() -> ActivityResult:
                services = await provider()
                try:
                    return await record_outcome_of(services.sql, outcome)
                except SqlUnavailable:
                    return ActivityResult.failure(DATABASE_UNAVAILABLE)

            result = await run_activity(NAME, DEADLINE_SECONDS, body, ActivityResult.failure)
            record_outcome(span, result)
            return wire(result)
    finally:
        if token is not None:
            otel_context.detach(token)


async def record_item_outcome(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(record_item_outcome)
