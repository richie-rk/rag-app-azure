"""`chunk_document`, the Chunk step (`14-ingestion.md` sections 6.5, 10.6 to 10.9 and 11.2 to
11.6; ADR 0046).

The method's chunker reads the plan's extract files and the summaries file and writes
`chunks/{chunkSetId}.jsonl`, its CPU-bound work off the event loop; this step then records the
chunk count on a set that is still `building` (zero rows means an earlier claim already indexed
this set, whose count is kept) and fails a build with no chunk (`no_extractable_text`) or with
more than 100,000 (`chunk_limit_exceeded`).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from opentelemetry.trace import Span
from pydantic import Field
from sqlalchemy import Connection, update

from docchat_core.ingestion.models import ActivityResult, ItemStage
from docchat_core.sql.tables.ingestion import chunk_sets
from docchat_ingestion.activities.documents import (
    CHUNK_LIMIT,
    DEADLINES,
    ActivityOutput,
    DocumentServices,
    ItemFailure,
    PlanInput,
    ServicesProvider,
    blueprint,
    document_services,
    fence_and_read,
    item_ids,
    method_context,
    registered,
    run_document_activity,
)
from docchat_ingestion.activities.support import literal

NAME: Final = "chunk_document"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
NO_EXTRACTABLE_TEXT: Final = "no_extractable_text"
CHUNK_LIMIT_EXCEEDED: Final = "chunk_limit_exceeded"


class ChunkResult(ActivityResult):
    """`{chunkCount}` with the `ActivityResult` fields."""

    chunk_count: int | None = Field(default=None, ge=0)


def record_chunk_count(connection: Connection, chunk_set_id: UUID, chunk_count: int) -> None:
    connection.execute(
        update(chunk_sets)
        .where(chunk_sets.c.id == chunk_set_id, chunk_sets.c.state == literal("building"))
        .values(chunk_count=chunk_count)
    )


async def chunk(services: DocumentServices, item: PlanInput, span: Span) -> ChunkResult:
    del span
    plan = item.plan
    facts = await fence_and_read(services, item, ItemStage.CHUNK, DEADLINE_SECONDS)
    implementation = registered(services, plan).implementation
    result = await implementation.chunk(
        method_context(item, facts), plan.method.validated_parameters()
    )
    count = result.chunk_count
    if count == 0:
        raise ItemFailure(NO_EXTRACTABLE_TEXT)
    if count > CHUNK_LIMIT:
        raise ItemFailure(CHUNK_LIMIT_EXCEEDED)
    await services.sql.run(
        lambda connection: record_chunk_count(connection, plan.chunk_set_id, count),
        name="chunk_count",
        retry="transient",
    )
    return ChunkResult.success(chunk_count=count)


def _failure(code: str, retry_after: int | None) -> ChunkResult:
    return ChunkResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        PlanInput,
        chunk,
        stage=ItemStage.CHUNK,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def chunk_document(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(chunk_document)
