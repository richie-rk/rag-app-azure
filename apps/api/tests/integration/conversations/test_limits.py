"""The conversation routes with the real request limiters, against SQL Server and the Cosmos DB
emulator (T-306 and T-018; `13-conversations-and-memory.md` section 4.1 rules 1 to 3 and
acceptance criterion 20; `11-api.md` section 1.11 and acceptance criterion 12).

The app gets the real burst limiter and builds the real write limiter over the emulator's `usage`
container. Each test acts as a fresh user (a token whose `oid` is new), so its burst bucket and
its write windows are its own; the T-306 tests hold the limiters' clock still with a `FakeClock`,
and T-018 runs on the system clock, from second 0 of a UTC minute.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

import httpx
import pytest
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from sqlalchemy import Engine, insert

from docchat_api.app import AREA_ROUTERS, V1_PREFIX
from docchat_api.conversations import feedback_routes, memory_routes, session_routes
from docchat_api.http.routing import DocChatRoute, current_principal
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.usage import epoch_minute, write_rate_window_id
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.tables.access import project_members, projects, users
from docchat_testing.builders import project_member_row, project_row, user_row
from docchat_testing.builders.cosmos import (
    cosmos_now,
    fact,
    memory_document,
    memory_if_match,
    session_document,
    turn_document,
    write_rate_window,
)
from docchat_testing.clock import FakeClock
from docchat_testing.data_audit import CosmosAudit, CosmosFaults
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    BuiltApp,
    build_app,
    operation_map,
    ports_under_test,
    serving,
)
from docchat_testing.fixtures.cosmos import EmulatorContainers
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.tokens import SigningKeys

OPERATIONS: Final = (
    "listSessions",
    "getSession",
    "listSessionTurns",
    "putTurnFeedback",
    "getMemory",
    "updateMemorySettings",
    "updateMemoryFact",
    "deleteMemoryFact",
    "clearMemory",
)
WRITES: Final = frozenset(
    {
        "putTurnFeedback",
        "updateMemorySettings",
        "updateMemoryFact",
        "deleteMemoryFact",
        "clearMemory",
    }
)
BODY_ROUTES: Final = ("putTurnFeedback", "updateMemorySettings", "updateMemoryFact")
WRITE_LIMIT: Final = 120
"""`api.write_requests_per_minute` at its seeded default."""
OVERSIZED: Final = b"x" * 70_000
"""A 70,000-byte body that isn't valid JSON."""


@dataclass(frozen=True)
class World:
    """A fresh user's project, session, completed turn and memory document."""

    user: UUID
    headers: dict[str, str]
    project: UUID
    session: UUID
    turn: UUID
    fact: UUID
    etag: str

    def request(self, operation: str) -> tuple[str, str, dict[str, Any] | None]:
        """The method, path and JSON body of an accepted request of `operation`."""
        turn = f"/v1/sessions/{self.session}/turns/{self.turn}"
        return {
            "listSessions": ("GET", "/v1/sessions", None),
            "getSession": ("GET", f"/v1/sessions/{self.session}", None),
            "listSessionTurns": ("GET", f"/v1/sessions/{self.session}/turns", None),
            "putTurnFeedback": ("PUT", f"{turn}/feedback", {"rating": "up"}),
            "getMemory": ("GET", "/v1/memory", None),
            "updateMemorySettings": ("PATCH", "/v1/memory", {"paused": True}),
            "updateMemoryFact": (
                "PUT",
                f"/v1/memory/facts/{self.fact}",
                {"text": "Works on supplier contract renewals", "category": "topic"},
            ),
            "deleteMemoryFact": ("DELETE", f"/v1/memory/facts/{self.fact}", None),
            "clearMemory": ("DELETE", "/v1/memory/facts", None),
        }[operation]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(cosmos_now())


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
def user_id() -> UUID:
    return uuid4()


@pytest.fixture
def member(admin: Engine, user_id: UUID) -> UUID:
    """The fresh user's `app.users` row and their membership of a new project."""
    project = project_row()
    with admin.begin() as connection:
        connection.execute(insert(users), user_row(object_id=user_id))
        connection.execute(insert(projects), project)
        connection.execute(insert(project_members), project_member_row(user_id, project["id"]))
    return UUID(str(project["id"]))


