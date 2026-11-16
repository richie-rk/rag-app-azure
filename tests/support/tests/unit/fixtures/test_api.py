from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from docchat_api.http.access import access
from docchat_api.http.ratelimit import BurstLimiter
from docchat_api.http.routing import DocChatRoute
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.opsconfig import decode_value
from docchat_core.settings import EnvironmentName
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories.users import ProvisionResult
from docchat_testing.builders.cosmos import session_document, turn_document
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fakes.limiters import PermissiveBurstLimiter, PermissiveWriteRateLimiter
from docchat_testing.fixtures.api import (
    CHAT_LIMITS,
    TEST_ENDPOINTS,
    BuiltApp,
    api_settings,
    build_app,
    operation_map,
    ports_under_test,
    raise_chat_limits,
    running_api,
    serve_live,
    serving,
    set_ops_config,
)
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.tokens import SESSION_AUTH, SigningKeys


def test_the_settings_name_the_minted_tokens_tenant_and_registrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The settings are built from values only, never from the process environment.
    monkeypatch.setenv("DOCCHAT_API_WORKERS", "16")

    settings = api_settings()

    assert settings.core.core_environment is EnvironmentName.LOCAL
    assert settings.auth.auth_tenant_id == SESSION_AUTH.tenant_id
    assert settings.auth.auth_api_client_id == SESSION_AUTH.api_client_id
    assert settings.auth.auth_web_client_id == SESSION_AUTH.web_client_id
    assert settings.auth.auth_test_client_id == SESSION_AUTH.test_client_id
    assert settings.api.api_workers == 1
    assert settings.api.cosmos_endpoint == TEST_ENDPOINTS["cosmos_endpoint"]
    assert api_settings(api={"api_workers": 3}).api.api_workers == 3


def test_every_test_endpoint_is_an_invalid_host() -> None:
    for endpoint in TEST_ENDPOINTS.values():
        assert re.search(r"\.invalid(:443)?/?$", endpoint), endpoint


def test_the_ports_carry_the_permissive_limiters_unless_the_real_ones_are_asked_for() -> None:
    clock = FakeClock()

    permissive = ports_under_test(clock=clock)
    real = ports_under_test(clock=clock, real_limiters=True)

    assert isinstance(permissive.burst_limiter, PermissiveBurstLimiter)
    assert isinstance(permissive.write_limiter, PermissiveWriteRateLimiter)
    assert isinstance(real.burst_limiter, BurstLimiter)
    assert real.write_limiter is None
    assert real.clock is clock


def test_the_operation_map_names_every_route() -> None:
    operations = operation_map()

    assert operations[("GET", "/healthz")] == "getHealth"
    assert operations[("GET", "/v1/me")] == "getCurrentUser"
    assert operations[("GET", "/v1/projects")] == "listMyProjects"


