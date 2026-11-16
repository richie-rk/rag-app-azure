"""T-573: the stub conformance test, driven by the pinned `openai` `AsyncOpenAI` client."""

from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING, Any

import openai
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    import httpx
    from openai.types.chat import ChatCompletionMessageParam

    type StartStub = Callable[..., AbstractAsyncContextManager[str]]
    type ClientFor = Callable[[str], httpx.AsyncClient]

MARKER = "0123456789abcdef"
SAFETY_IDENTIFIER = "0123456789abcdef0123456789abcdef"
STANDALONE_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


class StandaloneQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


def _client(base_url: str, *, max_retries: int = 0) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=f"{base_url}/openai/v1/", api_key="stub-test-token", max_retries=max_retries
    )


def _messages(question: str) -> list[ChatCompletionMessageParam]:
    sources = (
        "The sources below were retrieved for the question in the next message.\n\n"
        f"[[BEGIN SOURCE S1 {MARKER}]]\nTitle: t\n\nx\n[[END SOURCE S1 {MARKER}]]"
    )
    return [
        {"role": "system", "content": f"Block marker for this request: {MARKER}"},
        {"role": "user", "content": sources},
        {"role": "user", "content": question},
    ]


@pytest.fixture
async def openai_client(start_stub: StartStub) -> AsyncIterator[AsyncOpenAI]:
    async with start_stub(answer_tokens=24, tokens_per_chunk=3) as base_url:
        client = _client(base_url)
        try:
            yield client
        finally:
            await client.close()


async def test_t573_streamed_answer_with_usage(openai_client: AsyncOpenAI) -> None:
    stream = await openai_client.chat.completions.create(
        model="chat",
        messages=_messages("What does the source say?"),
        stream=True,
        stream_options={"include_usage": True},
        max_completion_tokens=12_000,
        reasoning_effort="low",
        safety_identifier=SAFETY_IDENTIFIER,
        extra_body={"prompt_cache_options": {"mode": "explicit"}},
    )
    text = ""
    finish_reasons: list[str] = []
    usage = None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
            if chunk.choices[0].finish_reason is not None:
                finish_reasons.append(chunk.choices[0].finish_reason)
        if chunk.usage is not None:
            usage = chunk.usage
    assert finish_reasons == ["stop"]
    assert "[S1]" in text
    assert len(text.replace("[S1]", "").split()) == 24
    assert usage is not None
    assert usage.completion_tokens == 24
    assert usage.prompt_tokens > 0
    assert usage.total_tokens == usage.prompt_tokens + 24
    assert usage.completion_tokens_details is not None
    assert usage.completion_tokens_details.reasoning_tokens == 0
    assert usage.prompt_tokens_details is not None
    assert usage.prompt_tokens_details.cached_tokens == 0


async def test_t573_structured_output(openai_client: AsyncOpenAI) -> None:
    completion = await openai_client.chat.completions.create(
        model="chat-small",
        messages=_messages("What does it mean?"),
        max_completion_tokens=800,
        reasoning_effort="none",
        safety_identifier=SAFETY_IDENTIFIER,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "standalone_query",
                "strict": True,
                "schema": STANDALONE_QUERY_SCHEMA,
            },
        },
    )
    choice = completion.choices[0]
    assert choice.finish_reason == "stop"
    assert choice.message.refusal is None
    assert choice.message.content is not None
    parsed = StandaloneQuery.model_validate(json.loads(choice.message.content))
    assert parsed == StandaloneQuery(query="stub-query")
    assert completion.usage is not None
    assert completion.usage.completion_tokens > 0


async def test_t573_structured_output_through_parse(openai_client: AsyncOpenAI) -> None:
    completion = await openai_client.chat.completions.parse(
        model="chat-small",
        messages=_messages("What does it mean?"),
        max_completion_tokens=800,
        reasoning_effort="none",
        response_format=StandaloneQuery,
    )
    assert completion.choices[0].message.parsed == StandaloneQuery(query="stub-query")


@pytest.mark.parametrize("as_floats", [True, False])
async def test_t573_embeddings_with_dimensions(
    openai_client: AsyncOpenAI, *, as_floats: bool
) -> None:
    texts = ["the same text", "another text", "the same text"]
    # Without a named format the client asks for base64 and decodes it itself.
    extra: dict[str, Any] = {"encoding_format": "float"} if as_floats else {}
    response = await openai_client.embeddings.create(
        model="embed-query-te3-large-v1", input=texts, dimensions=1024, **extra
    )
    vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
    assert [len(vector) for vector in vectors] == [1024, 1024, 1024]
    for vector in vectors:
        assert math.isclose(math.fsum(value * value for value in vector), 1.0, rel_tol=1e-5)
    assert vectors[0] == vectors[2]
    assert vectors[0] != vectors[1]
    assert response.usage.prompt_tokens == math.ceil(sum(len(text) for text in texts) / 4)


async def test_t573_the_client_honours_retry_after_ms(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub(
        throttle_every_n=2, throttle_deployments=("ingest-summary",), retry_after_ms=400
    ) as base_url:
        client = _client(base_url, max_retries=3)
        try:
            waited = 0.0
            for _ in range(2):
                started = time.monotonic()
                completion = await client.chat.completions.create(
                    model="ingest-summary",
                    messages=[{"role": "user", "content": "a table"}],
                    max_completion_tokens=2000,
                    reasoning_effort="low",
                )
                waited = time.monotonic() - started
                assert completion.choices[0].finish_reason == "stop"
        finally:
            await client.close()
        async with client_for(base_url) as stub:
            stats = (await stub.get("/_stub/stats")).json()
    # The second call met the injected 429 and waited the stated 400 ms before its retry.
    assert waited >= 0.4
    assert stats["ingest-summary"] == {
        "started": 3,
        "completed": 2,
        "throttled": 1,
        "clientDisconnected": 0,
    }


async def test_t573_a_throttled_answer_states_its_delay(start_stub: StartStub) -> None:
    async with start_stub(retry_after_ms=2500) as base_url:
        client = _client(base_url)
        try:
            with pytest.raises(openai.RateLimitError) as caught:
                await client.chat.completions.create(
                    model="chat",
                    messages=[{"role": "user", "content": "Q [[stub:status=429]]"}],
                    stream=True,
                    max_completion_tokens=100,
                    reasoning_effort="low",
                )
        finally:
            await client.close()
    assert caught.value.response.headers["retry-after-ms"] == "2500"
    assert caught.value.response.headers["retry-after"] == "3"
