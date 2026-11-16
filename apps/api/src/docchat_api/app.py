"""The composition root: `create_app()` and the lifespan (`11-api.md` sections 1.1, 1.3, 1.11 and
1.15; `01-repository.md` sections 4.4 and 7.1; `85-observability.md` section 1.2).

`create_app` builds the one FastAPI application without settings or network access, so the image's
smoke step and the OpenAPI exporter can call it anywhere: no OpenAPI or documentation route, no
trailing-slash redirect (a redirect breaks CORS preflights), the middleware of section 1.3 in
order, the problem handlers, `GET /healthz` and the `/v1` router of `DocChatRoute`. It takes the
two request limiters as ports; the test suites also inject the data-access layer and a clock.

Each worker's lifespan, after the fork: validates the settings and configures telemetry (unless
the caller passed settings: a test harness owns the process's telemetry), instruments the app (the
instrumentation can only wrap an app that exists), creates the client registry (one credential per
process), the data-access layer, the signing-key provider and its background task, the token
validator, the access checks, the last-seen throttle and the write limiter; loads the ops-config
snapshot (start-up check 11); warms the data-access layer it created (`01-repository.md` section
6.2 item 11), which never stops the start-up; builds the usage service (installed as the
process's), the conversation store over the Cosmos DB containers with its after-response work (the
chat pipeline's `post_turn`, unless the caller passed one), and the chat service over the model,
search and Prompt Shields clients (the Prompt Shields caller installed as the process's); and runs
the SQL idle release and the loop-lag monitor in a task group. Any other failure stops the worker
before it serves.
Shutdown cancels the background tasks, waits for the turns' finalisers, cancels the after-response
tasks still running (their leases expire), waits for the usage service's shielded writes, stops the
signing-key task and closes the data-access layer and every client.

A new `/v1` area adds its module's router to `AREA_ROUTERS`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Final, cast

from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.telemetry import TelemetryConfig
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.middleware import Middleware

from docchat_api.chat import citation_routes
from docchat_api.chat import route as chat_route
from docchat_api.chat.service import (
    ChatPorts,
    EncodingCache,
    Tokenizers,
    build_service,
    deployments,
)
from docchat_api.conversations import feedback_routes, memory_routes, session_routes
from docchat_api.conversations.extraction import MemoryContainer
from docchat_api.conversations.post_turn import PostTurnWorker, build_post_turn_work
from docchat_api.conversations.store import (
    ConversationsContainer,
    ConversationStore,
    MemoriesContainer,
)
from docchat_api.http.context import CorrelationMiddleware
from docchat_api.http.headers import SecurityHeadersMiddleware
from docchat_api.http.limits import BodyGuardMiddleware
from docchat_api.http.problems import UnhandledErrorMiddleware, install_problem_handlers
from docchat_api.http.ratelimit import (
    BurstLimiter,
    BurstLimiterPort,
    WindowStore,
    WriteLimiterPort,
    WriteRateLimiter,
)
from docchat_api.http.routing import DocChatRoute
from docchat_api.http.state import PROCESS_STATE, ApiProcess
from docchat_api.openapi import STREAMS_STATE, StreamSpec, build_document, stream_of
from docchat_api.routes import (
    admin_document_pdf,
    admin_documents,
    admin_indexes,
    admin_projects,
    admin_runs,
    admin_uploads,
    admin_users,
    health,
    me,
    projects,
)
from docchat_api.settings import ProcessSettings, load_process_settings
from docchat_api.usage import UsageContainer, UsageService
from docchat_core.access import AccessChecks
from docchat_core.auth import EntraSigningKeyProvider, SigningKeyProvider, TokenValidator
from docchat_core.clients import ClientRegistry, ServiceEndpoints
from docchat_core.clients.openai import ClientProfile
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.ingestion.names import INGESTION_QUEUE
from docchat_core.models.clock import Clock, SystemClock
from docchat_core.opsconfig import API_AREAS, OpsConfig, OpsConfigStore
from docchat_core.sql.engine import SqlEngine
from docchat_core.telemetry import configure_telemetry, monitor_loop_lag
from docchat_core.users import LastSeenThrottle

if TYPE_CHECKING:
    from azure.cosmos.aio import ContainerProxy
    from openai import AsyncOpenAI

type ContainerWrapper = Callable[[ContainerProxy], ContainerProxy]
"""Wraps a Cosmos DB container client the lifespan builds: the data-access audit or injected
faults (`docchat_testing.data_audit`)."""

TITLE: Final = "DocChat API"
API_VERSION: Final = "1"
V1_PREFIX: Final = "/v1"
ADMIN_PREFIX: Final = "/v1/admin"
HEALTH_PATH: Final = "/healthz"
SERVICE: Final = "docchat-api"
CONFIG_STATE: Final = "docchat_config"

AREA_ROUTERS: Final[tuple[APIRouter, ...]] = (
    me.router,
    projects.router,
    admin_projects.router,
    admin_users.router,
    admin_indexes.router,
    admin_documents.router,
    admin_uploads.router,
    admin_runs.router,
    chat_route.router,
    citation_routes.router,
    admin_document_pdf.router,
    session_routes.router,
    feedback_routes.router,
    memory_routes.router,
)
"""Every `/v1` area's router, mounted on the one `/v1` router of `DocChatRoute`."""

