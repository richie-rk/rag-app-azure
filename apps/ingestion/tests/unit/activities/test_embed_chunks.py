"""Unit tests of `embed_chunks` (T-187; `14-ingestion.md` sections 6.6, 13.1 and 13.2)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import openai
import pytest

from docchat_core.ingestion.names import chunks_path, embedding_batch_path
from docchat_core.ingestion.settings import TokenEncoding
from docchat_ingestion.activities import embed_chunks
from docchat_ingestion.activities.embed_chunks import form_batches
from docchat_ingestion.workfiles import ChunkLine, ChunksHeader, EmbeddingBatch, encode_chunks


def chunks(count: int, tokens: int = 100) -> list[ChunkLine]:
    return [
        ChunkLine(
            ordinal=index,
            chunk_text=f"passage {index}",
            heading_path=["Heading"],
            page_number=1,
            token_count=tokens,
        )
        for index in range(count)
    ]


def store_chunks(world: Any, plan: Any, lines: list[ChunkLine]) -> None:
    header = ChunksHeader(
        chunk_set_id=plan.chunk_set_id,
        document_id=plan.document_id,
        document_version_id=plan.version_id,
        chunk_count=len(lines),
        encoding=TokenEncoding.CL100K_BASE,
        title=plan.title,
    )
    world.work.blobs[chunks_path(plan.work_prefix, plan.chunk_set_id)] = encode_chunks(
        header, lines
    )


def test_t187_batches_respect_both_caps_in_ordinal_order() -> None:
    lines = chunks(5, tokens=30_000)
    batches = form_batches(lines, max_inputs=64, max_tokens=60_000)
    assert [[chunk.ordinal for chunk in batch.chunks] for batch in batches] == [[0, 1], [2, 3], [4]]
    by_inputs = form_batches(chunks(130), max_inputs=64, max_tokens=300_000)
    assert [len(batch.chunks) for batch in by_inputs] == [64, 64, 2]
    assert [batch.number for batch in by_inputs] == [0, 1, 2]
    assert form_batches([], 64, 60_000) == []


async def test_t187_input_is_the_chunk_text_and_dimensions_are_sent(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_chunks(world, plan, chunks(3))
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await embed_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    assert output["done"] is True
    assert output["embeddingTokens"] == 30
    calls = world.openai.embeddings.calls
    assert sorted(len(call["input"]) for call in calls) == [1, 2]
    assert all(call["dimensions"] == 4 for call in calls)
    assert all(call["encoding_format"] == "float" for call in calls)
    assert all(call["model"] == "embed-ingest-te3-large-v1" for call in calls)
    assert {text for call in calls for text in call["input"]} == {
        "passage 0",
        "passage 1",
        "passage 2",
    }
    first = json.loads(world.work.blobs[embedding_batch_path(plan.work_prefix, ids.chunk_set, 0)])
    assert first["formatVersion"] == 1
    assert first["firstOrdinal"] == 0
    # Vectors are matched to their inputs by index, whatever order the service returns them in.
    assert first["vectors"] == [[0.0] * 4, [1.0] * 4]


async def test_t187_existing_batch_files_are_reused(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_chunks(world, plan, chunks(3))
    stored = EmbeddingBatch(first_ordinal=0, vectors=[[0.5] * 4, [0.5] * 4], prompt_tokens=20)
    world.work.blobs[embedding_batch_path(plan.work_prefix, ids.chunk_set, 0)] = stored.encode()
    wrong = EmbeddingBatch(first_ordinal=2, vectors=[[0.5] * 3], prompt_tokens=10)
    world.work.blobs[embedding_batch_path(plan.work_prefix, ids.chunk_set, 1)] = wrong.encode()
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await embed_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["done"] is True
    (call,) = world.openai.embeddings.calls
    assert call["input"] == ["passage 2"]
    assert output["embeddingTokens"] == 10


async def test_the_budget_stops_new_batches(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_chunks(world, plan, chunks(6))
    world.sql.script(kit.fence_ok(), kit.build_facts())
    world.clock.step = 400.0
    output = await embed_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True
    assert output["done"] is False
    assert len(world.openai.embeddings.calls) < 3


async def test_a_vector_of_another_length_is_embedding_failed(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_chunks(world, plan, chunks(1))
    world.sql.script(kit.fence_ok(), kit.build_facts())
    world.openai.embeddings.wrong_length = True
    output = await embed_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "embedding_failed"
    assert output["retryable"] is False


def _status_error(status: int, headers: dict[str, str] | None = None) -> openai.APIStatusError:
    # A response double: the error reads only its status, headers and request.
    response: Any = SimpleNamespace(status_code=status, headers=headers or {}, request=None)
    classes = {
        400: openai.BadRequestError,
        429: openai.RateLimitError,
        503: openai.InternalServerError,
    }
    return classes[status]("refused", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "code", "retry_after"),
    [
        (lambda: _status_error(400), "embedding_failed", None),
        (lambda: _status_error(429, {"retry-after-ms": "1500"}), "embedding_throttled", 2),
        (lambda: _status_error(503, {"retry-after": "7"}), "embedding_unavailable", 7),
        (
            lambda: openai.APITimeoutError(request=SimpleNamespace()),  # type: ignore[arg-type]
            "embedding_unavailable",
            None,
        ),
    ],
)
async def test_service_failures(
    kit: SimpleNamespace, world: Any, error: Any, code: str, retry_after: int | None
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_chunks(world, plan, chunks(1))
    world.sql.script(kit.fence_ok(), kit.build_facts())
    world.openai.embeddings.errors.append(error())
    output = await embed_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == code
    assert output["retryAfterSeconds"] == retry_after


@pytest.mark.parametrize("stored", [None, b"not a chunks file", b"\xff"])
async def test_a_missing_or_damaged_chunks_file_is_internal_error(
    kit: SimpleNamespace, world: Any, stored: bytes | None
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    if stored is not None:
        world.work.blobs[chunks_path(plan.work_prefix, plan.chunk_set_id)] = stored
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await embed_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("internal_error", True)
    assert world.openai.embeddings.calls == []


async def test_a_chunks_file_of_another_set_is_refused(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    other = kit.make_plan(kit.Ids())
    store_chunks(world, other.model_copy(update={"work_prefix": plan.work_prefix}), chunks(1))
    world.work.blobs[chunks_path(plan.work_prefix, plan.chunk_set_id)] = world.work.blobs.pop(
        chunks_path(plan.work_prefix, other.chunk_set_id)
    )
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await embed_chunks.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "internal_error"
