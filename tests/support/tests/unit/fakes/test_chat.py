"""The chat pipeline's doubles: the model client, the search gateway and the Prompt Shields
endpoint (`84-testing.md` sections 1.4 and 2.3)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from docchat_testing.fakes.chat import (
    DEFAULT_SUMMARY,
    PLAIN_TEXT,
    SHIELD_ATTACK,
    CharacterTokenizer,
    ChatFakes,
    FakeModelClient,
    FakePromptShields,
    FakeSearchGateway,
    FakeStream,
    chunk,
    completion,
    search_result,
    text_stream,
    usage_of,
)


def json_call(name: str) -> dict[str, Any]:
    return {"model": "chat-small", "response_format": {"json_schema": {"name": name}}}


async def test_a_streamed_call_answers_its_script_then_the_default() -> None:
    model = FakeModelClient()
    model.streams.append(text_stream(["a"], usage=usage_of(1, 1)))

    scripted = await model.chat.completions.create(model="chat", stream=True)
    default = await model.chat.completions.create(model="chat", stream=True)
    chunks = [item async for item in scripted]
    defaults = [item async for item in default]

    assert [item.choices[0].delta.content for item in chunks[:1]] == ["a"]
    assert chunks[1].choices[0].finish_reason == "stop"
    assert chunks[2].choices == []
    assert chunks[2].usage.prompt_tokens == 1
    assert "[S1]" in "".join(
        item.choices[0].delta.content or "" for item in defaults if item.choices
    )
    await default.close()
    assert model.opened[1].closed
    assert len(model.answer_requests()) == 2


async def test_a_scripted_failure_is_raised() -> None:
    model = FakeModelClient()
    model.streams.append(RuntimeError("down"))
    model.replies["standalone_query"].append(ValueError("bad"))
    model.embeddings_script.append(KeyError("x"))

    with pytest.raises(RuntimeError):
        await model.chat.completions.create(stream=True)
    with pytest.raises(ValueError, match="bad"):
        await model.chat.completions.create(**json_call("standalone_query"))
    with pytest.raises(KeyError):
        await model.embeddings.create(model="e", input="x", dimensions=256)


async def test_structured_calls_answer_their_queue_or_a_default() -> None:
    model = FakeModelClient()
    model.replies["search_passage"].append('{"passage": "p"}')
    model.replies["followup_questions"].append(completion(None, refusal="no"))

    scripted = await model.chat.completions.create(**json_call("search_passage"))
    refused = await model.chat.completions.create(**json_call("followup_questions"))
    default = await model.chat.completions.create(**json_call("standalone_query"))

    assert scripted.choices[0].message.content == '{"passage": "p"}'
    assert refused.choices[0].message.refusal == "no"
    assert json.loads(default.choices[0].message.content) == {"query": "standalone search query"}
    assert [len(model.json_requests(name)) for name in ("search_passage", "standalone_query")] == [
        1,
        1,
    ]


async def test_memory_operations_default_to_an_empty_list() -> None:
    model = FakeModelClient()

    default = await model.chat.completions.create(**json_call("memory_operations"))

    assert json.loads(default.choices[0].message.content) == {"operations": []}
    assert len(model.json_requests("memory_operations")) == 1


async def test_plain_text_calls_answer_their_queue_or_a_default_summary() -> None:
    model = FakeModelClient()
    model.replies[PLAIN_TEXT].append(completion("Scripted summary.", finish_reason="length"))

    scripted = await model.chat.completions.create(model="chat-small", messages=[])
    default = await model.chat.completions.create(model="chat-small", messages=[])
    structured = await model.chat.completions.create(**json_call("standalone_query"))

    assert scripted.choices[0].finish_reason == "length"
    assert default.choices[0].message.content == DEFAULT_SUMMARY
    assert default.usage.prompt_tokens == 50
    assert structured.choices[0].message.content is not None
    assert len(model.text_requests()) == 2
    assert len(model.json_requests("standalone_query")) == 1


async def test_embeddings_return_a_unit_vector_of_the_requested_length() -> None:
    model = FakeModelClient()

    response = await model.embeddings.create(model="e", input="x", dimensions=256)

    assert len(response.data[0].embedding) == 256
    assert sum(value * value for value in response.data[0].embedding) == 1.0
    assert model.embedding_requests()[0]["dimensions"] == 256


async def test_a_stream_yields_its_chunks_then_fails() -> None:
    stream = FakeStream([chunk("x", filter_error=True)], failure=RuntimeError("cut"))
    iterator = aiter(stream)

    first = await anext(iterator)
    with pytest.raises(RuntimeError, match="cut"):
        await anext(iterator)

    assert first.choices[0].content_filter_results == {"error": {"code": "unavailable"}}


async def test_the_search_gateway_records_and_answers_by_script() -> None:
    gateway = FakeSearchGateway()
    row = search_result(document_id=uuid4(), chunk_set_id=uuid4(), version_id=uuid4())
    gateway.results.append(row)
    gateway.script.extend([[], RuntimeError("down")])
    client = gateway("docchat-x-g1")

    scripted = [item async for item in await client.search(search_text="q", top=50)]
    with pytest.raises(RuntimeError):
        await client.search(search_text="q")
    default = [item async for item in await client.search(search_text="q")]

    assert scripted == []
    assert default == [row]
    assert gateway.requests[0] == {"index": "docchat-x-g1", "search_text": "q", "top": 50}
    assert row["@search.reranker_score"] == 3.0


async def test_uploaded_chunks_are_searched_filtered_and_deleted_by_key() -> None:
    gateway = FakeSearchGateway()
    client = gateway("docchat-x-g1")
    first, second = uuid4(), uuid4()
    rows = [
        search_result(document_id=first, chunk_set_id=uuid4(), version_id=uuid4(), ordinal=0),
        search_result(document_id=first, chunk_set_id=uuid4(), version_id=uuid4(), ordinal=1),
        search_result(document_id=second, chunk_set_id=uuid4(), version_id=uuid4()),
    ]
    uploaded = await client.upload_documents(rows)
    only_first = f"search.in(documentId, '{first}', ',')"

    found = [item async for item in await client.search(search_text="*", filter=only_first)]
    await client.delete_documents([{"id": rows[0]["id"]}, {"id": "absent"}])
    after = [item async for item in await client.search(search_text="*", filter=only_first)]
    by_eq = [
        item
        async for item in await client.search(search_text="*", filter=f"documentId eq '{second}'")
    ]

    assert all(result.succeeded for result in uploaded)
    assert [item["id"] for item in found] == [rows[0]["id"], rows[1]["id"]]
    assert [item["id"] for item in after] == [rows[1]["id"]]
    assert [item["id"] for item in by_eq] == [rows[2]["id"]]
    assert all(0 <= item["@search.reranker_score"] <= 4 for item in [*found, *after, *by_eq])
    with pytest.raises(ValueError, match="doesn't evaluate"):
        await client.search(search_text="*", filter="title eq 'x' and documentId eq 'y'")


async def test_prompt_shields_flags_the_attack_text_and_refuses_oversized_calls() -> None:
    shields = FakePromptShields()
    client = shields.client()

    flagged = await client.post(
        "/contentsafety/text:shieldPrompt",
        json={"userPrompt": "", "documents": ["clean", f"x {SHIELD_ATTACK}"]},
    )
    too_many = await client.post(
        "/contentsafety/text:shieldPrompt", json={"userPrompt": "", "documents": ["d"] * 6}
    )
    too_long = await client.post(
        "/contentsafety/text:shieldPrompt", json={"userPrompt": "", "documents": ["x" * 10_001]}
    )
    shields.queued.append(httpx.Response(503))
    queued = await client.post("/contentsafety/text:shieldPrompt", json={"documents": []})

    assert flagged.json()["documentsAnalysis"] == [
        {"attackDetected": False},
        {"attackDetected": True},
    ]
    assert (too_many.status_code, too_long.status_code, queued.status_code) == (400, 400, 503)
    assert len(shields.requests) == 4
    await client.aclose()


def test_the_character_tokenizer_round_trips_and_the_ports_carry_the_doubles() -> None:
    tokenizer = CharacterTokenizer()
    fakes = ChatFakes()
    ports = fakes.ports()

    assert tokenizer.decode(tokenizer.encode("h\u00e9llo")) == "h\u00e9llo"
    assert ports.search_clients is fakes.search
    assert ports.tokenizers is not None
    assert ports.openai is not None
