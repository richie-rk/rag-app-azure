"""`index_chunks`, the Index step (`14-ingestion.md` sections 6.7, 8.13 and 13.3; ADRs 0044 and
0045).

One search document per chunk, its field names from `ChunkIndexFields.for_version(<the physical
index's schema version>)`, so an index built under an earlier schema keeps receiving its own
names until a rebuild replaces it. Documents go up with `upload_documents()` (action `upload`,
which writes the whole document under its key whether or not it exists, so a retry overwrites
what an earlier attempt wrote) in batches of at most `indexBatchMaxDocuments` documents and
`indexBatchMaxBytes` bytes of UTF-8 JSON, one batch in flight. A document the service refused
with 409, 422, 429 or 503 is retried up to 3 times; anything else is `index_upload_failed`.

Only when the accepted documents equal the chunk set's `chunk_count` does the set become
`ready`, so a set is never served with chunks missing; a set retired meanwhile is
`chunk_set_retired` and the Flip never runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Final
from uuid import UUID

from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError
from opentelemetry.trace import Span
from pydantic import Field
from sqlalchemy import Connection, update

from docchat_core.ingestion.models import ActivityResult, ItemStage
from docchat_core.ingestion.names import embedding_batch_path
from docchat_core.search.schema import ChunkIndexFields, chunk_key
from docchat_core.sql.tables.ingestion import chunk_sets
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_ingestion import telemetry
from docchat_ingestion.activities.documents import (
    DEADLINES,
    ActivityOutput,
    BuildPlan,
    DocumentServices,
    ItemFailure,
    PlanInput,
    SearchDocumentsPort,
    ServicesProvider,
    blueprint,
    document_services,
    fence_and_read,
    item_ids,
    read_blob,
    run_document_activity,
)
from docchat_ingestion.activities.embed_chunks import form_batches, read_chunks
from docchat_ingestion.activities.support import literal, literals, retry_after_seconds
from docchat_ingestion.workfiles import (
    ChunkLine,
    WorkFileError,
    decode_embedding_batch,
    json_size,
)

NAME: Final = "index_chunks"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
DOCUMENT_RETRIES: Final = 3
RETRIED_STATUSES: Final = frozenset({409, 422, 429, 503})
THROTTLED_STATUSES: Final = frozenset({429, 503})
INDEX_UPLOAD_FAILED: Final = "index_upload_failed"
SEARCH_UNAVAILABLE: Final = "search_unavailable"
TARGET_MISSING: Final = "target_missing"
CHUNK_SET_RETIRED: Final = "chunk_set_retired"
BATCH_ENVELOPE_BYTES: Final = len('{"value":[]}')
"""The request body around the documents; each document after the first adds a comma."""


class IndexResult(ActivityResult):
    """`{indexed}` with the `ActivityResult` fields."""

    indexed: int = Field(default=0, ge=0)


def search_document(
    fields: ChunkIndexFields, plan: BuildPlan, chunk: ChunkLine, vector: list[float]
) -> dict[str, Any]:
    """The search document of one chunk (section 8.13)."""
    return {
        fields.key: chunk_key(plan.document_id, plan.chunk_set_id, chunk.ordinal),
        fields.document_id: str(plan.document_id),
        fields.document_version_id: str(plan.version_id),
        fields.chunk_set_id: str(plan.chunk_set_id),
        fields.chunk_ordinal: chunk.ordinal,
        fields.title: plan.title,
        fields.heading_path: list(chunk.heading_path),
        fields.chunk_text: chunk.chunk_text,
        fields.page_number: chunk.page_number,
        fields.content_type: str(plan.content_type),
        fields.vector: vector,
    }


def upload_batches(
    documents: Sequence[dict[str, Any]], max_documents: int, max_bytes: int
) -> list[list[dict[str, Any]]]:
    """Batches of at most `max_documents` documents and `max_bytes` bytes of the batch's UTF-8
    JSON, whichever comes first (section 13.3); a document is never split."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = BATCH_ENVELOPE_BYTES
    for document in documents:
        added = json_size(document) + (1 if current else 0)
        if current and (len(current) >= max_documents or size + added > max_bytes):
            batches.append(current)
            current, size = [], BATCH_ENVELOPE_BYTES
            added = json_size(document)
        current.append(document)
        size += added
    if current:
        batches.append(current)
    return batches


