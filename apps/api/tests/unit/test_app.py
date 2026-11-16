from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from docchat_api import app as app_module
from docchat_api.app import (
    AREA_ROUTERS,
    NATIVE_TELEMETRY_OFF,
    Ports,
    _checked_routes,
    create_app,
)
from docchat_api.conversations.store import ConversationStore
from docchat_api.http.access import access
from docchat_api.http.context import CorrelationMiddleware
from docchat_api.http.headers import SecurityHeadersMiddleware
from docchat_api.http.limits import BodyGuardMiddleware
from docchat_api.http.problems import UnhandledErrorMiddleware
from docchat_api.http.routing import DocChatRoute
from docchat_api.http.state import PROCESS_STATE, ApiProcess
from docchat_api.openapi import STREAMS_STATE, ndjson_stream
from docchat_api.usage import UsageService
from docchat_core.clients import ClientRegistry
from docchat_core.opsconfig import OpsConfigInvalidError
from docchat_testing.fakes.limiters import PermissiveBurstLimiter, PermissiveWriteRateLimiter
from docchat_testing.fixtures.api import api_settings


def test_create_app_reads_no_setting(settings_env: pytest.MonkeyPatch) -> None:
    app = create_app()

    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.router.redirect_slashes is False


def test_the_middleware_runs_in_the_order_of_section_1_3() -> None:
    app = create_app()

    expected: list[object] = [
        SecurityHeadersMiddleware,
        CorrelationMiddleware,
        UnhandledErrorMiddleware,
        BodyGuardMiddleware,
    ]
    assert [cast("object", middleware.cls) for middleware in app.user_middleware] == expected


def test_fastapis_own_telemetry_is_off() -> None:
    assert NATIVE_TELEMETRY_OFF == {
        "tracing": False,
        "metrics": False,
        "logs": False,
        "operation_spans": False,
        "auto_configure": False,
    }


def test_every_v1_route_is_a_docchat_route() -> None:
    for router in AREA_ROUTERS:
        assert router.route_class is DocChatRoute
        assert all(isinstance(route, DocChatRoute) for route in router.routes)


def _app_with(router: APIRouter) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(router)
    return app


def test_an_admin_route_must_sit_under_v1_admin() -> None:
    router = APIRouter(prefix="/v1", route_class=DocChatRoute)

    @router.get("/projects-admin", operation_id="misplaced")
    @access("admin")
    async def misplaced() -> None:
        return

    with pytest.raises(TypeError, match="admin access exactly under"):
        _checked_routes(_app_with(router))


def test_a_user_route_must_sit_outside_v1_admin() -> None:
    router = APIRouter(prefix="/v1", route_class=DocChatRoute)

    @router.get("/admin/mine", operation_id="misplaced")
    @access("user")
    async def misplaced() -> None:
        return

    with pytest.raises(TypeError, match="admin access exactly under"):
        _checked_routes(_app_with(router))


def test_only_get_health_escapes_the_route_class() -> None:
    router = APIRouter()

    @router.get("/v1/plain")
    async def plain() -> None:
        return

    with pytest.raises(TypeError, match="DocChatRoute"):
        _checked_routes(_app_with(router))


class _Meta(BaseModel):
    type: Literal["meta"]


class _Done(BaseModel):
    type: Literal["done"]


def test_streaming_operations_are_collected() -> None:
    router = APIRouter(prefix="/v1", route_class=DocChatRoute)

    @router.post("/streams", operation_id="streamThings")
    @ndjson_stream("ThingEvent", _Meta | _Done)
    @access("user")
    async def stream() -> None:
        return

    streams = _checked_routes(_app_with(router))

    assert streams["streamThings"].name == "ThingEvent"
    assert set(getattr(create_app().state, STREAMS_STATE)) == {"createChatTurn"}


# --- The lifespan ---------------------------------------------------------------------------


class _Keys:
    def __init__(self, tenant: UUID) -> None:
        self.tenant = tenant
        self.events: list[str] = []
        _Keys.built.append(self)

    built: list[_Keys] = []  # noqa: RUF012 - the recorder of the instances the lifespan built

    async def get_key(self, kid: str) -> Any:
        raise AssertionError("no token in this test")

    def start(self) -> None:
        self.events.append("start")

    async def aclose(self) -> None:
        self.events.append("aclose")


