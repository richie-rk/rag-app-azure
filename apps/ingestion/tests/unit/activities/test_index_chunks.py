"""Unit tests of `index_chunks` (T-188; `14-ingestion.md` sections 6.7, 8.13 and 13.3)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError

from docchat_core.ingestion.names import chunks_path, embedding_batch_path
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.search.schema import ChunkIndexFields
from docchat_ingestion.activities import index_chunks
from docchat_ingestion.activities.embed_chunks import form_batches
from docchat_ingestion.activities.index_chunks import upload_batches
from docchat_ingestion.workfiles import (
    ChunkLine,
    ChunksHeader,
    EmbeddingBatch,
    encode_chunks,
    json_size,
)

FIELDS = ChunkIndexFields.for_version(1)


def prepare(kit: SimpleNamespace, world: Any, plan: Any, count: int = 3) -> None:
    lines = [
        ChunkLine(
            ordinal=index,
            chunk_text=f"text {index}",
            heading_path=["A", "B"],
            page_number=index + 1,
            token_count=10,
        )
        for index in range(count)
    ]
    header = ChunksHeader(
        chunk_set_id=plan.chunk_set_id,
        document_id=plan.document_id,
        document_version_id=plan.version_id,
        chunk_count=count,
        encoding=TokenEncoding.CL100K_BASE,
        title=plan.title,
    )
    world.work.blobs[chunks_path(plan.work_prefix, plan.chunk_set_id)] = encode_chunks(
        header, lines
    )
    for batch in form_batches(lines, kit.SETTINGS.embedding_batch_max_inputs, 60_000):
        stored = EmbeddingBatch(
            first_ordinal=batch.first_ordinal,
            vectors=[[float(chunk.ordinal)] * 4 for chunk in batch.chunks],
            prompt_tokens=1,
        )
        path = embedding_batch_path(plan.work_prefix, plan.chunk_set_id, batch.number)
        world.work.blobs[path] = stored.encode()


def test_t188_batches_never_exceed_either_cap() -> None:
    documents = [{"id": str(index), "text": "x" * 1000} for index in range(250)]
    by_count = upload_batches(documents, 100, 8_000_000)
    assert [len(batch) for batch in by_count] == [100, 100, 50]
    size = json_size(documents[0])
    by_bytes = upload_batches(documents, 100, size * 3 + 20)
    assert all(len(batch) <= 3 for batch in by_bytes)
    assert sum(len(batch) for batch in by_bytes) == 250
    for batch in by_bytes:
        assert json_size({"value": batch}) <= size * 3 + 20


async def test_t188_each_document_comes_from_the_schema_record(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    prepare(kit, world, plan)
    world.sql.script(kit.fence_ok(), kit.build_facts(chunk_count=3), kit.Result(rowcount=1))
    output = await index_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    assert output["indexed"] == 3
    document = world.search.documents[f"{ids.document}_{ids.chunk_set}_1"]
    assert document == {
        FIELDS.key: f"{ids.document}_{ids.chunk_set}_1",
        FIELDS.document_id: str(ids.document),
        FIELDS.document_version_id: str(ids.version),
        FIELDS.chunk_set_id: str(ids.chunk_set),
        FIELDS.chunk_ordinal: 1,
        FIELDS.title: "Annual report",
        FIELDS.heading_path: ["A", "B"],
        FIELDS.chunk_text: "text 1",
        FIELDS.page_number: 2,
        FIELDS.content_type: "application/pdf",
        FIELDS.vector: [1.0] * 4,
    }
    (ready,) = world.sql.connection.statements("UPDATE app.chunk_sets SET state=N'ready'")
    assert "N'building', N'ready'" in ready.sql


async def test_refused_documents_are_retried(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    prepare(kit, world, plan)
    world.search.refuse[f"{ids.document}_{ids.chunk_set}_0"] = [503, 409]
    world.sql.script(kit.fence_ok(), kit.build_facts(chunk_count=3), kit.Result(rowcount=1))
    output = await index_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["indexed"] == 3
    assert [len(batch) for batch in world.search.uploads] == [3, 1, 1]


@pytest.mark.parametrize("statuses", [[400], [503, 503, 503, 503]])
async def test_documents_still_refused_fail_the_upload(
    kit: SimpleNamespace, world: Any, statuses: list[int]
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    prepare(kit, world, plan)
    world.search.refuse[f"{ids.document}_{ids.chunk_set}_2"] = statuses
    world.sql.script(kit.fence_ok(), kit.build_facts(chunk_count=3))
    output = await index_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "index_upload_failed"
    assert output["retryable"] is True


async def test_a_retired_set_is_chunk_set_retired(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    prepare(kit, world, plan)
    world.sql.script(kit.fence_ok(), kit.build_facts(chunk_count=3), kit.Result(rowcount=0))
    output = await index_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "chunk_set_retired"
    assert output["retryable"] is False


async def test_a_count_that_differs_never_marks_the_set_ready(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    prepare(kit, world, plan)
    world.sql.script(kit.fence_ok(), kit.build_facts(chunk_count=4))
    output = await index_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "index_upload_failed"


def _http_error(status: int) -> HttpResponseError:
    error = HttpResponseError("refused")
    error.status_code = status
    return error


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ResourceNotFoundError("no index"), "target_missing"),
        (_http_error(429), "search_unavailable"),
        (_http_error(500), "search_unavailable"),
        (ServiceRequestError("unreachable"), "search_unavailable"),
    ],
)
async def test_service_failures(
    kit: SimpleNamespace, world: Any, error: Exception, code: str
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    prepare(kit, world, plan)
    world.search.errors.append(error)
    world.sql.script(kit.fence_ok(), kit.build_facts(chunk_count=3))
    output = await index_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == code


@pytest.mark.parametrize("stored", [None, b"not a batch file", b'{"formatVersion": 1}'])
async def test_a_missing_or_damaged_batch_file_is_internal_error(
    kit: SimpleNamespace, world: Any, stored: bytes | None
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    prepare(kit, world, plan)
    path = embedding_batch_path(plan.work_prefix, plan.chunk_set_id, 1)
    del world.work.blobs[path]
    if stored is not None:
        world.work.blobs[path] = stored
    world.sql.script(kit.fence_ok(), kit.build_facts(chunk_count=3))
    output = await index_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("internal_error", True)
    assert world.search.uploads == []
