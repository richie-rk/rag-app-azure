"""Retrieval: the search request, partial semantic responses, the served-chunk-set filter and the
keep count (`12-chat-pipeline.md` sections 6 and 7; `82-security.md` SEC-16).

A pass is one hybrid keyword and vector search with semantic ranking on the turn's active physical
index, through the process's `SearchClient` for that index (`docchat_core.clients.search_client`,
an LRU of 16). Every field name comes from `ChunkIndexFields.for_version(schema_version)`, so the
request never names a field the index builder didn't create; it sends no filter, search fields,
captions, answers, count, facets, debug, query language or speller. The pager is read to its end.

After each pass, one SQL statement returns the chunk set each result's document serves in this
physical index; a result survives only when its chunk set is the served one and it is
well-formed (a page of 1 or more and a citable content type), so a document update or delete is
atomic for readers. Filtering runs before the cut: the first `retrieval.keep_count` survivors, in
result order, are kept and ranked from 1.

A response in which no result carries a reranker score is a partial semantic response (an empty
one included): the service's fused order stands, the confidence test is skipped and the turn
never broadens.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery, VectorQuery

from docchat_api.chat import telemetry
from docchat_api.chat.calls import elapsed_ms
from docchat_api.chat.sources import Candidate, SearchHit
from docchat_core.access import ResolvedIndex
from docchat_core.cosmos.conversations import SearchResultRecord
from docchat_core.models.clock import Clock
from docchat_core.opsconfig import RetrievalOps
from docchat_core.search.schema import SEMANTIC_CONFIGURATION_NAME, ChunkIndexFields
from docchat_core.sql.repositories.retrieval import ServedChunkSet
from docchat_core.telemetry import set_attributes, start_span

type SearchPassName = Literal["first", "broadened"]

BROADENED_WEIGHT: Final = 1.0
SEMANTIC_ERROR_MODE: Final = "partial"
QUERY_TYPE: Final = "semantic"


type SearchClients = Callable[[str], SearchClient]
"""The process's search client of a physical index name (`docchat_core.clients.search_client`);
a test passes clients over `ReplayTransport`."""

type ServedLookup = Callable[[UUID, Sequence[UUID]], Awaitable[Mapping[UUID, ServedChunkSet]]]
"""The served-chunk-set statement: the physical index and the documents' IDs."""


def select_fields(fields: ChunkIndexFields) -> list[str]:
    """The ten retrievable fields, in the request's order; never the vector field."""
    return [
        fields.key,
        fields.document_id,
        fields.document_version_id,
        fields.chunk_set_id,
        fields.chunk_ordinal,
        fields.title,
        fields.heading_path,
        fields.page_number,
        fields.content_type,
        fields.chunk_text,
    ]


def vector_queries(
    fields: ChunkIndexFields, vectors: Sequence[Sequence[float]], candidate_count: int
) -> list[VectorQuery]:
    """One query per vector over the index's vector field: the first pass's single query, or the
    broadened pass's two, each with weight 1.0."""
    weighted = len(vectors) > 1
    return [
        VectorizedQuery(
            vector=list(vector),
            k_nearest_neighbors=candidate_count,
            fields=fields.vector,
            exhaustive=False,
            weight=BROADENED_WEIGHT if weighted else None,
        )
        for vector in vectors
    ]


@dataclass(frozen=True, slots=True)
class PassResult:
    """One pass: every well-identified result with whether it survived, the kept candidates,
    whether the response was partial, and the two durations."""

    hits: tuple[SearchHit, ...]
    served: tuple[bool, ...]
    kept: tuple[Candidate, ...]
    partial: bool
    search_ms: int
    filter_ms: int

    def records(self) -> list[SearchResultRecord]:
        """The trace's per-result records of this pass."""
        return [
            SearchResultRecord(
                chunk_key=hit.chunk_key,
                document_id=hit.document_id,
                chunk_set_id=hit.chunk_set_id,
                search_score=hit.search_score,
                reranker_score=hit.reranker_score,
                served=served,
            )
            for hit, served in zip(self.hits, self.served, strict=True)
        ]