class _OwnedEngine:
    def __init__(self, fake: Any, events: list[str]) -> None:
        self._fake = fake
        self.events = events
        self.opened = 8

    async def fetch_all(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append(f"read:{kwargs['name']}")
        return await self._fake.fetch_all(*args, **kwargs)

    async def warm(self) -> int:
        self.events.append("warm")
        return self.opened

    async def run_idle_release(self) -> None:
        self.events.append("idle-release")

    async def dispose(self) -> None:
        self.events.append("dispose")


async def _built(**arguments: Any) -> FastAPI:
    """`create_app` off the loop: building analyses every route's models for about 100 ms. The
    worker thread starts first, so the build never holds the interpreter lock while the loop's
    submitting step still runs."""
    await asyncio.to_thread(int)
    return await asyncio.to_thread(partial(create_app, **arguments))


async def test_the_lifespan_builds_the_process_from_the_environment(
    api_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch, fake_sql: Any
) -> None:
    events: list[str] = []
    configured: list[str] = []
    _Keys.built.clear()
    monkeypatch.setattr(app_module, "EntraSigningKeyProvider", _Keys)
    monkeypatch.setattr(
        app_module, "SqlEngine", lambda *args, **kwargs: _OwnedEngine(fake_sql, events)
    )
    monkeypatch.setattr(
        app_module, "configure_telemetry", lambda service, settings: configured.append(service)
    )
    app = await _built(ports=Ports(burst_limiter=PermissiveBurstLimiter()))

    async with app.router.lifespan_context(app):
        process = getattr(app.state, PROCESS_STATE)
        assert isinstance(process, ApiProcess)
        assert ClientRegistry._open is not None
        (keys,) = _Keys.built
        assert keys.tenant == UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
        assert keys.events == ["start"]
        assert process.settings.api.api_workers == 1
        assert type(process.write_limiter).__name__ == "WriteRateLimiter"

    assert configured == ["docchat-api"]
    assert keys.events == ["start", "aclose"]
    assert "dispose" in events
    assert getattr(app.state, PROCESS_STATE) is None
    assert ClientRegistry._open is None


async def test_the_lifespan_warms_its_engine_after_the_ops_config_read(
    api_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch, fake_sql: Any
) -> None:
    events: list[str] = []
    monkeypatch.setattr(app_module, "EntraSigningKeyProvider", _Keys)
    monkeypatch.setattr(
        app_module, "SqlEngine", lambda *args, **kwargs: _OwnedEngine(fake_sql, events)
    )
    monkeypatch.setattr(app_module, "configure_telemetry", lambda service, settings: None)
    app = await _built(ports=Ports(burst_limiter=PermissiveBurstLimiter()))

    async with app.router.lifespan_context(app):
        # Serving starts only after the warm-up, which follows the snapshot's read.
        assert [event for event in events if event != "idle-release"] == [
            "read:read_ops_config",
            "warm",
        ]

    assert events.count("warm") == 1


async def test_a_warm_up_that_opened_nothing_doesnt_stop_the_start_up(
    api_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch, fake_sql: Any
) -> None:
    events: list[str] = []
    engine = _OwnedEngine(fake_sql, events)
    engine.opened = 0
    monkeypatch.setattr(app_module, "EntraSigningKeyProvider", _Keys)
    monkeypatch.setattr(app_module, "SqlEngine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(app_module, "configure_telemetry", lambda service, settings: None)
    app = await _built(ports=Ports(burst_limiter=PermissiveBurstLimiter()))

    async with app.router.lifespan_context(app):
        assert isinstance(getattr(app.state, PROCESS_STATE), ApiProcess)

    assert "warm" in events


async def test_an_injected_engine_is_not_the_lifespans_to_release(fake_sql: Any) -> None:
    app = await _built(
        settings=api_settings(),
        ports=Ports(
            burst_limiter=PermissiveBurstLimiter(),
            write_limiter=PermissiveWriteRateLimiter(),
            sql_engine=fake_sql,
        ),
        signing_keys=_Keys(UUID(int=1)),
    )

    async with app.router.lifespan_context(app):
        assert getattr(app.state, PROCESS_STATE).sql is fake_sql
        # Its owner warms, releases and disposes it; the lifespan only reads the snapshot.
        assert fake_sql.names() == ["read_ops_config"]


async def test_an_invalid_ops_config_stops_the_start_up(fake_sql: Any) -> None:
    fake_sql.rows.pop("api.write_requests_per_minute")
    keys = _Keys(UUID(int=1))
    app = await _built(
        settings=api_settings(),
        ports=Ports(sql_engine=fake_sql, write_limiter=PermissiveWriteRateLimiter()),
        signing_keys=keys,
    )

    with pytest.raises(OpsConfigInvalidError):
        async with app.router.lifespan_context(app):
            pytest.fail("the app must not serve")

    assert keys.events == ["start", "aclose"]
    assert ClientRegistry._open is None


async def test_the_app_is_instrumented_once(fake_sql: Any) -> None:
    app = await _built(
        settings=api_settings(),
        ports=Ports(sql_engine=fake_sql, write_limiter=PermissiveWriteRateLimiter()),
        signing_keys=_Keys(UUID(int=1)),
    )

    async with app.router.lifespan_context(app):
        assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True
    async with app.router.lifespan_context(app):
        assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True


async def test_the_lifespan_builds_the_usage_service_and_the_conversation_store(
    fake_sql: Any,
) -> None:
    wrapped: list[str] = []

    def wrapper(container: Any) -> Any:
        wrapped.append(container.id)
        return container

    app = await _built(
        settings=api_settings(),
        ports=Ports(
            sql_engine=fake_sql,
            write_limiter=PermissiveWriteRateLimiter(),
            container_wrapper=wrapper,
        ),
        signing_keys=_Keys(UUID(int=1)),
    )

    async with app.router.lifespan_context(app):
        process = getattr(app.state, PROCESS_STATE)
        assert isinstance(process.usage, UsageService)
        assert UsageService.current() is process.usage
        assert isinstance(process.conversations, ConversationStore)

    assert sorted(wrapped) == ["conversations", "memories", "usage"]
    assert UsageService._current is None