# FastAPI's built-in OpenTelemetry support would add its own spans, metrics and exception logs
# beside the Azure Monitor distro's instrumentation, and its logs carry exception messages; the
# distro's server span is the only request span the design has.
NATIVE_TELEMETRY_OFF: Final[TelemetryConfig] = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Ports:
    """What the composition root takes from its caller instead of building. `None` builds the
    real one: the per-worker `BurstLimiter`, the Cosmos DB `WriteRateLimiter`, the data-access
    layer from the settings, the system clock, the container clients unwrapped, the registry's
    model, search and Prompt Shields clients."""

    burst_limiter: BurstLimiterPort | None = None
    write_limiter: WriteLimiterPort | None = None
    sql_engine: SqlEngine | None = None
    clock: Clock | None = None
    container_wrapper: ContainerWrapper | None = None
    chat: ChatPorts | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AppConfig:
    """`create_app`'s arguments, kept for the lifespan."""

    settings: ProcessSettings | None
    ports: Ports
    signing_keys: SigningKeyProvider | None
    burst_limiter: BurstLimiterPort
    clock: Clock = field(default_factory=SystemClock)


def _instrument(app: FastAPI) -> None:
    # The distro instruments FastAPI applications created after it is configured; this one was
    # created before, at import of the worker, so it is instrumented here and its middleware stack
    # is rebuilt with the server span outermost at the next request.
    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        FastAPIInstrumentor.instrument_app(app)
        app.middleware_stack = None


async def _started(ops: OpsConfigStore, *, warm: SqlEngine | None) -> OpsConfig:
    """The first ops-config snapshot (start-up check 11), then the warm-up of the engine the
    lifespan created (`01-repository.md` section 6.2 item 11), after the snapshot's read, whose
    retries ride out a resuming database. An engine a caller injects is the caller's to warm, as
    it is the caller's to release and dispose."""
    snapshot = await ops.start()
    if warm is not None:
        await warm.warm()
    return snapshot


