from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any, Protocol
from uuid import uuid4

import pytest
from fastapi import APIRouter, Query
from fastapi.dependencies.models import Dependant
from opentelemetry.sdk.trace import ReadableSpan
from pydantic import BaseModel

from docchat_api.app import AREA_ROUTERS, Ports
from docchat_api.http.access import access
from docchat_api.http.models import QueryModel
from docchat_api.http.ratelimit import BurstLimiter
from docchat_api.http.routing import (
    DEFAULT_ROUTE_OPTIONS,
    DocChatRoute,
    PrincipalMissingError,
    RouteOptions,
    current_principal,
    options_of,
    route_options,
)
from docchat_core.sql.repositories.users import ProvisionResult
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.limiters import PermissiveWriteRateLimiter
from docchat_testing.fixtures.api import ApiUnderTest, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

ROUTES_DEVIATING = frozenset(
    {
        "createChatTurn",
        "adminPreflightUploads",
        "adminReserveUpload",
        "adminUploadContent",
        "adminSubmitUploads",
    }
)
"""The five routes of `11-api.md` section 1.7's table that deviate from the default options."""


class Captured(Protocol):
    def server_spans(self) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


class FakeSql(Protocol):
    provision: ProvisionResult

    def names(self) -> list[str]: ...


# --- Route options -------------------------------------------------------------------------


def test_the_default_options() -> None:
    assert (
        RouteOptions(
            body_limit=65_536,
            content_types=("application/json",),
            burst_limited=True,
            write_limited=None,
        )
        == DEFAULT_ROUTE_OPTIONS
    )


@pytest.mark.parametrize(
    ("methods", "write_limited"),
    [(["GET"], False), (["POST"], True), (["PUT"], True), (["PATCH"], True), (["DELETE"], True)],
)
def test_the_write_limit_follows_the_method(methods: list[str], write_limited: bool) -> None:
    assert DEFAULT_ROUTE_OPTIONS.for_methods(methods).write_limited is write_limited


def test_a_declared_write_limit_wins() -> None:
    assert RouteOptions(write_limited=False).for_methods(["POST"]).write_limited is False


@pytest.mark.parametrize(
    "options",
    [
        {"body_limit": 0},
        {"content_types": ()},
        {"content_types": ("Application/JSON",)},
        {"content_types": ("application/json; charset=utf-8",)},
    ],
)
def test_route_options_refuse_nonsense(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"body limit|content types"):
        RouteOptions(**options)


def test_route_options_mark_the_endpoint() -> None:
    @route_options(body_limit=None, content_types=["application/octet-stream"], write_limited=False)
    async def endpoint() -> None:
        return

    assert options_of(endpoint) == RouteOptions(
        body_limit=None, content_types=("application/octet-stream",), write_limited=False
    )
    assert options_of(test_the_default_options) is DEFAULT_ROUTE_OPTIONS


def _routes() -> list[DocChatRoute]:
    return [
        route
        for router in AREA_ROUTERS
        for route in router.routes
        if isinstance(route, DocChatRoute)
    ]


def _input_models(dependant: Dependant) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    pending = [dependant]
    while pending:
        current = pending.pop()
        for field in (*current.query_params, *current.body_params):
            annotation = field.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                found.append(annotation)
        pending.extend(current.dependencies)
    return found


def test_every_input_model_forbids_unknown_members() -> None:
    for route in _routes():
        for model in _input_models(route.dependant):
            assert model.model_config.get("extra") == "forbid", (route.operation_id, model)


def test_t052_only_the_five_routes_deviate_from_the_default_options() -> None:
    for route in _routes():
        defaults = DEFAULT_ROUTE_OPTIONS.for_methods(sorted(route.methods or ()))
        if route.operation_id not in ROUTES_DEVIATING:
            assert route.route_options == defaults, route.operation_id


def test_t052_every_write_route_but_the_five_is_write_limited() -> None:
    for route in _routes():
        writes = bool((route.methods or set()) & {"POST", "PUT", "PATCH", "DELETE"})
        expected = writes and route.operation_id not in ROUTES_DEVIATING
        assert route.route_options.write_limited is expected, route.operation_id


# --- The route class ------------------------------------------------------------------------


