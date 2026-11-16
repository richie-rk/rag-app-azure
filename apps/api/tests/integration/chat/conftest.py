"""Fixtures of the chat pipeline's integration tests (`12-chat-pipeline.md` acceptance criteria).

Every app here runs against the session's SQL Server database and Cosmos DB emulator with
`CosmosAudit` on its containers, so a chat-path call without the full partition key fails the
test at teardown. Two pipelines:

- over the in-memory doubles of `docchat_testing.fakes.chat` (`flow`, `live_flow`): the model,
  search and Prompt Shields doubles, scripted per test;
- over the model stub of `compose.yaml` (`stub_flow`): the API's own OpenAI and Prompt Shields
  clients reach the stub's model and screening endpoints, and search stays the search double.
  The tests that measure time (pauses, timeouts, heartbeats) or read what the stub received use
  it, served through Uvicorn so the stream arrives as it is written.

A module changes ops-config values for its apps by overriding `chat_ops`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from functools import partial
from typing import Any, Final

import httpx
import pytest
from azure.cosmos.aio import ContainerProxy, DatabaseProxy
from sqlalchemy import Engine

from docchat_api.chat.service import ChatPorts
from docchat_core.clients.contentsafety import contentsafety_client
from docchat_core.clients.openai import api_openai_client
from docchat_core.cosmos.containers import CONVERSATIONS
from docchat_core.sql.engine import SqlEngine
from docchat_testing.chatflow import ChatFlow, StubControl
from docchat_testing.data_audit import CosmosAudit
from docchat_testing.fakes.chat import ChatFakes, character_tokenizers
from docchat_testing.fixtures.api import (
    CLIENT_TIMEOUT,
    ApiUnderTest,
    BuiltApp,
    build_app,
    emulator_containers,
    operation_map,
    ports_under_test,
    raise_chat_limits,
    serve_live,
    serving,
    set_ops_config,
)
from docchat_testing.fixtures.identities import PrincipalName, SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.isolation import IsolationContext, seed_project, seed_served_index
from docchat_testing.plugin import ComposeService
from docchat_testing.tokens import SigningKeys

STUB_TOKEN: Final = "local-emulator"
"""The constant bearer a loopback model or screening endpoint receives (the stub checks only
that one is present)."""
STUB_CONTROL_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
STUB_READ_TIMEOUT: Final = httpx.Timeout(300.0, connect=5.0)
"""A client of the stub-backed app waits for the longest stream the tests run (200 s)."""


def chat_app(
    *,
    sql_engine: SqlEngine,
    sql_database: SqlTestDatabase,
    cosmos: DatabaseProxy,
    signing_keys: SigningKeys,
    chat: ChatPorts,
    ops: dict[str, Any],
    wrap: Any,
) -> BuiltApp:
    """An app over the session's databases with the chat path's limits raised and `ops` set,
    each Cosmos DB container wrapped by `wrap`."""
    raise_chat_limits(sql_database)
    set_ops_config(sql_database, ops)
    return build_app(
        ports=ports_under_test(
            sql_engine=sql_engine,
            container_wrapper=emulator_containers(cosmos, wrap=wrap),
            chat=chat,
        ),
        signing_keys=signing_keys,
    )


def seeded(
    client: httpx.AsyncClient,
    admin: Engine,
    principals: dict[PrincipalName, SharedPrincipal],
    cosmos: DatabaseProxy,
    fakes: ChatFakes,
    *members: PrincipalName,
) -> IsolationContext:
    """Project A with `members` and its default index X serving one document."""
    context = IsolationContext(
        client=client, admin=admin, principals=principals, document={}, chat=fakes, cosmos=cosmos
    )
    seed_project(context, "A", *members)
    seed_served_index(context, "X", "A")
    return context


# --- Over the in-memory doubles -------------------------------------------------------------------


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
def audit() -> Iterator[CosmosAudit]:
    audit = CosmosAudit(operations=operation_map())
    yield audit
    audit.check()


@pytest.fixture
def chat_ops() -> dict[str, Any]:
    """Ops-config values set before the app starts, beside the raised chat limits."""
    return {}


@pytest.fixture
def audited_app(  # noqa: PLR0917 - fixtures
    api_sql_engine: SqlEngine,
    sql_database: SqlTestDatabase,
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    signing_keys: SigningKeys,
    audit: CosmosAudit,
    chat_ops: dict[str, Any],
) -> BuiltApp:
    """The `api` fixture's app with the audit wrapped around its emulator containers."""
    return chat_app(
        sql_engine=api_sql_engine,
        sql_database=sql_database,
        cosmos=cosmos_database,
        signing_keys=signing_keys,
        chat=chat_fakes.ports(),
        ops=chat_ops,
        wrap=audit,
    )


@pytest.fixture
async def chat_api(
    audited_app: BuiltApp, api_sql_engine: SqlEngine, chat_fakes: ChatFakes
) -> AsyncIterator[ApiUnderTest]:
    try:
        async with serving(audited_app) as started:
            started.chat = chat_fakes
            yield started
    finally:
        await api_sql_engine.dispose()


@pytest.fixture
async def live(
    audited_app: BuiltApp, api_sql_engine: SqlEngine
) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the audited app served by Uvicorn (`live_api_server`'s server)."""
    try:
        async with (
            serve_live(audited_app.app) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=CLIENT_TIMEOUT) as client,
        ):
            yield client
    finally:
        await api_sql_engine.dispose()


