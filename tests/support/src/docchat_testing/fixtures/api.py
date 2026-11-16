"""The in-process API (`84-testing.md` sections 1.4 and 3.1; `11-api.md` section 1.11 "Test seam").

`build_app` builds the app with `create_app(settings=…, ports=…, signing_keys=…)` outside the
test's event loop, because analysing every route's models takes tens of milliseconds; `serving`
runs its lifespan through `app.router.lifespan_context(app)` and serves it to an
`httpx.AsyncClient` over `ASGITransport` (`running_api` does both), so a request passes the
whole middleware stack, the route class and the problem handlers exactly as in a worker. The app
trusts the session's signing keys (`StaticSigningKeyProvider` over
`tokens.session_signing_keys()`), so `mint_token` tokens validate, and its `AuthSettings` name
`tokens.SESSION_AUTH`'s tenant and registrations. Its ports carry the permissive limiters unless
the test passes `real_limiters=True`.

The `api` fixture serves the app against the session's SQL Server database as the `docchat_api`
role and the session's Cosmos DB emulator database (`emulator_containers` puts its `usage`,
`conversations` and `memories` containers in place of the ones the lifespan builds), with the chat
pipeline over the in-memory model, search and Prompt Shields doubles (`ApiUnderTest.chat`);
`live_api_server` serves the same app through Uvicorn on a random loopback port (`serve_live`) for
the streaming and disconnect tests, which need real connections (section 2.9). The
chat path's per-user limits are raised for it, as the permissive limiters lift the burst and write
limits: the suites that use it send many turns as one shared principal, and the limits' own tests
build their services directly. The data-access layer and the app (`api_app`) are built in
synchronous fixtures, outside the test's event loop, because creating the SQLAlchemy engine loads
the dialect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final
from uuid import UUID

import httpcore2  # noqa: F401 - loaded outside the loop, see the note below
import httpx
import pytest
import uvicorn
from azure.cosmos.aio import ContainerProxy, DatabaseProxy
from fastapi import APIRouter, FastAPI
from pydantic import JsonValue, SecretStr
from sqlalchemy import update

from docchat_api.app import AREA_ROUTERS, V1_PREFIX, ContainerWrapper, Ports, create_app
from docchat_api.chat.service import ChatPorts
from docchat_api.conversations.post_turn import PostTurnWorker
from docchat_api.http.ratelimit import BurstLimiter
from docchat_api.http.routing import DocChatRoute
from docchat_api.http.state import PROCESS_STATE, ApiProcess
from docchat_api.settings import ApiSettings, ProcessSettings
from docchat_core.auth import AuthSettings, StaticSigningKeyProvider
from docchat_core.ingestion.settings import EmbeddingModelCatalog, UploadSettings
from docchat_core.models.clock import Clock
from docchat_core.opsconfig import encode_value
from docchat_core.settings import CoreSettings, LocalSettings
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import ops_config
from docchat_core.telemetry import TelemetrySettings
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fakes.limiters import PermissiveBurstLimiter, PermissiveWriteRateLimiter
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.tokens import SESSION_AUTH, MintSettings, SigningKeys, session_signing_keys

# The lifespan builds the API profile's OpenAI client on the loop, and its transport (`httpx2`)
# imports `httpcore2` when the first client is built: a fifth of a second the first time in a
# process. Imported with this module, outside any loop, the session's first lifespan doesn't trip
# the loop guard.

BASE_URL: Final = "http://docchat-api.test"
CLIENT_TIMEOUT: Final = httpx.Timeout(30.0, connect=5.0)
"""The in-process client never waits on a network; the bound only keeps a hung app from
hanging the test past its own timeout."""
WORKLOAD_PRINCIPAL_ID: Final = UUID("5f1c0de0-0000-4000-8000-0000000000a1")
TEST_SQL_THREADS: Final = 4

# Hosts under `.invalid` never resolve: the in-process app builds clients for them and calls none.
TEST_ENDPOINTS: Final[Mapping[str, str]] = {
    "cosmos_endpoint": "https://cosmos.docchat-test.invalid:443/",
    "search_endpoint": "https://search.docchat-test.invalid",
    "openai_endpoint": "https://openai.docchat-test.invalid",
    "contentsafety_endpoint": "https://contentsafety.docchat-test.invalid",
    "storage_blob_endpoint": "https://blob.docchat-test.invalid/",
    "storage_queue_endpoint": "https://queue.docchat-test.invalid/",
}


def api_settings(
    *, mint: MintSettings = SESSION_AUTH, api: Mapping[str, Any] | None = None
) -> ProcessSettings:
    """A valid set of the API's settings for an in-process app: a `local` process whose
    `AuthSettings` match the minted tokens. `model_validate` reads only the values given, never
    the process environment; `api` overrides `ApiSettings` fields."""
    return ProcessSettings(
        core=CoreSettings.model_validate(
            {"core_environment": "local", "core_workload_principal_id": WORKLOAD_PRINCIPAL_ID}
        ),
        auth=AuthSettings.model_validate(
            {
                "auth_tenant_id": str(mint.tenant_id),
                "auth_api_client_id": str(mint.api_client_id),
                "auth_web_client_id": str(mint.web_client_id),
                "auth_test_client_id": str(mint.test_client_id),
            }
        ),
        telemetry=TelemetrySettings.model_validate({"build_commit": "0" * 40}),
        sql=SqlSettings.model_validate({}),
        local=LocalSettings.model_validate({}),
        upload=UploadSettings.model_validate(
            {
                "search_max_indexes": 15,
                "storage_blob_endpoint": TEST_ENDPOINTS["storage_blob_endpoint"],
                "storage_queue_endpoint": TEST_ENDPOINTS["storage_queue_endpoint"],
                "storage_malware_scanning_enabled": False,
            }
        ),
        embeddings=EmbeddingModelCatalog.model_validate({}),
        api=ApiSettings.model_validate(
            {
                "cosmos_endpoint": TEST_ENDPOINTS["cosmos_endpoint"],
                "search_endpoint": TEST_ENDPOINTS["search_endpoint"],
                "openai_endpoint": TEST_ENDPOINTS["openai_endpoint"],
                "contentsafety_endpoint": TEST_ENDPOINTS["contentsafety_endpoint"],
                "api_workers": 1,
                **(api or {}),
            }
        ),
    )


def ports_under_test(
    *,
    sql_engine: SqlEngine | None = None,
    clock: Clock | None = None,
    real_limiters: bool = False,
    container_wrapper: ContainerWrapper | None = None,
    chat: ChatPorts | None = None,
) -> Ports:
    """The ports of an app under test: the permissive limiters, or the real burst limiter with
    the write limiter left to the lifespan (the Cosmos DB window) when `real_limiters`;
    `container_wrapper` replaces or wraps the Cosmos DB containers, `chat` the chat pipeline's
    model, search and Prompt Shields clients."""
    if real_limiters:
        return Ports(
            burst_limiter=BurstLimiter(clock),
            sql_engine=sql_engine,
            clock=clock,
            container_wrapper=container_wrapper,
            chat=chat,
        )
    return Ports(
        burst_limiter=PermissiveBurstLimiter(),
        write_limiter=PermissiveWriteRateLimiter(),
        sql_engine=sql_engine,
        clock=clock,
        container_wrapper=container_wrapper,
        chat=chat,
    )


@dataclass(slots=True)
class ApiUnderTest:
    """A started app, its client, the ports it was given and, for the `api` fixture, the chat
    pipeline's doubles."""

    app: FastAPI
    client: httpx.AsyncClient
    ports: Ports
    operations: dict[tuple[str, str], str] = field(default_factory=dict)
    chat: ChatFakes | None = None


