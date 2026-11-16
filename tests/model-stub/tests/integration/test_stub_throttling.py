"""T-576: the stub throttling and recorder tests, over a real connection to the stub."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    import httpx

    type StartStub = Callable[..., AbstractAsyncContextManager[str]]
    type ClientFor = Callable[[str], httpx.AsyncClient]

CHAT = "/openai/v1/chat/completions"
EMBEDDINGS = "/openai/v1/embeddings"


def _summary(deployment: str) -> dict[str, Any]:
    return {
        "model": deployment,
        "messages": [{"role": "user", "content": "a table"}],
        "max_completion_tokens": 2000,
        "reasoning_effort": "low",
    }


def _embedding(deployment: str, text: str = "chunk") -> dict[str, Any]:
    return {"model": deployment, "input": [text], "dimensions": 8, "encoding_format": "float"}


async def test_t576_every_nth_request_per_listed_deployment(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    settings = {
        "throttle_every_n": 3,
        "throttle_deployments": ("embed-ingest-te3-large-v1", "ingest-summary"),
        "retry_after_ms": 500,
    }
    async with start_stub(**settings) as base_url, client_for(base_url) as client:
        embeds = [
            await client.post(EMBEDDINGS, json=_embedding("embed-ingest-te3-large-v1"))
            for _ in range(6)
        ]
        summaries = [await client.post(CHAT, json=_summary("ingest-summary")) for _ in range(6)]
        chats = [await client.post(CHAT, json=_summary("chat")) for _ in range(6)]
        stats = (await client.get("/_stub/stats")).json()
    for responses in (embeds, summaries):
        assert [response.status_code for response in responses] == [200, 200, 429] * 2
        assert responses[2].headers["retry-after-ms"] == "500"
        assert responses[2].headers["retry-after"] == "1"
    assert [response.status_code for response in chats] == [200] * 6
    assert stats == {
        "chat": {"started": 6, "completed": 6, "throttled": 0, "clientDisconnected": 0},
        "embed-ingest-te3-large-v1": {
            "started": 6,
            "completed": 4,
            "throttled": 2,
            "clientDisconnected": 0,
        },
        "ingest-summary": {"started": 6, "completed": 4, "throttled": 2, "clientDisconnected": 0},
    }


async def test_t576_an_empty_list_throttles_every_deployment(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub(throttle_every_n=2) as base_url, client_for(base_url) as client:
        statuses = [
            (await client.post(CHAT, json=_summary(deployment))).status_code
            for deployment in ("chat", "chat-small", "chat", "chat-small")
        ]
    assert statuses == [200, 200, 429, 429]


async def test_t576_stats_count_disconnects(start_stub: StartStub, client_for: ClientFor) -> None:
    hang = {
        "model": "chat",
        "messages": [{"role": "user", "content": "Q [[stub:hang]]"}],
        "stream": True,
    }
    async with start_stub() as base_url, client_for(base_url) as client:
        async with client.stream("POST", CHAT, json=hang) as response:
            assert response.status_code == 200
        # The server notices the closed connection shortly after the client closes it.
        stats: dict[str, Any] = {}
        for _ in range(100):
            stats = (await client.get("/_stub/stats")).json()
            if stats["chat"]["clientDisconnected"]:
                break
            await asyncio.sleep(0.02)
    assert stats["chat"] == {"started": 1, "completed": 0, "throttled": 0, "clientDisconnected": 1}


async def test_t576_the_recorder_keeps_the_last_2000_requests(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        for start in range(0, 2005, 10):
            await asyncio.gather(
                *(
                    client.post(EMBEDDINGS, json=_embedding("embed-query", f"text {index}"))
                    for index in range(start, min(start + 10, 2005))
                )
            )
        kept = (await client.get("/_stub/requests")).json()["requests"]
        tail = (await client.get("/_stub/requests", params={"after": 2000})).json()["requests"]
        cleared = await client.delete("/_stub/requests")
        after_clear = (await client.get("/_stub/requests")).json()["requests"]
    assert len(kept) == 2000
    assert [entry["seq"] for entry in kept] == list(range(6, 2006))
    assert {entry["path"] for entry in kept} == {EMBEDDINGS}
    assert {entry["deployment"] for entry in kept} == {"embed-query"}
    assert all(entry["body"]["dimensions"] == 8 for entry in kept)
    assert "stub-test-token" not in json.dumps(kept)
    assert [entry["seq"] for entry in tail] == [2001, 2002, 2003, 2004, 2005]
    assert cleared.status_code == 204
    assert after_clear == []


async def test_t576_scripted_responses_come_in_order_before_the_schema_fill(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    followups: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": "followup_questions",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
                "required": ["questions"],
                "additionalProperties": False,
            },
        },
    }
    body = {**_summary("chat-small"), "reasoning_effort": "none", "response_format": followups}
    scripted = [{"questions": ["first?"]}, {"questions": ["second?", "third?"]}]
    async with start_stub() as base_url, client_for(base_url) as client:
        await client.put("/_stub/scripts/followup_questions", json={"responses": scripted})
        await client.put("/_stub/scripts/standalone_query", json={"responses": [{"query": "x"}]})
        contents = [
            json.loads(
                (await client.post(CHAT, json=body)).json()["choices"][0]["message"]["content"]
            )
            for _ in range(3)
        ]
    assert contents == [*scripted, {"questions": ["stub-questions"]}]