def build_documents(
    plan: BuildPlan,
    chunks: Sequence[ChunkLine],
    vectors: dict[int, list[float]],
    max_documents: int,
    max_bytes: int,
) -> list[list[dict[str, Any]]]:
    """Every chunk's search document, cut into upload batches; CPU-bound, so callers run it in a
    worker thread (section 4.4 rule 5)."""
    fields = ChunkIndexFields.for_version(plan.physical_index.schema_version)
    documents = [search_document(fields, plan, chunk, vectors[chunk.ordinal]) for chunk in chunks]
    return upload_batches(documents, max_documents, max_bytes)


async def read_vectors(
    services: DocumentServices, item: PlanInput, chunks: Sequence[ChunkLine]
) -> dict[int, list[float]]:
    """Every chunk's vector from Embed's batch files, which the same batching names."""
    plan = item.plan
    settings = item.settings
    vectors: dict[int, list[float]] = {}
    batches = form_batches(
        chunks, settings.embedding_batch_max_inputs, settings.embedding_batch_max_tokens
    )
    for batch in batches:
        path = embedding_batch_path(plan.work_prefix, plan.chunk_set_id, batch.number)
        data = await read_blob(services.work, path)
        if data is None:
            raise WorkFileError
        stored = await asyncio.to_thread(decode_embedding_batch, data)
        if stored is None or not stored.matches(
            batch.first_ordinal, len(batch.chunks), plan.physical_index.dimensions
        ):
            raise WorkFileError
        for offset, vector in enumerate(stored.vectors):
            vectors[batch.first_ordinal + offset] = vector
    return vectors


def _status(result: Any) -> int | None:
    status = getattr(result, "status_code", None)
    return status if isinstance(status, int) else None


async def upload(search: SearchDocumentsPort, batch: list[dict[str, Any]], key_field: str) -> int:
    """Upload one batch, retrying refused documents up to 3 times for 409, 422, 429 and 503;
    returns the number accepted."""
    pending = batch
    accepted = 0
    for attempt in range(DOCUMENT_RETRIES + 1):
        try:
            results = await search.upload_documents(documents=pending)
        except ResourceNotFoundError:
            raise ItemFailure(TARGET_MISSING) from None
        except HttpResponseError as error:
            if error.status_code in THROTTLED_STATUSES:
                telemetry.record_throttled("search")
            raise ItemFailure(
                SEARCH_UNAVAILABLE, retry_after_seconds=retry_after_seconds(error)
            ) from None
        except AzureError:
            raise ItemFailure(SEARCH_UNAVAILABLE) from None
        refused = {result.key: _status(result) for result in results if not bool(result.succeeded)}
        accepted += len(pending) - len(refused)
        if not refused:
            return accepted
        if attempt == DOCUMENT_RETRIES or any(
            status not in RETRIED_STATUSES for status in refused.values()
        ):
            raise ItemFailure(INDEX_UPLOAD_FAILED)
        pending = [document for document in pending if document[key_field] in refused]
    raise ItemFailure(INDEX_UPLOAD_FAILED)


def mark_ready(connection: Connection, chunk_set_id: UUID) -> bool:
    """`building` or `ready` to `ready`; `False` when the set was retired meanwhile."""
    changed = connection.execute(
        update(chunk_sets)
        .where(
            chunk_sets.c.id == chunk_set_id,
            chunk_sets.c.state.in_(literals(("building", "ready"))),
        )
        .values(state=literal("ready"), ready_at=SYSUTCDATETIME)
    ).rowcount
    return bool(changed == 1)


async def index(services: DocumentServices, item: PlanInput, span: Span) -> IndexResult:
    del span
    plan = item.plan
    settings = item.settings
    facts = await fence_and_read(services, item, ItemStage.INDEX, DEADLINE_SECONDS)
    chunks = await read_chunks(services, item)
    vectors = await read_vectors(services, item, chunks.chunks)
    batches = await asyncio.to_thread(
        build_documents,
        plan,
        chunks.chunks,
        vectors,
        settings.index_batch_max_documents,
        settings.index_batch_max_bytes,
    )
    search = services.search(plan.physical_index.name)
    key_field = ChunkIndexFields.for_version(plan.physical_index.schema_version).key
    accepted = 0
    for batch in batches:
        accepted += await upload(search, batch, key_field)
    if accepted != facts.chunk_count:
        raise ItemFailure(INDEX_UPLOAD_FAILED)
    ready = await services.sql.run(
        lambda connection: mark_ready(connection, plan.chunk_set_id),
        name="index_ready",
        retry="transient",
    )
    if not ready:
        raise ItemFailure(CHUNK_SET_RETIRED)
    return IndexResult.success(indexed=accepted)


def _failure(code: str, retry_after: int | None) -> IndexResult:
    return IndexResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        PlanInput,
        index,
        stage=ItemStage.INDEX,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def index_chunks(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(index_chunks)