def operation_map() -> dict[tuple[str, str], str]:
    """`(HTTP method, route template)` to operationId, as `data_audit.CosmosAudit` attributes
    calls made inside a request's server span; read from the routers, so no document is built
    on the test's event loop."""
    operations: dict[tuple[str, str], str] = {("GET", "/healthz"): "getHealth"}
    for router in AREA_ROUTERS:
        for route in router.routes:
            if isinstance(route, DocChatRoute) and route.operation_id is not None:
                for method in route.methods or ():
                    operations[(method, V1_PREFIX + route.path)] = route.operation_id
    return operations


@dataclass(frozen=True, slots=True)
class BuiltApp:
    """An app built for one test, not started yet, and the ports it was given."""

    app: FastAPI
    ports: Ports


def build_app(
    *,
    settings: ProcessSettings | None = None,
    ports: Ports | None = None,
    signing_keys: SigningKeys | None = None,
    routers: Sequence[APIRouter] = (),
) -> BuiltApp:
    """The app of one test, built synchronously: building it analyses every route's models for
    tens of milliseconds, more with each area, so a fixture builds it outside the test's event
    loop and `serving` starts it there. `routers` adds a test's own routes (a platform test's
    probe routes) beside the app's."""
    keys = session_signing_keys() if signing_keys is None else signing_keys
    given = ports_under_test() if ports is None else ports
    app = create_app(
        settings=api_settings() if settings is None else settings,
        ports=given,
        signing_keys=StaticSigningKeyProvider(keys.public_keys()),
    )
    for router in routers:
        app.include_router(router)
    return BuiltApp(app=app, ports=given)


