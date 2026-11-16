"""`embed_chunks`, the Embed step (`14-ingestion.md` sections 6.6, 13.1 and 13.2; ADRs 0050 and
0067).

The chunks file is cut into batches greedily in ordinal order, at most
`embeddingBatchMaxInputs` inputs and `embeddingBatchMaxTokens` tokens each (counted from each
chunk's `tokenCount`), so the same file always gives the same batches. A batch whose file
already holds its first ordinal, its vector count and the index's dimension count is reused;
the others are embedded with at most `embeddingConcurrency` requests in flight, each vector's
length checked, and written to `embeddings/{chunkSetId}/batch-{n:05d}.json`. After
`activityTimeBudgetSeconds` of work no new batch starts and the step returns `done: false`; the
orchestrator calls it once more.

The input is exactly each chunk's `chunkText`, so the vector always describes the text a reader
gets, and `dimensions` is always sent. A `400` is `embedding_failed`; a `429` or a `5xx` or
timeout after the client's own retries is the retryable `embedding_throttled` or
`embedding_unavailable`, with the service's `Retry-After`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import openai
from opentelemetry.trace import Span
from pydantic import Field

from docchat_core.ingestion.models import ActivityResult, ItemStage
from docchat_core.ingestion.names import chunks_path, embedding_batch_path
from docchat_ingestion import telemetry
from docchat_ingestion.activities.documents import (
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
    read_blob,
    run_bounded,
    run_document_activity,
    write_blob,
)
from docchat_ingestion.activities.support import retry_after_seconds
from docchat_ingestion.workfiles import (
    ChunkLine,
    ChunksFile,
    EmbeddingBatch,
    WorkFileError,
    decode_chunks,
    decode_embedding_batch,
)

NAME: Final = "embed_chunks"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
EMBEDDING_FAILED: Final = "embedding_failed"
EMBEDDING_THROTTLED: Final = "embedding_throttled"
EMBEDDING_UNAVAILABLE: Final = "embedding_unavailable"
ENCODING_FORMAT: Final = "float"
FIRST_SERVER_ERROR: Final = 500
BAD_REQUEST: Final = 400
TOO_MANY_REQUESTS: Final = 429


class EmbedResult(ActivityResult):
    """`{done, embeddingTokens}` with the `ActivityResult` fields; the tokens are this call's."""

    done: bool = False
    embedding_tokens: int = Field(default=0, ge=0)


@dataclass(frozen=True, slots=True)
class Batch:
    """Batch `number` (0-based): its chunks, in ordinal order."""

    number: int
    chunks: tuple[ChunkLine, ...]

    @property
    def first_ordinal(self) -> int:
        return self.chunks[0].ordinal


def form_batches(chunks: Sequence[ChunkLine], max_inputs: int, max_tokens: int) -> list[Batch]:
    """Greedy batches in ordinal order: a chunk that would take a batch over either cap starts
    the next one (section 13.2 item 1)."""
    batches: list[Batch] = []
    current: list[ChunkLine] = []
    tokens = 0
    for chunk in chunks:
        if current and (len(current) >= max_inputs or tokens + chunk.token_count > max_tokens):
            batches.append(Batch(len(batches), tuple(current)))
            current, tokens = [], 0
        current.append(chunk)
        tokens += chunk.token_count
    if current:
        batches.append(Batch(len(batches), tuple(current)))
    return batches


async def read_chunks(services: DocumentServices, item: PlanInput) -> ChunksFile:
    """The plan's chunks file, decoded in a worker thread; it must exist, decode and belong to
    this chunk set, or the step fails with `WorkFileError`."""
    plan = item.plan
    data = await read_blob(services.work, chunks_path(plan.work_prefix, plan.chunk_set_id))
    if data is None:
        raise WorkFileError
    try:
        chunks = await asyncio.to_thread(decode_chunks, data)
    except (ValueError, UnicodeDecodeError):
        raise WorkFileError from None
    if chunks.header.chunk_set_id != plan.chunk_set_id:
        raise WorkFileError
    return chunks


