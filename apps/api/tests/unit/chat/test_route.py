"""`createChatTurn` through the in-process app: route options, the checks before the stream, the
lease on a refusal, the response and the replay (T-212's chat half, T-219's refusal case, the unit
half of T-270; `12-chat-pipeline.md` sections 2, 3 and 14.1).

The app runs over a scripted data-access layer, in-memory Cosmos DB containers and the chat
doubles; the conversation store and the usage service are the real ones.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import pytest

from docchat_api.app import Ports, create_app
from docchat_api.chat.models import REQUEST_EXAMPLE
from docchat_api.chat.route import CHAT_BODY_LIMIT, router
from docchat_api.http.ratelimit import BurstLimiter
from docchat_api.http.routing import DocChatRoute
from docchat_core.cosmos.containers import CONTAINERS
from docchat_core.errors import DocChatProblem
from docchat_core.sql.engine import SqlEngine, SqlUnavailable
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fakes.limiters import PermissiveWriteRateLimiter
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    after_response_idle,
    ports_under_test,
    running_api,
)
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

if TYPE_CHECKING:
    from azure.cosmos.aio import ContainerProxy

PROJECT = UUID(REQUEST_EXAMPLE["projectId"])
INDEX = UUID("0c0c0c0c-0000-4000-8000-00000000000c")
PHYSICAL = UUID("0c0c0c0c-0000-4000-8000-0000000000c1")
PATH = "/v1/chat/turns"


class ChatSql:
    """The scripted data-access layer: the seeded rows and provisioning of the unit conftest,
    and the chat path's access rows for project `PROJECT` with its default index."""

    def __init__(self, base: Any) -> None:
        self.base = base
        self.member = True
        self.index = True
        self.unavailable = False

    async def fetch_all(self, statement: object, parameters: Any = None, **kwargs: Any) -> Any:
        if kwargs["name"] == "served_chunk_sets":
            return []
        return await self.base.fetch_all(statement, parameters, **kwargs)

    async def fetch_one(self, statement: object, parameters: Any = None, **kwargs: Any) -> Any:
        name = kwargs["name"]
        if self.unavailable:
            raise SqlUnavailable("exhausted", 40613, "")
        if name == "authorize_project":
            if parameters["project_id"] != PROJECT:
                return None
            return SimpleNamespace(
                id=PROJECT,
                name="A",
                description="",
                instructions="",
                example_questions_json="[]",
                status="active",
                is_member=1 if self.member else 0,
            )
        if name == "resolve_index":
            return SimpleNamespace(
                override_index_id=None,
                override_expires_at=None,
                default_index_id=INDEX if self.index else None,
            )
        if name == "read_index_record":
            return SimpleNamespace(
                id=INDEX,
                status="ready",
                physical_index_id=PHYSICAL,
                name="docchat-0c0c0c0c00004000800000000000000c-g1",
                schema_version=1,
                embedding_model="text-embedding-3-large",
                embedding_dimensions=1024,
                query_embedding_deployment="embed-query-te3-large-v1",
            )
        return await self.base.fetch_one(statement, parameters, **kwargs)

    async def run(self, work: Callable[[Any], Any], **kwargs: Any) -> Any:
        return await self.base.run(work, **kwargs)

    async def dispose(self) -> None:
        return None


@dataclass
class Chat:
    api: ApiUnderTest
    sql: ChatSql
    fakes: ChatFakes
    containers: dict[str, FakeContainer]

    def leases(self) -> list[str]:
        return [
            item_id
            for (_, item_id) in self.containers["usage"].items
            if item_id.startswith("lease:")
        ]


@pytest.fixture
def fakes() -> ChatFakes:
    return ChatFakes()


@pytest.fixture
async def chat(fake_sql: Any, fakes: ChatFakes, signing_keys: SigningKeys) -> AsyncIterator[Chat]:
    containers = {definition.name: FakeContainer.of(definition) for definition in CONTAINERS}

    def replace(container: ContainerProxy) -> ContainerProxy:
        return cast("ContainerProxy", containers[container.id])

    sql = ChatSql(fake_sql)
    ports = ports_under_test(
        sql_engine=cast("SqlEngine", sql), container_wrapper=replace, chat=fakes.ports()
    )
    async with running_api(ports=ports, signing_keys=signing_keys) as api:
        yield Chat(api, sql, fakes, containers)


def body(**changes: Any) -> dict[str, Any]:
    return {**REQUEST_EXAMPLE, "clientTurnId": str(uuid4()), **changes}


def lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines()]


# --- T-212 --------------------------------------------------------------------------------------


def test_t212_the_route_and_its_options() -> None:
    (route,) = [route for route in router.routes if isinstance(route, DocChatRoute)]

    assert route.path == "/chat/turns"
    assert route.operation_id == "createChatTurn"
    options = route.route_options
    assert (options.body_limit, options.burst_limited, options.write_limited) == (
        CHAT_BODY_LIMIT,
        False,
        False,
    )
    assert route.access_spec.scoped_params == ("body:/projectId", "body:/sessionId")
    document = create_app().openapi()
    operation = document["paths"]["/v1/chat/turns"]["post"]
    # FastAPI's document encoder leaves out null members, so the example omits `sessionId`,
    # which the request model reads as null.
    example = operation["requestBody"]["content"]["application/json"]["example"]
    assert example == {key: value for key, value in REQUEST_EXAMPLE.items() if value is not None}
    assert "too_many_requests" not in operation["x-docchat-error-codes"]
    assert "write_rate_limited" not in operation["x-docchat-error-codes"]