async def lay_out(
    cosmos: DatabaseProxy, alice: SharedPrincipal, user: UUID, project: UUID
) -> World:
    conversations = cosmos.get_container_client(CONVERSATIONS.name)
    session = session_document(user_id=user, project_id=project)
    await conversations.upsert_item(session.to_item())
    turn = turn_document(user_id=user, session_id=session.session_id, project_id=project)
    await conversations.upsert_item(turn.to_item())
    remembered = fact()
    memory = memory_document(user_id=user, facts=[remembered])
    written = await cosmos.get_container_client(MEMORIES.name).upsert_item(memory.to_item())
    return World(
        user=user,
        headers=alice.authorization(oid=str(user)),
        project=project,
        session=session.session_id,
        turn=turn.id,
        fact=remembered.id,
        etag=memory_if_match(written),
    )


async def send(api: ApiUnderTest, world: World, operation: str, **extra: Any) -> httpx.Response:
    method, path, body = world.request(operation)
    headers = dict(world.headers)
    if operation in WRITES - {"putTurnFeedback"}:
        headers["If-Match"] = world.etag
    return await api.client.request(method, path, json=body, headers=headers, **extra)


def build(
    api_sql_engine: SqlEngine,
    signing_keys: SigningKeys,
    containers: EmulatorContainers,
    clock: FakeClock | None,
) -> BuiltApp:
    ports = replace(
        ports_under_test(
            sql_engine=api_sql_engine,
            clock=clock,
            real_limiters=True,
            chat=ChatFakes().ports(),
        ),
        container_wrapper=containers,
    )
    return build_app(ports=ports, signing_keys=signing_keys)


@pytest.fixture
def audit() -> CosmosAudit:
    return CosmosAudit(operations=operation_map())


@pytest.fixture
def emulator(audit: CosmosAudit) -> EmulatorContainers:
    return EmulatorContainers(audit=audit)


@pytest.fixture
def built(
    api_sql_engine: SqlEngine,
    signing_keys: SigningKeys,
    emulator: EmulatorContainers,
    clock: FakeClock,
) -> BuiltApp:
    return build(api_sql_engine, signing_keys, emulator, clock)


@pytest.fixture
async def api(
    built: BuiltApp,
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    emulator: EmulatorContainers,
) -> AsyncIterator[ApiUnderTest]:
    emulator.database = cosmos_database
    try:
        async with serving(built) as started:
            started.operations = operation_map()
            yield started
    finally:
        await api_sql_engine.dispose()


# --- T-306 ---------------------------------------------------------------------------------------


def test_t306_every_route_is_a_docchat_route_of_v1_taking_the_caller_from_the_token() -> None:
    routers = (session_routes.router, feedback_routes.router, memory_routes.router)
    assert all(router in AREA_ROUTERS for router in routers)
    found = {
        str(route.operation_id): route
        for router in routers
        for route in router.routes
        if isinstance(route, DocChatRoute)
    }
    assert set(found) == set(OPERATIONS)
    for route in found.values():
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert current_principal in calls
        assert route.route_options.burst_limited is True
        assert route.route_options.write_limited is (route.operation_id in WRITES)
    assert {
        (method, path)
        for (method, path), operation in operation_map().items()
        if operation in OPERATIONS
    } <= {
        (method, V1_PREFIX + route.path)
        for route in found.values()
        for method in route.methods or ()
    }


@pytest.mark.parametrize("operation", BODY_ROUTES)
async def test_t306_a_body_route_without_a_token_is_401_whatever_the_body(
    *,
    api: ApiUnderTest,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    member: UUID,
    user_id: UUID,
    operation: str,
) -> None:
    world = await lay_out(cosmos_database, alice, user_id, member)
    method, path, _ = world.request(operation)

    responses = [
        await api.client.request(
            method, path, content=content, headers={"Content-Type": "application/json"}
        )
        for content in (b"{not json", OVERSIZED)
    ]

    assert [response.status_code for response in responses] == [401, 401]
    assert all(response.json()["code"] == "authentication_required" for response in responses)


async def test_t306_an_empty_burst_bucket_refuses_every_operation_before_cosmos(
    *,
    api: ApiUnderTest,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    member: UUID,
    user_id: UUID,
    audit: CosmosAudit,
) -> None:
    world = await lay_out(cosmos_database, alice, user_id, member)
    limiter = api.ports.burst_limiter
    assert limiter is not None
    for _ in range(40):
        limiter.check(user_id)
    calls = len(audit.calls)

    responses = [await send(api, world, operation) for operation in OPERATIONS]

    for response in responses:
        assert response.status_code == 429, response.status_code
        assert response.json()["code"] == "too_many_requests"
        assert response.json()["retryAfterSeconds"] == 1
        assert response.headers["retry-after"] == "1"
    assert len(audit.calls) == calls