def test_a_v1_endpoint_must_declare_its_access() -> None:
    router = APIRouter(route_class=DocChatRoute)

    async def endpoint() -> None:
        return

    with pytest.raises(TypeError, match="declares access"):
        router.add_api_route("/x", endpoint, operation_id="x")
    with pytest.raises(TypeError, match="declares access"):
        router.add_api_route("/x", access("public")(endpoint), operation_id="x")


def test_a_v1_endpoint_must_have_an_operation_id() -> None:
    router = APIRouter(route_class=DocChatRoute)

    @access("user")
    async def endpoint() -> None:
        return

    with pytest.raises(TypeError, match="operationId"):
        router.add_api_route("/x", endpoint)


class _Query(QueryModel):
    project_id: str | None = None


def test_a_scoped_parameter_must_be_a_parameter() -> None:
    router = APIRouter(route_class=DocChatRoute)

    @access("user", scoped_params=["projectId", "body:/items"])
    async def known(query: Annotated[_Query, Query()]) -> None:
        return

    @access("user", scoped_params=["indexId"])
    async def unknown() -> None:
        return

    router.add_api_route("/known", known, operation_id="known")
    with pytest.raises(TypeError, match="isn't a parameter"):
        router.add_api_route("/unknown", unknown, operation_id="unknown")


def test_the_route_publishes_its_extensions_and_if_match() -> None:
    router = APIRouter(route_class=DocChatRoute)

    @access("admin", if_match=True, error_codes=["project_not_found"])
    async def endpoint(project_id: str) -> None:
        return

    router.add_api_route(
        "/admin/projects/{project_id}",
        endpoint,
        methods=["DELETE"],
        operation_id="adminRemoveThing",
        openapi_extra={"x-other": 1},
    )
    route = router.routes[0]

    assert isinstance(route, DocChatRoute)
    assert route.openapi_extra is not None
    assert route.openapi_extra["x-docchat-access"] == "admin"
    assert route.openapi_extra["x-other"] == 1
    assert route.openapi_extra["parameters"][0]["name"] == "If-Match"
    assert {"admin_required", "write_rate_limited", "precondition_required"} <= set(
        route.openapi_extra["x-docchat-error-codes"]
    )


def test_current_principal_needs_the_route_class() -> None:
    with pytest.raises(PrincipalMissingError):
        current_principal(SimpleNamespace(state=SimpleNamespace()))  # type: ignore[arg-type]


# --- The order of checks --------------------------------------------------------------------


