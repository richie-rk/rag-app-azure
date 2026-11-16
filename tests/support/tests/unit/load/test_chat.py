from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.load.chat import find_project, send_turn
from docchat_testing.load.common import LoadError

BASE = "https://app-docchat-api-load-abc123.azurewebsites.net"
PROJECT = "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"


async def bearer() -> str:
    return "token-1"


def client(transport: httpx.AsyncBaseTransport) -> DeployedApiClient:
    return DeployedApiClient(BASE, bearer, transport=transport)


async def test_the_project_is_found_by_name(chat_api: Any) -> None:
    async with client(chat_api.transport) as api:
        assert await find_project(api) == PROJECT


async def test_the_project_is_found_on_a_later_page() -> None:
    pages = [
        {"items": [{"projectId": "a", "name": "Other"}], "nextCursor": "c1"},
        {"items": [{"projectId": PROJECT, "name": "Load test"}], "nextCursor": None},
    ]
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("cursor"))
        return httpx.Response(200, json=pages[len(seen) - 1])

    async with client(httpx.MockTransport(handler)) as api:
        assert await find_project(api) == PROJECT
    assert seen == [None, "c1"]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, json={"items": [], "nextCursor": None}), "project_missing"),
        (httpx.Response(403, json={"code": "role_not_assigned"}), "project_unavailable"),
    ],
)
async def test_a_missing_or_unreadable_project_is_a_load_error(
    response: httpx.Response, code: str
) -> None:
    async with client(httpx.MockTransport(lambda request: response)) as api:
        with pytest.raises(LoadError) as raised:
            await find_project(api)
    assert raised.value.code == code


async def test_a_turn_that_ends_with_done(chat_api: Any) -> None:
    async with client(chat_api.transport) as api:
        result = await send_turn(api, project_id=PROJECT, question="Q?")
        again = await send_turn(
            api, project_id=PROJECT, question="Q2?", session_id=result.session_id
        )
    assert result.done
    assert result.status == 200
    assert result.reason is None
    assert again.session_id == result.session_id
    first, second = chat_api.turns
    assert first["sessionId"] is None
    assert second["sessionId"] == result.session_id
    assert first["projectId"] == PROJECT
    assert uuid.UUID(first["clientTurnId"]) != uuid.UUID(second["clientTurnId"])
    assert first["token"] == "token-1"


@pytest.mark.parametrize(
    ("outcome", "status", "reason"),
    [
        ("error", 200, "error_event"),
        ("invalid", 200, "invalid_stream"),
        ("status_503", 503, "status_503"),
    ],
)
async def test_a_turn_that_doesnt_end_with_done_says_why(
    chat_api: Any, outcome: str, status: int, reason: str
) -> None:
    chat_api.outcomes = [outcome]
    async with client(chat_api.transport) as api:
        result = await send_turn(api, project_id=PROJECT, question="Q?")
    assert not result.done
    assert (result.status, result.reason) == (status, reason)


async def test_a_transport_failure_is_a_failed_turn() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with client(httpx.MockTransport(handler)) as api:
        result = await send_turn(api, project_id=PROJECT, question="Q?")
    assert (result.status, result.done, result.reason) == (0, False, "transport_ConnectError")


async def test_the_turn_asks_for_ndjson() -> None:
    accepted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        accepted.append(request.headers.get("Accept", ""))
        return httpx.Response(429, json={"code": "too_many_streams"})

    async with client(httpx.MockTransport(handler)) as api:
        result = await send_turn(api, project_id=PROJECT, question="Q?")
    assert accepted == ["application/x-ndjson"]
    assert result.reason == "status_429"
