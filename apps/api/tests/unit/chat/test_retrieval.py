"""The search request, partial responses, the served-chunk-set filter and the keep count (T-232
and the unit half of T-233/T-234; `12-chat-pipeline.md` sections 6 and 7.1).

The search requests go through the real async `SearchClient` given `ReplayTransport`, so the
request shape on the wire (`vectorQueries`, `select`, the semantic configuration) is what these
tests check."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient

from docchat_api.chat.retrieval import (
    FilterFailedError,
    Retriever,
    SearchFailedError,
    confident,
    select_fields,
)
from docchat_core.access import PhysicalIndex, ResolvedIndex
from docchat_core.clients import SEARCH_API_VERSION
from docchat_core.opsconfig import RetrievalOps
from docchat_core.search.schema import ChunkIndexFields
from docchat_core.sql.repositories.retrieval import ServedChunkSet
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.replay import Exchange, ReplayTransport

FIELDS = ChunkIndexFields.for_version(1)
NAME = "docchat-0123456789abcdef0123456789abcdef-g1"
SESSION = UUID("7c2d0000-0000-4000-8000-000000000001")
INDEX = ResolvedIndex(
    search_index_id=uuid4(),
    source="project_default",
    override_expires_at=None,
    physical_index=PhysicalIndex(
        id=uuid4(),
        name=NAME,
        schema_version=1,
        embedding_model="text-embedding-3-large",
        embedding_dimensions=3,
        query_embedding_deployment="embed-query-te3-large-v1",
    ),
)
RETRIEVAL = RetrievalOps.model_validate(
    {
        "retrieval.candidate_count": 50,
        "retrieval.keep_count": 8,
        "retrieval.reranker_threshold": 2.0,
        "retrieval.min_confident_results": 1,
        "retrieval.semantic_max_wait_ms": 2000,
        "retrieval.broaden_timeout_seconds": 8,
    }
)


def wire_result(
    document: UUID, chunk_set: UUID, *, page: int | None = 1, reranker: float | None = 3.0
) -> dict[str, Any]:
    return {
        "@search.score": 1.5,
        "@search.rerankerScore": reranker,
        "id": f"{document}_{chunk_set}_0",
        "documentId": str(document),
        "documentVersionId": str(uuid4()),
        "chunkSetId": str(chunk_set),
        "chunkOrdinal": 0,
        "title": "Title",
        "headingPath": ["Heading"],
        "pageNumber": page,
        "contentType": "application/pdf",
        "chunkText": "Chunk",
    }


def transport_for(*pages: Sequence[Mapping[str, Any]]) -> ReplayTransport:
    exchanges = [
        Exchange.from_json(
            {
                "request": {
                    "method": "POST",
                    "path": f"/indexes('{NAME}')/docs/search.post.search",
                    "query": {"api-version": SEARCH_API_VERSION},
                },
                "response": {"status": 200, "json": {"value": list(page)}},
            }
        )
        for page in pages
    ]
    return ReplayTransport(exchanges)


class Served:
    def __init__(
        self, rows: Mapping[UUID, ServedChunkSet], failure: Exception | None = None
    ) -> None:
        self.rows = rows
        self.failure = failure
        self.calls: list[tuple[UUID, list[UUID]]] = []

    async def __call__(
        self, physical: UUID, documents: Sequence[UUID]
    ) -> Mapping[UUID, ServedChunkSet]:
        self.calls.append((physical, list(documents)))
        if self.failure is not None:
            raise self.failure
        return self.rows


def served_row(document: UUID, chunk_set: UUID) -> ServedChunkSet:
    return ServedChunkSet(
        document_id=document,
        chunk_set_id=chunk_set,
        document_version_id=uuid4(),
        source_name="doc.pdf",
    )


def retriever(transport: ReplayTransport, served: Served) -> Retriever:
    client = SearchClient(
        "https://search.example.invalid",
        NAME,
        AzureKeyCredential("unused"),
        api_version=SEARCH_API_VERSION,
        transport=transport,
    )
    return Retriever(
        clients=lambda name: client,
        served=served,
        index=INDEX,
        fields=FIELDS,
        retrieval=RETRIEVAL,
        session_id=SESSION,
        clock=FakeClock(),
    )


def test_select_lists_the_ten_retrievable_fields_in_order() -> None:
    assert select_fields(FIELDS) == [
        "id",
        "documentId",
        "documentVersionId",
        "chunkSetId",
        "chunkOrdinal",
        "title",
        "headingPath",
        "pageNumber",
        "contentType",
        "chunkText",
    ]
    assert FIELDS.vector not in select_fields(FIELDS)


@pytest.fixture
def documents() -> list[tuple[UUID, UUID]]:
    return [(uuid4(), uuid4()) for _ in range(3)]


async def test_t232_the_first_pass_request_on_the_wire(documents: list[tuple[UUID, UUID]]) -> None:
    transport = transport_for([wire_result(*pair) for pair in documents])
    served = Served(
        {document: served_row(document, chunk_set) for document, chunk_set in documents}
    )

    result = await retriever(transport, served).run("expense claims", [[0.1, 0.2, 0.3]], "first")

    (sent,) = transport.requests
    body = sent.json()
    assert body["search"] == "expense claims"
    assert body["queryType"] == "semantic"
    assert body["semanticConfiguration"] == "chunk-semantic-v1"
    assert body["semanticErrorHandling"] == "partial"
    assert body["semanticMaxWaitInMilliseconds"] == 2000
    assert body["top"] == 50
    assert body["sessionId"] == str(SESSION)
    assert body["select"] == ",".join(select_fields(FIELDS))
    assert body["vectorQueries"] == [
        {
            "kind": "vector",
            "vector": [0.1, 0.2, 0.3],
            "k": 50,
            "fields": "chunkVector",
            "exhaustive": False,
        }
    ]
    for never in (
        "filter",
        "searchFields",
        "captions",
        "answers",
        "count",
        "facets",
        "debug",
        "queryLanguage",
        "speller",
    ):
        assert never not in body
    assert [candidate.rank for candidate in result.kept] == [1, 2, 3]
    assert served.calls == [(INDEX.physical_index.id, [document for document, _ in documents])]
    assert not result.partial


async def test_t232_the_broadened_request_carries_two_weighted_queries(
    documents: list[tuple[UUID, UUID]],
) -> None:
    transport = transport_for([wire_result(*documents[0])])

    await retriever(transport, Served({})).run("q", [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], "broadened")

    queries = transport.requests[0].json()["vectorQueries"]
    assert [query["vector"] for query in queries] == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert all(query["fields"] == "chunkVector" and query["weight"] == 1.0 for query in queries)


async def test_t233_unserved_malformed_and_deleted_results_are_dropped_before_the_cut() -> None:
    served_pairs = [(uuid4(), uuid4()) for _ in range(12)]
    results: list[dict[str, Any]] = []
    rows: dict[UUID, ServedChunkSet] = {}
    for index, (document, chunk_set) in enumerate(served_pairs):
        rows[document] = served_row(document, chunk_set)
        results.append(wire_result(uuid4(), uuid4()))  # an unserved or deleted document
        results.append(wire_result(document, uuid4()))  # a superseded chunk set of a served one
        results.append(wire_result(document, chunk_set, page=None if index == 0 else 1))
        if len(results) >= 48:
            break
    transport = transport_for(results)

    result = await retriever(transport, Served(rows)).run("q", [[0.0, 0.0, 1.0]], "first")

    assert len(result.hits) == len(results)
    assert sum(result.served) == 11
    assert len(result.kept) == 8
    assert [candidate.hit.document_id for candidate in result.kept] == [
        document for document, _ in served_pairs[1:9]
    ]
    assert [record.served for record in result.records()][:3] == [False, False, False]


async def test_a_response_without_reranker_scores_is_partial(
    documents: list[tuple[UUID, UUID]],
) -> None:
    transport = transport_for([wire_result(*pair, reranker=None) for pair in documents])
    served = Served(
        {document: served_row(document, chunk_set) for document, chunk_set in documents}
    )

    result = await retriever(transport, served).run("q", [[1.0, 0.0, 0.0]], "first")

    assert result.partial
    empty = await retriever(transport_for([]), Served({})).run("q", [[1.0, 0.0, 0.0]], "first")
    assert empty.partial
    assert empty.kept == ()


async def test_no_results_run_no_statement() -> None:
    served = Served({})

    result = await retriever(transport_for([]), served).run("q", [[1.0, 0.0, 0.0]], "first")

    assert served.calls == []
    assert result.filter_ms == 0


async def test_a_search_failure_and_a_statement_failure_are_their_own_errors(
    documents: list[tuple[UUID, UUID]],
) -> None:
    failing = ReplayTransport([])
    with pytest.raises(SearchFailedError):
        await retriever(failing, Served({})).run("q", [[1.0, 0.0, 0.0]], "first")
    with pytest.raises(FilterFailedError):
        await retriever(
            transport_for([wire_result(*documents[0])]), Served({}, RuntimeError("sql"))
        ).run("q", [[1.0, 0.0, 0.0]], "first")


async def test_the_confidence_rule(documents: list[tuple[UUID, UUID]]) -> None:
    scores = [1.9, None, 2.0]
    transport = transport_for(
        [wire_result(*pair, reranker=score) for pair, score in zip(documents, scores, strict=True)]
    )
    served = Served(
        {document: served_row(document, chunk_set) for document, chunk_set in documents}
    )
    result = await retriever(transport, served).run("q", [[1.0, 0.0, 0.0]], "first")

    assert confident(result.kept, RETRIEVAL)
    assert not confident(result.kept[:2], RETRIEVAL)
    two = RETRIEVAL.model_copy(update={"min_confident_results": 2})
    assert not confident(result.kept, two)