@pytest.mark.parametrize(
    "body", [b"x" * 70_000, b"{not json", b'{"name": 1}'], ids=["oversized", "malformed", "invalid"]
)
async def test_t048_no_token_and_a_bad_body_answers_401(
    started: ApiUnderTest, fake_sql: FakeSql, body: bytes
) -> None:
    response = await started.client.post(
        f"/v1/probe/items/{uuid4()}", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert "ensure_user" not in fake_sql.names()


async def test_t048_no_token_and_an_unsupported_media_type_answers_401(
    started: ApiUnderTest,
) -> None:
    response = await started.client.post(
        f"/v1/probe/items/{uuid4()}", content=b"x", headers={"Content-Type": "text/plain"}
    )

    assert response.status_code == 401


async def test_a_token_without_a_role_is_role_not_assigned_and_a_denial(
    started: ApiUnderTest, erin: SharedPrincipal, captured: Captured
) -> None:
    response = await started.client.get("/v1/me", headers=erin.authorization())

    assert (response.status_code, response.json()["code"]) == (403, "role_not_assigned")
    assert captured.points("docchat.access.denials") == {
        (("kind", "role_not_assigned"), ("operation", "getCurrentUser")): 1
    }


@pytest.mark.parametrize(
    "body",
    [b"{not json", b"x" * 70_000, b'{"name": ""}'],
    ids=["malformed", "oversized", "invalid"],
)
async def test_a_user_on_an_admin_route_is_refused_before_any_validation(
    started: ApiUnderTest,
    alice: SharedPrincipal,
    fake_sql: FakeSql,
    captured: Captured,
    body: bytes,
) -> None:
    response = await started.client.post(
        "/v1/admin/probe",
        content=body,
        headers=alice.authorization() | {"Content-Type": "application/json"},
    )

    assert (response.status_code, response.json()["code"]) == (403, "admin_required")
    assert "ensure_user" not in fake_sql.names()
    assert captured.points("docchat.access.denials") == {
        (("kind", "admin_required"), ("operation", "probeAdmin")): 1
    }


async def test_the_server_span_names_the_caller_and_the_client(
    started: ApiUnderTest, alice: SharedPrincipal, captured: Captured
) -> None:
    await started.client.get("/v1/me", headers=alice.authorization())

    (server,) = captured.server_spans()
    assert server.attributes is not None
    assert server.attributes["enduser.id"] == str(alice.oid)
    assert server.attributes["docchat.auth.client"] == "web"


async def test_t006_a_forged_platform_principal_header_does_not_change_the_caller(
    started: ApiUnderTest, alice: SharedPrincipal, carol: SharedPrincipal
) -> None:
    forged = {
        "X-MS-CLIENT-PRINCIPAL-ID": str(carol.oid),
        "X-MS-CLIENT-PRINCIPAL-NAME": carol.email,
        "X-MS-CLIENT-PRINCIPAL": "eyJhdXRoX3R5cCI6ImFhZCJ9",
    }

    me = await started.client.get("/v1/me", headers=alice.authorization() | forged)
    anonymous = await started.client.get("/v1/me", headers=forged)

    assert me.status_code == 200
    assert me.json()["userId"] == str(alice.oid)
    assert me.json()["isAdmin"] is False
    assert anonymous.status_code == 401


async def test_provisioning_runs_after_the_checks_and_marks_the_audit_action(
    started: ApiUnderTest, dave: SharedPrincipal, fake_sql: FakeSql, captured: Captured
) -> None:
    fake_sql.provision = ProvisionResult(provisioned=True, default_project_count=2)

    response = await started.client.get("/v1/me", headers=dave.authorization())

    assert response.status_code == 200
    assert fake_sql.names().count("ensure_user") == 1
    (server,) = captured.server_spans()
    assert server.attributes is not None
    assert server.attributes["docchat.audit.action"] == "user.provisioned"


async def test_a_write_route_consults_the_write_limiter_with_the_snapshot_limit(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.post(
        f"/v1/probe/items/{uuid4()}",
        json={"name": "n", "projectId": str(uuid4())},
        headers=alice.authorization(),
    )

    assert response.status_code == 200
    write_limiter: Any = started.ports.write_limiter
    assert write_limiter.checked == [(alice.oid, 120)]


async def test_a_route_without_the_burst_limit_skips_it(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    burst: Any = started.ports.burst_limiter
    await started.client.post(
        "/v1/probe/chat",
        json={"name": "n", "projectId": str(uuid4())},
        headers=alice.authorization(),
    )
    await started.client.get("/v1/me", headers=alice.authorization())

    assert burst.checked == [alice.oid]


async def test_t016_a_burst_refused_request_touches_no_store(
    fake_sql: Any,
    signing_keys: SigningKeys,
    alice: SharedPrincipal,
    captured: Captured,
    probes: list[APIRouter],
) -> None:
    clock = FakeClock()
    writes = PermissiveWriteRateLimiter()
    ports = Ports(
        burst_limiter=BurstLimiter(clock), write_limiter=writes, sql_engine=fake_sql, clock=clock
    )
    body = {"name": "n", "projectId": str(uuid4())}
    async with running_api(ports=ports, signing_keys=signing_keys, routers=probes) as api:
        allowed = [
            await api.client.post(
                f"/v1/probe/items/{uuid4()}", json=body, headers=alice.authorization()
            )
            for _ in range(40)
        ]
        calls = len(fake_sql.calls)
        refused = await api.client.post(
            f"/v1/probe/items/{uuid4()}", json=body, headers=alice.authorization()
        )

    assert {response.status_code for response in allowed} == {200}
    assert refused.status_code == 429
    assert refused.json()["code"] == "too_many_requests"
    assert refused.headers["retry-after"] == "1"
    assert len(writes.checked) == 40
    assert len(fake_sql.calls) == calls