def confident(kept: Sequence[Candidate], retrieval: RetrievalOps) -> bool:
    """Whether at least `retrieval.min_confident_results` kept results reach
    `retrieval.reranker_threshold`; a null score never counts."""
    reaching = sum(
        1
        for candidate in kept
        if candidate.hit.reranker_score is not None
        and candidate.hit.reranker_score >= retrieval.reranker_threshold
    )
    return reaching >= retrieval.min_confident_results


class SearchFailedError(Exception):
    """The search request failed after its client's retries."""


class FilterFailedError(Exception):
    """The served-chunk-set statement failed."""


class Retriever:
    """The search and filter of one turn's index."""

    def __init__(
        self,
        *,
        clients: SearchClients,
        served: ServedLookup,
        index: ResolvedIndex,
        fields: ChunkIndexFields,
        retrieval: RetrievalOps,
        session_id: UUID,
        clock: Clock,
    ) -> None:
        self._clients = clients
        self._served = served
        self._index = index
        self._fields = fields
        self._retrieval = retrieval
        self._session_id = session_id
        self._clock = clock

    async def _search(
        self, query: str, vectors: Sequence[Sequence[float]], search_pass: SearchPassName
    ) -> list[SearchHit]:
        physical = self._index.physical_index
        candidate_count = self._retrieval.candidate_count
        started = self._clock.monotonic()
        with start_span(
            "docchat.chat.search",
            **{"docchat.physical_index_id": str(physical.id), "docchat.pass": search_pass},
        ) as span:
            try:
                client = self._clients(physical.name)
                results = await client.search(
                    search_text=query,
                    query_type=QUERY_TYPE,
                    semantic_configuration_name=SEMANTIC_CONFIGURATION_NAME[
                        physical.schema_version
                    ],
                    semantic_error_mode=SEMANTIC_ERROR_MODE,
                    semantic_max_wait_in_milliseconds=self._retrieval.semantic_max_wait_ms,
                    vector_queries=vector_queries(self._fields, vectors, candidate_count),
                    top=candidate_count,
                    select=select_fields(self._fields),
                    session_id=str(self._session_id),
                )
                hits: list[SearchHit] = []
                async for result in results:
                    hit = SearchHit.read(result, self._fields)
                    if hit is not None:
                        hits.append(hit)
            except Exception as failure:
                # Whatever the client raises after its own retries (a transport error, a status,
                # an undecodable body) ends this pass alike; the caller decides the turn's fate.
                raise SearchFailedError(type(failure).__name__) from failure
            finally:
                telemetry.record_search(elapsed_ms(self._clock, started), search_pass)
            partial = not any(hit.reranker_score is not None for hit in hits)
            set_attributes(
                span, **{"docchat.results": len(hits), "docchat.rerank_partial": partial}
            )
        return hits

    async def _filter(self, hits: Sequence[SearchHit]) -> tuple[list[bool], list[Candidate]]:
        if not hits:
            return [], []
        with start_span("docchat.chat.served_filter", **{"docchat.candidates": len(hits)}) as span:
            try:
                served = await self._served(
                    self._index.physical_index.id, [hit.document_id for hit in hits]
                )
            except Exception as failure:
                raise FilterFailedError(type(failure).__name__) from failure
            flags: list[bool] = []
            kept: list[Candidate] = []
            for hit in hits:
                row = served.get(hit.document_id)
                survives = row is not None and row.chunk_set_id == hit.chunk_set_id
                survives = survives and hit.well_formed
                flags.append(survives)
                if survives and row is not None and len(kept) < self._retrieval.keep_count:
                    kept.append(Candidate(rank=len(kept) + 1, hit=hit, source_name=row.source_name))
            set_attributes(span, **{"docchat.served": sum(flags)})
        return flags, kept

    async def run(
        self, query: str, vectors: Sequence[Sequence[float]], search_pass: SearchPassName
    ) -> PassResult:
        """One pass: search, then the filter and the keep count. Raises `SearchFailedError` or
        `FilterFailedError`."""
        started = self._clock.monotonic()
        hits = await self._search(query, vectors, search_pass)
        search_ms = elapsed_ms(self._clock, started)
        filtered = self._clock.monotonic()
        served, kept = await self._filter(hits)
        partial = not any(hit.reranker_score is not None for hit in hits)
        return PassResult(
            hits=tuple(hits),
            served=tuple(served),
            kept=tuple(kept),
            partial=partial,
            search_ms=search_ms,
            filter_ms=elapsed_ms(self._clock, filtered),
        )