@asynccontextmanager
async def serving(built: BuiltApp) -> AsyncIterator[ApiUnderTest]:
    """A built app started (its lifespan) and served to an `httpx.AsyncClient`. Exceptions stay
    inside the app (`raise_app_exceptions=False`), whose failure boundary answers them."""
    app = built.app
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, timeout=CLIENT_TIMEOUT, follow_redirects=False
        ) as client:
            yield ApiUnderTest(app=app, client=client, ports=built.ports)


async def after_response_idle(app: FastAPI) -> None:
    """Wait until the started app's turns are finalised and the after-response work they
    scheduled has ended (`12-chat-pipeline.md` section 12.5; `13-conversations-and-memory.md`
    section 6.4), so a test counts model calls and reads stores after it rather than racing it.
    A stream can end before its finaliser has stored a `failed` or `cancelled` turn or scheduled
    a `completed` turn's work, so the finalisers come first. An app given another `post_turn` has
    no after-response work to wait for."""
    process = getattr(app.state, PROCESS_STATE, None)
    if not isinstance(process, ApiProcess):
        raise TypeError("the app holds no started ApiProcess")
    await process.chat.tracker.wait_held()
    worker = process.chat.post_turn
    if isinstance(worker, PostTurnWorker):
        await worker.wait_idle()


LIVE_HOST: Final = "127.0.0.1"