def _seeded_rows() -> dict[str, str]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return dict(re.findall(r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `\w+` \|$", text, re.M))
    raise FileNotFoundError("80-data-model.md")


class _Rows:
    def __init__(self) -> None:
        self.rows = _seeded_rows()

    async def fetch_all(self, statement: object, parameters: object = None, **kwargs: Any) -> Any:
        return list(self.rows.items())

    async def run(self, work: Callable[[Any], Any], **kwargs: Any) -> Any:
        return ProvisionResult(provisioned=False, default_project_count=0)


async def test_running_api_serves_the_app_with_the_session_keys_and_extra_routers(
    signing_keys: SigningKeys, alice: SharedPrincipal
) -> None:
    router = APIRouter(prefix="/v1", route_class=DocChatRoute)

    @router.get("/extra", operation_id="extra")
    @access("user")
    async def extra() -> dict[str, str]:
        return {"ok": "yes"}

    ports = ports_under_test(sql_engine=cast("SqlEngine", _Rows()))
    async with running_api(ports=ports, signing_keys=signing_keys, routers=[router]) as api:
        me = await api.client.get("/v1/me", headers=alice.authorization())
        added = await api.client.get("/v1/extra", headers=alice.authorization())
        failed = await api.client.get("/v1/me", headers={"Authorization": "Bearer x.y.z"})

    assert me.status_code == 200
    assert added.json() == {"ok": "yes"}
    assert failed.status_code == 401
    assert api.ports is ports


@pytest.fixture
def built_outside(signing_keys: SigningKeys) -> BuiltApp:
    return build_app(
        ports=ports_under_test(sql_engine=cast("SqlEngine", _Rows())), signing_keys=signing_keys
    )


async def test_an_app_built_outside_the_loop_is_served_with_its_ports(
    built_outside: BuiltApp, alice: SharedPrincipal
) -> None:
    built = built_outside
    ports = built.ports

    async with serving(built) as api:
        me = await api.client.get("/v1/me", headers=alice.authorization())

    assert me.status_code == 200
    assert (api.app, api.ports) == (built.app, ports)


class _RecordingEngine:
    """Stands for `sa`'s engine of the session database: records each statement executed."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.disposed = False

    def begin(self) -> _RecordingEngine:
        return self

    def __enter__(self) -> _RecordingEngine:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    def dispose(self) -> None:
        self.disposed = True


def test_raise_chat_limits_stores_each_value_in_the_canonical_form_the_store_reads() -> None:
    engine = _RecordingEngine()

    raise_chat_limits(cast("SqlTestDatabase", SimpleNamespace(engine=lambda: engine)))

    written = {
        params["config_key_1"]: params["value_json"]
        for params in (statement.compile().params for statement in engine.statements)
    }
    assert written == {
        "ratelimit.chat_requests_per_minute": '{"value":1200}',
        "ratelimit.max_concurrent_streams": '{"value":200}',
    }
    assert {key: decode_value(value) for key, value in written.items()} == dict(CHAT_LIMITS)
    assert engine.disposed


def test_set_ops_config_writes_booleans_and_numbers_as_values() -> None:
    engine = _RecordingEngine()

    set_ops_config(
        cast("SqlTestDatabase", SimpleNamespace(engine=lambda: engine)),
        {"memory.enabled": False, "chat.max_turn_seconds": 180},
    )

    assert [statement.compile().params["value_json"] for statement in engine.statements] == [
        '{"value":false}',
        '{"value":180}',
    ]


# T-564: the permissive limiters lift the burst and write limits; `real_limiters` restores them.


class _Members(_Rows):
    """`_Rows` that also answers the project check of a chat-path route: one active project
    whose member every caller is."""

    def __init__(self, project: UUID) -> None:
        super().__init__()
        self.project = project

    async def fetch_one(self, statement: object, parameters: Any = None, **kwargs: Any) -> Any:
        if kwargs.get("name") == "authorize_project":
            return SimpleNamespace(
                id=self.project,
                name="Travel",
                description="",
                instructions="",
                example_questions_json="[]",
                status="active",
                is_member=1,
            )
        return None


@dataclass
class _FixtureApp:
    built: BuiltApp
    clock: FakeClock
    conversations: FakeContainer


def _fixture_app(signing_keys: SigningKeys, *, real_limiters: bool) -> _FixtureApp:
    clock = FakeClock()
    containers = {
        definition.name: FakeContainer.of(definition, clock=clock)
        for definition in (CONVERSATIONS, MEMORIES, USAGE)
    }
    ports = replace(
        ports_under_test(
            sql_engine=cast("SqlEngine", _Members(uuid4())),
            clock=clock,
            real_limiters=real_limiters,
        ),
        container_wrapper=lambda container: cast("Any", containers[container.id]),
    )
    return _FixtureApp(
        built=build_app(ports=ports, signing_keys=signing_keys),
        clock=clock,
        conversations=containers[CONVERSATIONS.name],
    )


@pytest.fixture
def permissive_app(signing_keys: SigningKeys) -> _FixtureApp:
    return _fixture_app(signing_keys, real_limiters=False)


@pytest.fixture
def limited_app(signing_keys: SigningKeys) -> _FixtureApp:
    return _fixture_app(signing_keys, real_limiters=True)


async def test_t564_one_oid_gets_no_429_in_a_burst_from_the_permissive_app(
    permissive_app: _FixtureApp, alice: SharedPrincipal
) -> None:
    session_id = uuid4()
    session = session_document(user_id=alice.oid, session_id=session_id)
    turn = turn_document(user_id=alice.oid, session_id=session_id, project_id=session.project_id)
    permissive_app.conversations.put(session.to_item())
    permissive_app.conversations.put(turn.to_item())
    feedback = f"/v1/sessions/{session_id}/turns/{turn.id}/feedback"
    started = permissive_app.clock.monotonic()

    async with serving(permissive_app.built) as api:
        me = [
            (await api.client.get("/v1/me", headers=alice.authorization())).status_code
            for _ in range(100)
        ]
        rated = [
            (
                await api.client.put(feedback, json={"rating": "up"}, headers=alice.authorization())
            ).status_code
            for _ in range(150)
        ]

    # Every call falls inside one second of the app's clock, the limiters' window.
    assert permissive_app.clock.monotonic() - started < 1
    assert me == [200] * 100
    assert rated == [200] * 150
    burst = permissive_app.built.ports.burst_limiter
    writes = permissive_app.built.ports.write_limiter
    assert isinstance(burst, PermissiveBurstLimiter)
    assert isinstance(writes, PermissiveWriteRateLimiter)
    assert burst.checked == [alice.oid] * 250
    assert writes.checked == [(alice.oid, 120)] * 150


async def test_t564_the_real_limiters_refuse_the_41st_call_of_a_burst(
    limited_app: _FixtureApp, alice: SharedPrincipal
) -> None:
    async with serving(limited_app.built) as api:
        answers = [await api.client.get("/v1/me", headers=alice.authorization()) for _ in range(41)]

    assert [answer.status_code for answer in answers[:40]] == [200] * 40
    assert answers[40].status_code == 429
    assert answers[40].json()["code"] == "too_many_requests"


async def test_serve_live_runs_the_lifespan_and_serves_over_a_real_connection() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        events.append("started")
        yield
        events.append("stopped")

    app = FastAPI(lifespan=lifespan)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"answer": "pong"}

    async with serve_live(app) as base_url, httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get("/ping")
        assert events == ["started"]

    assert base_url.startswith("http://127.0.0.1:")
    assert response.json() == {"answer": "pong"}
    assert events == ["started", "stopped"]