@pytest.fixture
def conversations(cosmos_database: DatabaseProxy) -> ContainerProxy:
    """The `conversations` container without the audit, for the tests' own reads."""
    return cosmos_database.get_container_client(CONVERSATIONS.name)


type AppFactory = Callable[..., BuiltApp]
type FlowFactory = Callable[[httpx.AsyncClient], ChatFlow]


@pytest.fixture
def app_factory(
    api_sql_engine: SqlEngine,
    sql_database: SqlTestDatabase,
    cosmos_database: DatabaseProxy,
    signing_keys: SigningKeys,
) -> AppFactory:
    """`chat_app` over the session's databases: a test passes `chat`, `ops` and `wrap`. Call it
    from a synchronous fixture, outside the test's loop."""
    return partial(
        chat_app,
        sql_engine=api_sql_engine,
        sql_database=sql_database,
        cosmos=cosmos_database,
        signing_keys=signing_keys,
    )


@pytest.fixture
def flow_factory(
    admin: Engine,
    principals: dict[PrincipalName, SharedPrincipal],
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    conversations: ContainerProxy,
) -> FlowFactory:
    """Project A with `alice` and its default index X, served to the given client."""

    def make(client: httpx.AsyncClient) -> ChatFlow:
        world = seeded(client, admin, principals, cosmos_database, chat_fakes, "alice")
        return ChatFlow(world=world, conversations=conversations)

    return make


@pytest.fixture
def flow(  # noqa: PLR0917 - fixtures
    chat_api: ApiUnderTest,
    admin: Engine,
    principals: dict[PrincipalName, SharedPrincipal],
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    conversations: ContainerProxy,
) -> ChatFlow:
    """Project A with `alice` and `bob` and its default index X, in process."""
    world = seeded(chat_api.client, admin, principals, cosmos_database, chat_fakes, "alice", "bob")
    return ChatFlow(world=world, conversations=conversations)


@pytest.fixture
def live_flow(  # noqa: PLR0917 - fixtures
    live: httpx.AsyncClient,
    admin: Engine,
    principals: dict[PrincipalName, SharedPrincipal],
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    conversations: ContainerProxy,
) -> ChatFlow:
    """Project A with `alice` and its default index X, over real connections."""
    world = seeded(live, admin, principals, cosmos_database, chat_fakes, "alice")
    return ChatFlow(world=world, conversations=conversations)


# --- Over the model stub --------------------------------------------------------------------------


@pytest.fixture
def stub_ports(compose_model_stub: ComposeService, chat_fakes: ChatFakes) -> ChatPorts:
    """The API's own model and Prompt Shields clients aimed at the stub, built outside the
    test's loop; search stays the search double."""
    endpoint = compose_model_stub.address
    return ChatPorts(
        openai=api_openai_client(
            endpoint, STUB_TOKEN, connect_timeout_seconds=10, read_timeout_seconds=60
        ),
        search_clients=chat_fakes.search,
        contentsafety=contentsafety_client(endpoint, STUB_TOKEN, timeout_seconds=5),
        tokenizers=character_tokenizers,
    )


@pytest.fixture
def stub_app(  # noqa: PLR0917 - fixtures
    api_sql_engine: SqlEngine,
    sql_database: SqlTestDatabase,
    cosmos_database: DatabaseProxy,
    signing_keys: SigningKeys,
    audit: CosmosAudit,
    stub_ports: ChatPorts,
    chat_ops: dict[str, Any],
) -> BuiltApp:
    return chat_app(
        sql_engine=api_sql_engine,
        sql_database=sql_database,
        cosmos=cosmos_database,
        signing_keys=signing_keys,
        chat=stub_ports,
        ops=chat_ops,
        wrap=audit,
    )


@pytest.fixture
def stub_http(compose_model_stub: ComposeService) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=compose_model_stub.address,
        timeout=STUB_CONTROL_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    )


@pytest.fixture
async def stub(stub_http: httpx.AsyncClient) -> AsyncIterator[StubControl]:
    """The stub's recorder from this test's first request on, and its scripts, cleared after."""
    control = StubControl(stub_http)
    try:
        await control.mark()
        yield control
    finally:
        await control.clear_scripts()
        await stub_http.aclose()


@pytest.fixture
async def stub_live(
    stub_app: BuiltApp, api_sql_engine: SqlEngine, stub_ports: ChatPorts
) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the stub-backed app served by Uvicorn."""
    try:
        async with (
            serve_live(stub_app.app) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=STUB_READ_TIMEOUT) as client,
        ):
            yield client
    finally:
        await api_sql_engine.dispose()
        if stub_ports.openai is not None:
            await stub_ports.openai.close()
        if stub_ports.contentsafety is not None:
            await stub_ports.contentsafety.aclose()


@pytest.fixture
def stub_flow(  # noqa: PLR0917 - fixtures
    stub_live: httpx.AsyncClient,
    admin: Engine,
    principals: dict[PrincipalName, SharedPrincipal],
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    conversations: ContainerProxy,
) -> ChatFlow:
    """Project A with `alice` and its default index X, over the model stub."""
    world = seeded(stub_live, admin, principals, cosmos_database, chat_fakes, "alice")
    return ChatFlow(world=world, conversations=conversations)
