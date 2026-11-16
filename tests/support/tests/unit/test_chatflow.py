from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from docchat_core.cosmos.conversations import turn_trace_id
from docchat_testing.chatflow import (
    CHAT_TURNS_PATH,
    QUESTION,
    ChatFlow,
    StubControl,
    stream_events,
)
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fixtures.identities import make_principals
from docchat_testing.isolation import IsolationContext


class _Container:
    def __init__(self) -> None:
        self.reads: list[tuple[str, Any]] = []

    async def read_item(self, item: str, partition_key: Any) -> dict[str, Any]:
        self.reads.append((item, partition_key))
        return {"id": item}


def _flow(handler: httpx.MockTransport) -> tuple[ChatFlow, _Container]:
    world = IsolationContext(
        client=httpx.AsyncClient(transport=handler, base_url="http://api.test"),
        admin=cast("Engine", None),
        principals=make_principals(),
        document={},
        chat=ChatFakes(),
    )
    world.ids |= {"A": uuid4(), "DX": uuid4(), "CX": uuid4(), "VX": uuid4()}
    container = _Container()
    return ChatFlow(world=world, conversations=container), container


def _ndjson(*events: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


SESSION, TURN = str(uuid4()), str(uuid4())
META: dict[str, Any] = {
    "type": "meta",
    "sessionId": SESSION,
    "turnId": TURN,
    "sessionCreated": True,
    "replayed": False,
    "correlationId": "4bf92f3577b34da6a3ce929d0e0e4736",
}


async def test_ask_posts_a_chat_turn_request_with_the_callers_token() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=b"")

    flow, _ = _flow(httpx.MockTransport(answer))
    session = uuid4()

    await flow.ask("bob", session=session)

    (request,) = sent
    body = json.loads(request.content)
    assert request.url.path == CHAT_TURNS_PATH
    assert body["projectId"] == str(flow.world.ids["A"])
    assert body["sessionId"] == str(session)
    assert body["question"] == QUESTION
    UUID(body["clientTurnId"])
    assert request.headers["authorization"] == flow.headers("bob")["Authorization"]


def test_a_result_belongs_to_the_labelled_index() -> None:
    flow, _ = _flow(httpx.MockTransport(lambda request: httpx.Response(200)))

    result = flow.result("Some text.", ordinal=2)

    assert result["documentId"] == str(flow.world.ids["DX"])
    assert result["chunkSetId"] == str(flow.world.ids["CX"])
    assert result["chunkText"] == "Some text."
    assert result["chunkOrdinal"] == 2


async def test_turn_and_trace_are_read_in_the_callers_partition() -> None:
    flow, container = _flow(httpx.MockTransport(lambda request: httpx.Response(200)))

    await flow.turn(META)
    await flow.turn(META, trace=True)
    await flow.session(SESSION, "bob")

    partition = [flow.oid(), META["sessionId"]]
    assert container.reads == [
        (META["turnId"], partition),
        (str(turn_trace_id(UUID(TURN))), partition),
        (META["sessionId"], [flow.oid("bob"), META["sessionId"]]),
    ]


async def test_stream_events_checks_the_order_and_refuses_a_problem() -> None:
    done = {
        "type": "done",
        "turnId": META["turnId"],
        "truncated": False,
        "usage": {
            "promptTokens": 1,
            "completionTokens": 1,
            "reasoningTokens": 0,
            "cachedTokens": 0,
            "totalTokens": 2,
            "estimated": False,
        },
    }
    citations = {"type": "citations", "citations": [], "allowedLinks": []}

    events = await stream_events(httpx.Response(200, content=_ndjson(META, citations, done)))

    assert [event["type"] for event in events] == ["meta", "citations", "done"]
    with pytest.raises(AssertionError, match="404"):
        await stream_events(httpx.Response(404, json={"code": "project_not_found"}))


async def test_the_stub_control_reads_only_requests_after_its_mark() -> None:
    recorded: list[dict[str, Any]] = [
        {"seq": 4, "path": "/openai/v1/chat/completions", "deployment": "chat", "body": {}},
        {
            "seq": 5,
            "path": "/openai/v1/chat/completions",
            "deployment": "chat-small",
            "body": {"response_format": {"json_schema": {"name": "followup_questions"}}},
        },
        {"seq": 6, "path": "/contentsafety/text:shieldPrompt", "deployment": None, "body": {}},
    ]
    calls: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path == "/_stub/requests":
            after = int(request.url.params["after"])
            return httpx.Response(
                200, json={"requests": [entry for entry in recorded if entry["seq"] > after]}
            )
        if request.url.path == "/_stub/stats":
            return httpx.Response(200, json={"chat": {"started": 1}})
        return httpx.Response(204)

    control = StubControl(
        httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://stub")
    )
    control.seen = 3

    assert [entry["seq"] for entry in await control.chat_requests()] == [4, 5]
    assert [entry["seq"] for entry in await control.chat_requests("chat")] == [4]
    assert [entry["seq"] for entry in await control.json_requests("followup_questions")] == [5]
    assert [entry["seq"] for entry in await control.screening_requests()] == [6]
    await control.mark()
    assert control.seen == 6
    assert await control.stats() == {"chat": {"started": 1}}
    await control.script("shield_prompt", [{"status": 503}])
    await control.clear_scripts()
    await control.clear_scripts()
    assert [(call.method, call.url.path) for call in calls[-2:]] == [
        ("PUT", "/_stub/scripts/shield_prompt"),
        ("DELETE", "/_stub/scripts"),
    ]