def _conversation_store(
    container: Callable[[str], ContainerProxy],
    *,
    settings: ProcessSettings,
    chat_ports: ChatPorts,
    tokenizers: Tokenizers,
    ops: OpsConfigStore,
    snapshot: OpsConfig,
    openai: AsyncOpenAI,
    sql: SqlEngine,
    clock: Clock,
) -> tuple[ConversationStore, PostTurnWorker | None]:
    """The conversation store over the `conversations` and `memories` containers, and its
    after-response work unless the caller passed the chat pipeline's `post_turn`. Both count in
    the chat encoding."""
    conversations = container(CONVERSATIONS.name)
    memories = container(MEMORIES.name)
    chat_tokenizer = tokenizers(settings.api.openai_chat_encoding)
    # The SDK's overloaded signatures are wider than the ports, which name only the calls each
    # component makes.
    store = ConversationStore(
        cast("ConversationsContainer", conversations),
        cast("MemoriesContainer", memories),
        ops=ops,
        tokenizer=chat_tokenizer,
        clock=clock,
    )
    if chat_ports.post_turn is not None:
        return store, None
    worker = build_post_turn_work(
        conversations=cast("ConversationsContainer", conversations),
        memories=cast("MemoryContainer", memories),
        ops=ops,
        snapshot=snapshot,
        openai=openai,
        small=deployments(settings.api, tokenizers)[1],
        tokenizer=chat_tokenizer,
        sql=sql,
        tenant_id=settings.auth.auth_tenant_id,
        workload_principal_id=settings.core.core_workload_principal_id,
        clock=clock,
    )
    return store, worker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = cast("AppConfig", getattr(app.state, CONFIG_STATE))
    if config.settings is None:
        settings = load_process_settings()
        configure_telemetry(SERVICE, settings.telemetry)
    else:
        # The caller is a test harness: it configured the process's telemetry itself (the
        # plugin's in-memory exporters), because the OpenTelemetry providers can be set once.
        settings = config.settings
    _instrument(app)
    async with AsyncExitStack() as stack:
        registry = ClientRegistry(
            settings.core,
            settings.local,
            profile=ClientProfile.API,
            endpoints=ServiceEndpoints(
                cosmos_endpoint=settings.api.cosmos_endpoint,
                cosmos_database=settings.api.cosmos_database,
                blob_endpoint=settings.upload.storage_blob_endpoint,
                queue_endpoint=settings.upload.storage_queue_endpoint,
                queue_names=(INGESTION_QUEUE,),
                search_endpoint=settings.api.search_endpoint,
                search_timeout_seconds=settings.api.search_timeout_seconds,
                openai_endpoint=settings.api.openai_endpoint,
                openai_connect_timeout_seconds=settings.api.openai_connect_timeout_seconds,
                openai_read_timeout_seconds=settings.api.openai_read_timeout_seconds,
                contentsafety_endpoint=settings.api.contentsafety_endpoint,
                contentsafety_timeout_seconds=settings.api.contentsafety_timeout_seconds,
            ),
        )
        stack.push_async_callback(registry.close)
        sql = config.ports.sql_engine
        owns_sql = sql is None
        if sql is None:
            sql = SqlEngine(
                "api", sql=settings.sql, local=settings.local, credential=registry.credential
            )
            stack.push_async_callback(sql.dispose)
        keys = config.signing_keys or EntraSigningKeyProvider(settings.auth.auth_tenant_id)
        keys.start()
        stack.push_async_callback(keys.aclose)
        ops = OpsConfigStore(
            sql, API_AREAS, chat_small_efforts=settings.api.chat_small_efforts, clock=config.clock
        )
        snapshot = await _started(ops, warm=sql if owns_sql else None)
        database = registry.cosmos_database
        wrap = config.ports.container_wrapper

        def container(name: str) -> ContainerProxy:
            client = database.get_container_client(name)
            return client if wrap is None else wrap(client)

        # The SDK's overloaded signatures are wider than the ports, which name only the calls
        # each component makes.
        usage_container = container(USAGE.name)
        write_limiter = config.ports.write_limiter or WriteRateLimiter(
            cast("WindowStore", usage_container), clock=config.clock
        )
        usage = UsageService(
            cast("UsageContainer", usage_container), ops=ops, clock=config.clock
        ).install()
        stack.push_async_callback(usage.aclose)
        chat_ports = config.ports.chat or ChatPorts()
        tokenizers = chat_ports.tokenizers or EncodingCache()
        openai = chat_ports.openai or registry.openai
        conversations, worker = _conversation_store(
            container,
            settings=settings,
            chat_ports=chat_ports,
            tokenizers=tokenizers,
            ops=ops,
            snapshot=snapshot,
            openai=openai,
            sql=sql,
            clock=config.clock,
        )
        chat = build_service(
            api=settings.api,
            embeddings=settings.embeddings,
            openai=openai,
            search_clients=registry.search_client
            if chat_ports.search_clients is None
            else chat_ports.search_clients,
            contentsafety=chat_ports.contentsafety or registry.contentsafety,
            sql=sql,
            conversations=conversations,
            usage=usage,
            clock=config.clock,
            post_turn=chat_ports.post_turn or worker,
            tokenizers=tokenizers,
        )
        chat.shields.install()
        stack.callback(chat.shields.uninstall)
        if worker is not None:
            # Closed after the turns' finalisers, which schedule its tasks, and before the
            # Prompt Shields caller and the usage service its tasks use.
            stack.push_async_callback(worker.aclose)
        stack.push_async_callback(chat.aclose)
        process = ApiProcess(
            settings=settings,
            sql=sql,
            ops=ops,
            access=AccessChecks(sql, clock=config.clock),
            throttle=LastSeenThrottle(config.clock),
            validator=TokenValidator(
                settings.auth, environment=settings.core.core_environment, keys=keys
            ),
            burst_limiter=config.burst_limiter,
            write_limiter=write_limiter,
            usage=usage,
            conversations=conversations,
            chat=chat,
        )
        async with asyncio.TaskGroup() as group:
            background = [
                group.create_task(
                    monitor_loop_lag(settings.telemetry.telemetry_loop_lag_interval_ms)
                )
            ]
            if owns_sql:
                background.append(group.create_task(sql.run_idle_release()))
            setattr(app.state, PROCESS_STATE, process)
            try:
                yield
            finally:
                setattr(app.state, PROCESS_STATE, None)
                for task in background:
                    task.cancel()