def _failure_of(error: Exception, deployment: str) -> ItemFailure:
    """The item code of an embeddings failure after the client's own retries."""
    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        if status == TOO_MANY_REQUESTS:
            telemetry.record_throttled("openai", deployment)
            return ItemFailure(EMBEDDING_THROTTLED, retry_after_seconds=retry_after_seconds(error))
        if status >= FIRST_SERVER_ERROR:
            return ItemFailure(
                EMBEDDING_UNAVAILABLE, retry_after_seconds=retry_after_seconds(error)
            )
        if status == BAD_REQUEST:
            return ItemFailure(EMBEDDING_FAILED)
    if isinstance(error, openai.APITimeoutError | openai.APIConnectionError):
        return ItemFailure(EMBEDDING_UNAVAILABLE)
    raise error


@dataclass(slots=True)
class Embedder:
    services: DocumentServices
    item: PlanInput
    budget_ends: float
    tokens: int = 0
    written: int = 0

    @property
    def deployment(self) -> str:
        return self.item.plan.physical_index.ingestion_deployment

    @property
    def dimensions(self) -> int:
        return self.item.plan.physical_index.dimensions

    def path(self, batch: Batch) -> str:
        plan = self.item.plan
        return embedding_batch_path(plan.work_prefix, plan.chunk_set_id, batch.number)

    async def reusable(self, batch: Batch) -> bool:
        """A batch file that exists with the batch's first ordinal, vector count and the index's
        dimension count is reused on a retry."""
        data = await read_blob(self.services.work, self.path(batch))
        if data is None:
            return False
        stored = await asyncio.to_thread(decode_embedding_batch, data)
        return stored is not None and stored.matches(
            batch.first_ordinal, len(batch.chunks), self.dimensions
        )

    async def embed(self, batch: Batch) -> None:
        try:
            response: Any = await self.services.openai.embeddings.create(
                model=self.deployment,
                input=[chunk.chunk_text for chunk in batch.chunks],
                dimensions=self.dimensions,
                encoding_format=ENCODING_FORMAT,
            )
        except openai.OpenAIError as error:
            raise _failure_of(error, self.deployment) from None
        vectors = _vectors(response, len(batch.chunks), self.dimensions)
        prompt_tokens = int(response.usage.prompt_tokens)
        stored = EmbeddingBatch(
            first_ordinal=batch.first_ordinal, vectors=vectors, prompt_tokens=prompt_tokens
        )
        data = await asyncio.to_thread(stored.encode)
        await write_blob(self.services.work, self.path(batch), data)
        self.tokens += prompt_tokens
        self.written += 1
        telemetry.record_tokens(self.deployment, "embedding", prompt_tokens)


def _vectors(response: Any, count: int, dimensions: int) -> list[list[float]]:
    """The vectors in input order, matched by `data[i].index`; any vector of another length than
    the index's dimensions is `embedding_failed`."""
    vectors: list[list[float] | None] = [None] * count
    for entry in response.data:
        index = int(entry.index)
        if not 0 <= index < count or vectors[index] is not None:
            raise ItemFailure(EMBEDDING_FAILED)
        vector = [float(value) for value in entry.embedding]
        if len(vector) != dimensions:
            raise ItemFailure(EMBEDDING_FAILED)
        vectors[index] = vector
    if any(vector is None for vector in vectors):
        raise ItemFailure(EMBEDDING_FAILED)
    return [vector for vector in vectors if vector is not None]


async def embed(services: DocumentServices, item: PlanInput, span: Span) -> EmbedResult:
    del span
    settings = item.settings
    await fence_and_read(services, item, ItemStage.EMBED, DEADLINE_SECONDS)
    budget_ends = services.clock.monotonic() + settings.activity_time_budget_seconds
    chunks = await read_chunks(services, item)
    batches = form_batches(
        chunks.chunks, settings.embedding_batch_max_inputs, settings.embedding_batch_max_tokens
    )
    embedder = Embedder(services, item, budget_ends)
    pending = [batch for batch in batches if not await embedder.reusable(batch)]

    async def one(batch: Batch) -> None:
        # In-flight requests finish after the budget; no new one starts.
        if services.clock.monotonic() < budget_ends:
            await embedder.embed(batch)

    await run_bounded(pending, settings.embedding_concurrency, one)
    done = embedder.written == len(pending)
    return EmbedResult.success(done=done, embedding_tokens=embedder.tokens)


def _failure(code: str, retry_after: int | None) -> EmbedResult:
    return EmbedResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        PlanInput,
        embed,
        stage=ItemStage.EMBED,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def embed_chunks(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(embed_chunks)
