"""T-574: the stub directive tests, over a real connection to the stub."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    type StartStub = Callable[..., AbstractAsyncContextManager[str]]
    type ClientFor = Callable[[str], httpx.AsyncClient]

CHAT = "/openai/v1/chat/completions"
STRUCTURED = {
    "type": "json_schema",
    "json_schema": {
        "name": "standalone_query",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def _chat(question: str, **fields: Any) -> dict[str, Any]:
    return {
        "model": "chat",
        "messages": [{"role": "user", "content": question}],
        "max_completion_tokens": 1000,
        "reasoning_effort": "low",
        **fields,
    }


def _streamed(question: str) -> dict[str, Any]:
    return _chat(question, stream=True, stream_options={"include_usage": True})


async def _stats(client: httpx.AsyncClient, deployment: str = "chat") -> dict[str, int]:
    stats: dict[str, int] = (await client.get("/_stub/stats")).json()[deployment]
    return stats


async def _wait_for_disconnect(client: httpx.AsyncClient, *, within: float) -> dict[str, int]:
    """Poll the stub's counters until a disconnect shows or `within` seconds pass."""
    deadline = asyncio.get_running_loop().time() + within
    while True:
        stats = await _stats(client)
        if stats["clientDisconnected"] or asyncio.get_running_loop().time() > deadline:
            return stats
        await asyncio.sleep(0.02)


async def test_t574_status_429_sets_retry_after_ms(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub(retry_after_ms=750) as base_url, client_for(base_url) as client:
        response = await client.post(CHAT, json=_streamed("Q [[stub:status=429]]"))
        failed = await client.post(CHAT, json=_streamed("Q [[stub:status=503]]"))
    assert response.status_code == 429
    assert response.headers["retry-after-ms"] == "750"
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"error": {"code": "429", "message": "stub directive"}}
    assert failed.status_code == 503
    assert failed.json() == {"error": {"code": "503", "message": "stub directive"}}


async def test_t574_cut_ends_a_stream_without_its_final_chunk(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        response = await client.post(CHAT, json=_streamed("Q [[stub:cut=3]]"))
        stats = await _stats(client)
    lines = [line for line in response.text.split("\n") if line]
    assert response.status_code == 200
    assert "data: [DONE]" not in lines
    chunks = [json.loads(line.removeprefix("data: ")) for line in lines]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    contents = [chunk for chunk in chunks[1:] if chunk["choices"][0]["delta"].get("content")]
    assert len(contents) == 3
    assert len(chunks) == 4
    assert all(chunk["choices"][0]["finish_reason"] is None for chunk in chunks)
    assert stats["completed"] == 0


async def test_t574_hang_holds_until_the_client_disconnects(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        async with client.stream("POST", CHAT, json=_streamed("Q [[stub:hang]]")) as response:
            assert response.status_code == 200
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.5):
                    await anext(response.aiter_bytes())
        stats = await _wait_for_disconnect(client, within=2.0)
    assert stats["clientDisconnected"] == 1
    assert stats["completed"] == 0


async def test_t574_a_disconnect_stops_a_slow_stream(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    question = "Q [[stub:delay_ms=200]] [[stub:first_ms=0]]"
    async with start_stub() as base_url, client_for(base_url) as client:
        async with client.stream("POST", CHAT, json=_streamed(question)) as response:
            events = 0
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events += 1
                if events == 3:
                    break
        stats = await _wait_for_disconnect(client, within=2.0)
    assert stats["clientDisconnected"] == 1
    assert stats["completed"] == 0


async def test_t574_filter_prompt_returns_content_filter(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        response = await client.post(CHAT, json=_streamed("Q [[stub:filter=prompt]]"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "content_filter"
    assert response.json()["error"]["innererror"] == {"code": "ResponsibleAIPolicyViolation"}


async def test_t574_filter_completion_ends_the_stream_filtered(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        response = await client.post(CHAT, json=_streamed("Q [[stub:filter=completion]]"))
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.split("\n")
        if line.startswith("data: {")
    ]
    finishes = [chunk["choices"][0]["finish_reason"] for chunk in chunks if chunk["choices"]]
    assert finishes[-1] == "content_filter"


async def test_t574_an_unknown_directive_returns_400(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        response = await client.post(CHAT, json=_chat("Q [[stub:explode]]"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_stub_directive"


async def test_t574_a_stream_qualified_directive_spares_a_structured_call(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    question = "Q [[stub:status=500@stream]]"
    async with start_stub() as base_url, client_for(base_url) as client:
        structured = await client.post(CHAT, json=_chat(question, response_format=STRUCTURED))
        streamed = await client.post(CHAT, json=_streamed(question))
    assert structured.status_code == 200
    content = structured.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"query": "stub-query"}
    assert streamed.status_code == 500