@asynccontextmanager
async def serve_live(app: FastAPI) -> AsyncIterator[str]:
    """Uvicorn serving `app` on the test's own loop at a random port of the loopback interface,
    for the streaming and disconnect tests that need real connections; yields the base URL. The
    app's lifespan runs inside the server, as in a worker."""
    config = uvicorn.Config(
        app, host=LIVE_HOST, port=0, lifespan="on", log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    serving_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if serving_task.done():
                serving_task.result()
                raise RuntimeError("the live API server stopped before it started")
            await asyncio.sleep(0.01)
        (listener,) = server.servers
        port = listener.sockets[0].getsockname()[1]
        yield f"http://{LIVE_HOST}:{port}"
    finally:
        server.should_exit = True
        await serving_task


@asynccontextmanager
async def running_api(
    *,
    settings: ProcessSettings | None = None,
    ports: Ports | None = None,
    signing_keys: SigningKeys | None = None,
    routers: Sequence[APIRouter] = (),
) -> AsyncIterator[ApiUnderTest]:
    """`build_app` and `serving` in one step, for a test that starts its app inside its own
    coroutine: the app is built in a worker thread, so the build never holds the loop."""
    # The worker thread starts first, on its own: a thread started for the build would take the
    # interpreter lock straight into the build's long native calls while the loop's submitting
    # step still waits for it, and that step would count as a blocked loop.
    await asyncio.to_thread(int)
    built = await asyncio.to_thread(
        partial(build_app, settings=settings, ports=ports, signing_keys=signing_keys),
        routers=routers,
    )
    async with serving(built) as started:
        yield started


@pytest.fixture
def api_sql_engine(sql_database: SqlTestDatabase) -> SqlEngine:
    """The in-process API's data-access layer, as the `docchat_api` role of the session's
    database; the `api` fixture disposes of it."""
    return SqlEngine(
        "api",
        sql=SqlSettings(sql_threads=TEST_SQL_THREADS),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=sql_database.connection_string("docchat_api"),
    )


CHAT_LIMITS: Final[Mapping[str, int]] = {
    "ratelimit.chat_requests_per_minute": 1200,
    "ratelimit.max_concurrent_streams": 200,
}
"""The chat path's per-user limits in the suites of the `api` fixture: each value's maximum."""


def set_ops_config(sql_database: SqlTestDatabase, values: Mapping[str, JsonValue]) -> None:
    """Store each value in `app.ops_config` in its canonical form `{"value": <value>}` before
    the app loads its snapshot; the SQL fixture's clean-up restores the seeded rows after the
    test."""
    engine = sql_database.engine()
    try:
        with engine.begin() as connection:
            for key, value in values.items():
                connection.execute(
                    update(ops_config)
                    .where(ops_config.c.config_key == key)
                    .values(value_json=encode_value(value))
                )
    finally:
        engine.dispose()


def raise_chat_limits(sql_database: SqlTestDatabase) -> None:
    """Set `CHAT_LIMITS` in `app.ops_config` before the app loads its snapshot."""
    set_ops_config(sql_database, CHAT_LIMITS)


def emulator_containers(
    database: DatabaseProxy, wrap: ContainerWrapper | None = None
) -> ContainerWrapper:
    """A container wrapper that serves the session's emulator containers, by name, in place of the
    ones the lifespan builds (its database is the fixed `docchat`), optionally wrapped again
    (`data_audit.CosmosAudit`)."""

    def replace(container: ContainerProxy) -> ContainerProxy:
        emulator = database.get_container_client(container.id)
        return emulator if wrap is None else wrap(emulator)

    return replace


@pytest.fixture
def chat_fakes() -> ChatFakes:
    return ChatFakes()


@pytest.fixture
def api_app(
    api_sql_engine: SqlEngine,
    sql_database: SqlTestDatabase,
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    signing_keys: SigningKeys,
) -> BuiltApp:
    """The in-process API over the session's databases, with the permissive limiters and the
    chat doubles, built outside the test's event loop."""
    raise_chat_limits(sql_database)
    return build_app(
        ports=ports_under_test(
            sql_engine=api_sql_engine,
            container_wrapper=emulator_containers(cosmos_database),
            chat=chat_fakes.ports(),
        ),
        signing_keys=signing_keys,
    )


@pytest.fixture
async def live_api_server(api_app: BuiltApp, api_sql_engine: SqlEngine) -> AsyncIterator[str]:
    """The `api` fixture's app served by Uvicorn on a random loopback port, for the streaming
    and disconnect tests; the base URL."""
    try:
        async with serve_live(api_app.app) as base_url:
            yield base_url
    finally:
        await api_sql_engine.dispose()


@pytest.fixture
async def api(
    api_app: BuiltApp, api_sql_engine: SqlEngine, chat_fakes: ChatFakes
) -> AsyncIterator[ApiUnderTest]:
    """The in-process API over the session's databases, started."""
    try:
        async with serving(api_app) as started:
            started.operations = operation_map()
            started.chat = chat_fakes
            yield started
    finally:
        await api_sql_engine.dispose()
