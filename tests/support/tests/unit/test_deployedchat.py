"""The deployed suites' chat-turn helper (`docchat_testing.deployedchat`)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.deployedchat import (
    CHAT_PATH,
    RATE_RETRY_MARGIN_SECONDS,
    ChatTurnError,
    send_chat_turn,
)

BASE = "https://app-docchat-api-dev.example"
PROJECT = "3f2b8c1e-7d4a-4e9b-9c55-0a6f1d2e8b74"
TURN = "9a1e2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e"
SESSION = "5f0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e"
META = {
    "type": "meta",
    "sessionId": SESSION,
    "turnId": TURN,
    "sessionCreated": True,
    "replayed": False,
    "correlationId": "0af7651916cd43dd8448eb211c80319c",
}
CITATION = {
    "number": 1,
    "title": "tables",
    "pageNumber": 2,
    "headingPath": ["2 Treatment plants"],
    "contentType": "application/pdf",
    "openAs": "pdf_page",
}
DONE = {
    "type": "done",
    "turnId": TURN,
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
CITATIONS = {"type": "citations", "citations": [], "allowedLinks": []}
ERROR = {
    "type": "error",
    "code": "question_filtered",
    "message": "The question was blocked.",
    "retryable": False,
    "retryAfterSeconds": None,
    "correlationId": META["correlationId"],
}


def lines(*events: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def problem(status: int, code: str, retry_after: int | None = None) -> httpx.Response:
    body: dict[str, Any] = {"type": f"urn:docchat:problem:{code}", "status": status, "code": code}
    if retry_after is not None:
        body["retryAfterSeconds"] = retry_after
    return httpx.Response(status, json=body, headers={"Content-Type": "application/problem+json"})


class Clock:
    """A clock that moves one second per reading, and records the waits asked for."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)


def client(responses: Iterator[httpx.Response], sent: list[httpx.Request]) -> DeployedApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return next(responses)

    async def token() -> str:
        return "token-1"

    return DeployedApiClient(BASE, token, transport=httpx.MockTransport(handler))


async def test_a_turn_is_read_to_its_end_with_the_first_line_time() -> None:
    sent: list[httpx.Request] = []
    delta = {"type": "delta", "text": "Four plants ⟦1⟧"}
    body = lines(
        META, delta, {"type": "citations", "citations": [CITATION], "allowedLinks": []}, DONE
    )
    clock = Clock()
    async with client(iter([httpx.Response(200, content=body)]), sent) as api:
        turn = await send_chat_turn(
            api, project_id=PROJECT, question="How many plants?", monotonic=clock.monotonic
        )
    assert turn.status_code == 200
    assert str(turn.meta.turn_id) == TURN
    assert str(turn.require_done().turn_id) == TURN
    assert [citation.title for citation in turn.citations] == ["tables"]
    assert turn.answer == "Four plants ⟦1⟧"
    assert turn.first_line_seconds == 1.0
    (request,) = sent
    assert request.url.path == CHAT_PATH
    sent_body = json.loads(request.content)
    assert sent_body["projectId"] == PROJECT
    assert sent_body["sessionId"] is None
    assert sent_body["question"] == "How many plants?"


async def test_an_error_event_is_reported_by_its_code() -> None:
    async with client(iter([httpx.Response(200, content=lines(META, ERROR))]), []) as api:
        turn = await send_chat_turn(api, project_id=PROJECT, question="q")
    assert turn.error is not None
    assert turn.error.code == "question_filtered"
    assert turn.citations == []
    with pytest.raises(ChatTurnError, match="error question_filtered"):
        turn.require_done()


async def test_a_refusal_comes_back_with_its_problem() -> None:
    async with client(iter([problem(404, "project_not_found")]), []) as api:
        turn = await send_chat_turn(api, project_id=PROJECT, question="q")
    assert turn.status_code == 404
    assert turn.problem_code == "project_not_found"
    assert turn.events == []
    assert turn.describe() == "404 project_not_found"
    with pytest.raises(ChatTurnError):
        _ = turn.meta


async def test_rate_limited_waits_and_sends_again_with_a_new_client_turn_id() -> None:
    sent: list[httpx.Request] = []
    clock = Clock()
    responses = iter(
        [
            problem(429, "rate_limited", 20),
            httpx.Response(200, content=lines(META, CITATIONS, DONE)),
        ]
    )
    async with client(responses, sent) as api:
        turn = await send_chat_turn(
            api, project_id=PROJECT, question="q", session_id=SESSION,
            sleep=clock.sleep,
        )  # fmt: skip
    assert turn.done is not None
    assert clock.waits == [20 + RATE_RETRY_MARGIN_SECONDS]
    assert turn.rate_waited_seconds == 20 + RATE_RETRY_MARGIN_SECONDS
    first, second = (json.loads(request.content) for request in sent)
    assert first["clientTurnId"] != second["clientTurnId"]
    assert first["sessionId"] == second["sessionId"] == SESSION


async def test_rate_limited_beyond_the_wait_limit_goes_back_to_the_test() -> None:
    clock = Clock()
    responses = iter([problem(429, "rate_limited", 100), problem(429, "rate_limited", 100)])
    async with client(responses, []) as api:
        turn = await send_chat_turn(
            api, project_id=PROJECT, question="q", rate_wait=150, sleep=clock.sleep
        )
    assert turn.problem_code == "rate_limited"
    assert clock.waits == [100 + RATE_RETRY_MARGIN_SECONDS]


async def test_other_429_codes_go_back_to_the_test_at_once() -> None:
    clock = Clock()
    async with client(iter([problem(429, "too_many_streams", 5)]), []) as api:
        turn = await send_chat_turn(api, project_id=PROJECT, question="q", sleep=clock.sleep)
    assert turn.problem_code == "too_many_streams"
    assert clock.waits == []