async def test_t212_a_body_above_32768_bytes_is_refused_before_it_is_parsed(
    chat: Chat, alice: SharedPrincipal
) -> None:
    response = await chat.api.client.post(
        PATH,
        content=b"{" + b" " * CHAT_BODY_LIMIT + b"}",
        headers=alice.authorization() | {"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"


async def test_t212_an_empty_burst_bucket_still_serves_create_chat_turn(
    fake_sql: Any, fakes: ChatFakes, signing_keys: SigningKeys, alice: SharedPrincipal
) -> None:
    containers = {definition.name: FakeContainer.of(definition) for definition in CONTAINERS}

    def replace(container: ContainerProxy) -> ContainerProxy:
        return cast("ContainerProxy", containers[container.id])

    limiter = BurstLimiter(FakeClock())
    ports = Ports(
        burst_limiter=limiter,
        write_limiter=PermissiveWriteRateLimiter(),
        sql_engine=cast("SqlEngine", ChatSql(fake_sql)),
        container_wrapper=replace,
        chat=fakes.ports(),
    )
    refused = 0
    for _ in range(100):
        try:
            limiter.check(alice.oid)
        except DocChatProblem:
            refused += 1
    async with running_api(ports=ports, signing_keys=signing_keys) as api:
        response = await api.client.post(PATH, json=body(), headers=alice.authorization())

    assert refused > 0
    assert response.status_code == 200


# --- The stream ---------------------------------------------------------------------------------


async def test_a_turn_streams_ndjson_with_the_stream_headers(
    chat: Chat, alice: SharedPrincipal, captured: Any
) -> None:
    response = await chat.api.client.post(
        PATH, json=body(), headers=alice.authorization() | {"Accept-Encoding": "gzip, br"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-length" not in response.headers
    assert "content-encoding" not in response.headers
    events = lines(response.text)
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "done"
    assert chat.leases() == []
    (span,) = captured.finished_spans("docchat.chat.prestream")
    assert span.attributes is not None
    assert span.attributes["docchat.outcome"] == "accepted"


async def test_a_duplicate_of_a_completed_turn_replays(chat: Chat, alice: SharedPrincipal) -> None:
    request = body()
    first = await chat.api.client.post(PATH, json=request, headers=alice.authorization())
    await after_response_idle(chat.api.app)
    calls = len(chat.fakes.model.requests)

    again = await chat.api.client.post(PATH, json=request, headers=alice.authorization())

    replayed = lines(again.text)
    assert replayed[0]["replayed"] is True
    assert replayed[-1] == {**lines(first.text)[-1], "turnId": replayed[-1]["turnId"]}
    assert len(chat.fakes.model.requests) == calls


# --- Refusals before the stream -------------------------------------------------------------


async def test_another_project_is_project_not_found_and_frees_the_lease(
    chat: Chat, alice: SharedPrincipal, captured: Any
) -> None:
    response = await chat.api.client.post(
        PATH, json=body(projectId=str(uuid4())), headers=alice.authorization()
    )

    assert response.status_code == 404
    assert response.json()["code"] == "project_not_found"
    assert chat.leases() == []
    assert chat.fakes.model.requests == []
    (span,) = captured.finished_spans("docchat.chat.prestream")
    assert span.attributes is not None
    assert span.attributes["docchat.outcome"] == "project_not_found"


async def test_a_project_without_an_index_is_409(chat: Chat, alice: SharedPrincipal) -> None:
    chat.sql.index = False

    response = await chat.api.client.post(PATH, json=body(), headers=alice.authorization())

    assert response.status_code == 409
    assert response.json()["code"] == "project_index_unavailable"
    assert chat.leases() == []


async def test_an_unknown_session_is_session_not_found(chat: Chat, alice: SharedPrincipal) -> None:
    response = await chat.api.client.post(
        PATH, json=body(sessionId=str(uuid4())), headers=alice.authorization()
    )

    assert response.status_code == 404
    assert response.json() == {**response.json(), "code": "session_not_found"}
    assert "sessionId" not in response.json()
    assert chat.leases() == []


async def test_sql_unavailable_is_503_and_frees_the_lease(
    chat: Chat, alice: SharedPrincipal
) -> None:
    chat.sql.unavailable = True

    response = await chat.api.client.post(PATH, json=body(), headers=alice.authorization())

    assert response.status_code == 503
    assert response.json()["code"] == "dependency_unavailable"
    assert chat.leases() == []


async def test_the_fourth_request_in_a_minute_is_rate_limited(
    chat: Chat, alice: SharedPrincipal
) -> None:
    responses = [
        await chat.api.client.post(
            PATH, json=body(projectId=str(uuid4())), headers=alice.authorization()
        )
        for _ in range(4)
    ]

    assert [response.status_code for response in responses] == [404, 404, 404, 429]
    response = responses[-1]
    assert response.json()["code"] == "rate_limited"
    assert response.headers["retry-after"] == str(response.json()["retryAfterSeconds"])


async def test_an_invalid_question_is_422_and_isnt_counted(
    chat: Chat, alice: SharedPrincipal
) -> None:
    response = await chat.api.client.post(
        PATH, json=body(question="\u200b"), headers=alice.authorization()
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body.question", "reason": "empty"}]
    assert not [key for key in chat.containers["usage"].items if key[1].startswith("rate:")]