def _checked_routes(app: FastAPI) -> dict[str, StreamSpec]:
    """The streaming operations, after checking every route: `GET /healthz` is the one route
    the route class doesn't serve, an `admin` route sits under `/v1/admin` and every other `/v1`
    route outside it."""
    streams: dict[str, StreamSpec] = {}
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, DocChatRoute):
            if context.path != HEALTH_PATH:
                raise TypeError(f"{context.path}: a /v1 route is a DocChatRoute")
            continue
        path = str(context.path)
        under_admin = path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")
        if under_admin != (route.access_spec.level == "admin"):
            raise TypeError(f"{route.operation_id}: admin access exactly under {ADMIN_PREFIX}")
        stream = stream_of(route.endpoint)
        if stream is not None:
            streams[str(route.operation_id)] = stream
    return streams


def create_app(
    *,
    settings: ProcessSettings | None = None,
    ports: Ports | None = None,
    signing_keys: SigningKeyProvider | None = None,
) -> FastAPI:
    """The API application; reads no setting and touches no network."""
    given = Ports() if ports is None else ports
    clock: Clock = SystemClock() if given.clock is None else given.clock
    app = FastAPI(
        title=TITLE,
        version=API_VERSION,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
        separate_input_output_schemas=False,
        telemetry=NATIVE_TELEMETRY_OFF,
        lifespan=lifespan,
        # Outermost first, inside the instrumentation's server span (section 1.3 step 1).
        middleware=[
            Middleware(SecurityHeadersMiddleware),
            Middleware(CorrelationMiddleware),
            Middleware(UnhandledErrorMiddleware),
            Middleware(BodyGuardMiddleware),
        ],
    )
    install_problem_handlers(app)
    app.include_router(health.router)
    v1 = APIRouter(prefix=V1_PREFIX, route_class=DocChatRoute)
    for router in AREA_ROUTERS:
        v1.include_router(router)
    app.include_router(v1)
    setattr(app.state, STREAMS_STATE, _checked_routes(app))
    setattr(
        app.state,
        CONFIG_STATE,
        AppConfig(
            settings=settings,
            ports=given,
            signing_keys=signing_keys,
            # Not `or`: a burst limiter with no buckets yet has length 0, so it is falsy.
            burst_limiter=BurstLimiter(clock)
            if given.burst_limiter is None
            else given.burst_limiter,
            clock=clock,
        ),
    )
    app.openapi = partial(build_document, app)  # type: ignore[method-assign]
    return app