async def test_t306_a_full_write_window_refuses_the_five_writes_and_not_the_reads(
    *,
    api: ApiUnderTest,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    member: UUID,
    user_id: UUID,
    clock: FakeClock,
) -> None:
    world = await lay_out(cosmos_database, alice, user_id, member)
    usage = cosmos_database.get_container_client(USAGE.name)
    minute = epoch_minute(clock.now())
    for window in (minute - 1, minute):
        document = write_rate_window(
            user_id=user_id, id=write_rate_window_id(window), count=WRITE_LIMIT
        )
        await usage.upsert_item(document.to_item())

    responses = {operation: await send(api, world, operation) for operation in OPERATIONS}

    for operation, response in responses.items():
        if operation in WRITES:
            assert response.status_code == 429, (operation, response.status_code)
            assert response.json()["code"] == "write_rate_limited"
            assert 1 <= response.json()["retryAfterSeconds"] <= 120
        else:
            assert response.status_code == 200, (operation, response.status_code)


@pytest.fixture
def faulty_usage() -> EmulatorContainers:
    return EmulatorContainers(audit=CosmosFaults(USAGE.name))


@pytest.fixture
def faulty_built(
    api_sql_engine: SqlEngine,
    signing_keys: SigningKeys,
    faulty_usage: EmulatorContainers,
    clock: FakeClock,
) -> BuiltApp:
    return build(api_sql_engine, signing_keys, faulty_usage, clock)


async def test_t306_a_failing_usage_container_fails_the_writes_closed(
    *,
    faulty_built: BuiltApp,
    faulty_usage: EmulatorContainers,
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    member: UUID,
    user_id: UUID,
) -> None:
    world = await lay_out(cosmos_database, alice, user_id, member)
    faulty_usage.database = cosmos_database
    memories = cosmos_database.get_container_client(MEMORIES.name)
    before = await memories.read_item(str(user_id), str(user_id))

    try:
        async with serving(faulty_built) as api:
            responses = {operation: await send(api, world, operation) for operation in OPERATIONS}
    finally:
        await api_sql_engine.dispose()

    for operation, response in responses.items():
        if operation in WRITES:
            assert response.status_code == 503, (operation, response.status_code)
            assert response.json()["code"] == "dependency_unavailable"
        else:
            assert response.status_code == 200, (operation, response.status_code)
    after = await memories.read_item(str(user_id), str(user_id))
    assert after["_etag"] == before["_etag"]
    turn = await cosmos_database.get_container_client(CONVERSATIONS.name).read_item(
        str(world.turn), [str(user_id), str(world.session)]
    )
    assert turn["feedback"] is None


# --- T-018 ---------------------------------------------------------------------------------------


@pytest.fixture
def system_clock_built(
    api_sql_engine: SqlEngine, signing_keys: SigningKeys, emulator: EmulatorContainers
) -> BuiltApp:
    return build(api_sql_engine, signing_keys, emulator, None)


async def test_t018_130_feedback_writes_in_one_minute_allow_exactly_120(
    *,
    system_clock_built: BuiltApp,
    emulator: EmulatorContainers,
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    member: UUID,
    user_id: UUID,
) -> None:
    world = await lay_out(cosmos_database, alice, user_id, member)
    emulator.database = cosmos_database
    now = datetime.now(UTC)
    await asyncio.sleep(60 - now.second - now.microsecond / 1_000_000)
    minute = epoch_minute(datetime.now(UTC))

    responses: list[httpx.Response] = []
    loop = asyncio.get_running_loop()
    try:
        async with serving(system_clock_built) as api:
            started = loop.time()
            for index in range(130):
                # Each request starts 0.4 s after the previous one started, whatever it took.
                await asyncio.sleep(max(0.0, started + 0.4 * index - loop.time()))
                responses.append(await send(api, world, "putTurnFeedback"))
    finally:
        await api_sql_engine.dispose()

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == WRITE_LIMIT
    refused = [response for response in responses if response.status_code == 429]
    assert len(refused) == 10
    for response in refused:
        body = response.json()
        assert body["code"] == "write_rate_limited"
        assert 1 <= body["retryAfterSeconds"] <= 60
        assert response.headers["retry-after"] == str(body["retryAfterSeconds"])
    window = await cosmos_database.get_container_client(USAGE.name).read_item(
        write_rate_window_id(minute), str(user_id)
    )
    assert (window["count"], window["ttl"]) == (WRITE_LIMIT, 180)
    with contextlib.suppress(CosmosResourceNotFoundError):
        await cosmos_database.get_container_client(USAGE.name).delete_item(
            write_rate_window_id(minute), str(user_id)
        )
