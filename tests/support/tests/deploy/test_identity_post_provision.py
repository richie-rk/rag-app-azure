"""Unit tests of `scripts/identity_post_provision.py` (`83-infrastructure.md` section 7.5)."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
ADMIN = "47dc73a8-b97e-4be8-9481-614f6b17a11f"
USER = "6f46cf45-f1e3-4812-8003-c51ae0f5f98d"
DEFAULT = "00000000-0000-0000-0000-000000000000"
GROUPS = {name: str(uuid.uuid5(uuid.NAMESPACE_DNS, name)) for name in (
    "users", "admins", "first-admin", "test-users", "test-admins", "test-norole"
)}  # fmt: skip


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script("scripts/identity_post_provision.py", "docchat_script_identity_post")


class FakeGraph:
    """An in-memory tenant answering the Microsoft Graph calls of section 7.5."""

    def __init__(self, env: str) -> None:
        roles = ("web", "api", "test") if env != "prod" else ("web", "api")
        self.applications = {f"docchat-{env}-abc123-{r}": f"appid-{r}" for r in roles}
        self.principals = {f"appid-{r}": f"sp-{r}" for r in roles} | {GRAPH_APP_ID: "sp-graph"}
        self.grants: list[dict[str, str]] = []
        self.assignments: dict[str, list[dict[str, str]]] = {}
        self.refuse_api_default = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1.0")
        if request.method == "GET" and path.startswith("/applications(uniqueName="):
            name = path.split("'")[1]
            app_id = self.applications.get(name)
            if app_id is None:
                return httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}})
            return httpx.Response(200, json={"id": f"obj-{app_id}", "appId": app_id})
        if request.method == "GET" and path.startswith("/servicePrincipals(appId="):
            return httpx.Response(200, json={"id": self.principals[path.split("'")[1]]})
        if path.startswith("/oauth2PermissionGrants"):
            return self._grants(request, path)
        if path.endswith("/appRoleAssignedTo"):
            return self._assignments(request, path.split("/")[2])
        raise AssertionError(f"unexpected {request.method} {path}")

    def _grants(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "GET":
            query = request.url.params["$filter"]
            client = query.split("'")[1]
            found = [g for g in self.grants if g["clientId"] == client]
            return httpx.Response(200, json={"value": found})
        body = json.loads(request.content)
        if request.method == "POST":
            self.grants.append({"id": f"grant-{len(self.grants)}", **body})
            return httpx.Response(201, json=body)
        grant = next(g for g in self.grants if g["id"] == path.split("/")[2])
        grant.update(body)
        return httpx.Response(204)

    def _assignments(self, request: httpx.Request, resource: str) -> httpx.Response:
        items = self.assignments.setdefault(resource, [])
        if request.method == "GET":
            return httpx.Response(200, json={"value": items})
        body = json.loads(request.content)
        if self.refuse_api_default and resource == "sp-api" and body["appRoleId"] == DEFAULT:
            return httpx.Response(400, json={"error": {"code": "Request_BadRequest"}})
        items.append(body)
        return httpx.Response(201, json=body)

    def assigned(self, resource: str) -> set[tuple[str, str]]:
        return {(a["principalId"], a["appRoleId"]) for a in self.assignments.get(resource, [])}

    def writes(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if r.method != "GET"]


def _argv(env: str) -> list[str]:
    argv = [
        "--environment", env, "--resource-token", "abc123",
        "--users-group", GROUPS["users"], "--admins-group", GROUPS["admins"],
        "--admin", GROUPS["first-admin"],
    ]  # fmt: skip
    if env != "prod":
        argv += ["--test-users-group", GROUPS["test-users"]]
        argv += ["--test-admins-group", GROUPS["test-admins"]]
    if env == "dev":
        argv += ["--test-norole-group", GROUPS["test-norole"]]
    return argv


async def _run(graph: FakeGraph, env: str) -> Any:
    args = script.parse_args(_argv(env))
    with respx.mock(assert_all_called=False) as router:
        router.route(host="graph.microsoft.com").mock(side_effect=graph)
        async with httpx.AsyncClient(
            timeout=script.TIMEOUT, follow_redirects=False, verify=script.TLS_CONTEXT
        ) as client:
            return await script.run(args, "graph-token", client)


@pytest.mark.parametrize(
    "argv",
    [
        ["--environment", "prod", "--resource-token", "abc123", "--users-group", GROUPS["users"],
         "--admins-group", GROUPS["admins"], "--test-users-group", GROUPS["test-users"]],
        _argv("dev")[:-2],
        [*_argv("load"), "--test-norole-group", GROUPS["test-norole"]],
        _argv("load")[:-2],
        ["--environment", "dev", "--resource-token", "abc123", "--admins-group", GROUPS["admins"]],
        [*_argv("prod")[:5], "not-a-guid", *_argv("prod")[6:]],
        ["--environment", "test", *_argv("prod")[2:]],
    ],
    ids=[
        "test-group-in-prod", "dev-without-norole", "norole-in-load", "load-without-test-admins",
        "missing-users-group", "bad-object-id", "unknown-environment",
    ],
)  # fmt: skip
def test_a_refused_or_missing_argument_exits_1_before_any_graph_call(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    async def no_token() -> str:
        raise AssertionError("no Graph call may happen")

    monkeypatch.setattr(script, "graph_token", no_token)

    with pytest.raises(SystemExit) as raised:
        script.main(argv)
    assert raised.value.code == 1


async def test_dev_grants_consent_and_assigns_every_group() -> None:
    graph = FakeGraph("dev")

    summary = await _run(graph, "dev")

    assert {(g["clientId"], g["resourceId"], g["scope"]) for g in graph.grants} == {
        ("sp-web", "sp-graph", "openid profile offline_access"),
        ("sp-test", "sp-graph", "openid profile"),
    }
    assert all(g["consentType"] == "AllPrincipals" for g in graph.grants)
    assert graph.assigned("sp-api") == {
        (GROUPS["users"], USER),
        (GROUPS["admins"], ADMIN),
        (GROUPS["first-admin"], ADMIN),
        (GROUPS["test-users"], USER),
        (GROUPS["test-admins"], ADMIN),
        (GROUPS["test-norole"], DEFAULT),
    }
    assert graph.assigned("sp-web") == {
        (GROUPS[name], DEFAULT)
        for name in ("users", "admins", "first-admin", "test-users", "test-admins", "test-norole")
    }
    assert graph.assigned("sp-test") == {
        (GROUPS[name], DEFAULT) for name in ("test-users", "test-admins", "test-norole")
    }
    assert summary.refused == []
    request = graph.requests[0]
    assert request.headers["Authorization"] == "Bearer graph-token"
    assert request.headers["client-request-id"] == summary.correlation_id


async def test_prod_touches_no_test_client() -> None:
    graph = FakeGraph("prod")

    await _run(graph, "prod")

    assert [g["clientId"] for g in graph.grants] == ["sp-web"]
    assert graph.assigned("sp-api") == {
        (GROUPS["users"], USER),
        (GROUPS["admins"], ADMIN),
        (GROUPS["first-admin"], ADMIN),
    }
    assert "sp-test" not in graph.assignments


async def test_t030_a_second_run_changes_no_grant_and_no_assignment() -> None:
    # The logic of the post-provision idempotency test; its deployed run in dev is person-run
    # by a tenant administrator and recorded in the stage-0 record (84-testing.md section 6.3).
    graph = FakeGraph("dev")
    await _run(graph, "dev")
    writes = len(graph.writes())

    summary = await _run(graph, "dev")

    assert len(graph.writes()) == writes
    assert summary.grants_created == []
    assert summary.grants_updated == []
    assert summary.assignments_created == []
    assert len(summary.assignments_existing) == 15


async def test_an_existing_grant_is_patched_to_exactly_its_scope_set() -> None:
    graph = FakeGraph("load")
    graph.grants = [
        {"id": "g1", "clientId": "sp-web", "consentType": "AllPrincipals",
         "resourceId": "sp-graph", "scope": "openid User.Read"},
        {"id": "g2", "clientId": "sp-test", "consentType": "AllPrincipals",
         "resourceId": "sp-graph", "scope": "profile openid"},
    ]  # fmt: skip

    summary = await _run(graph, "load")

    assert graph.grants[0]["scope"] == "openid profile offline_access"
    assert graph.grants[1]["scope"] == "profile openid"
    assert summary.grants_updated == ["web client: openid profile offline_access"]
    assert summary.grants_unchanged == ["test client: openid profile"]


async def test_a_refused_default_access_assignment_completes_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = FakeGraph("dev")
    graph.refuse_api_default = True

    async def token() -> str:
        return "graph-token"

    monkeypatch.setattr(script, "graph_token", token)

    with respx.mock(assert_all_called=False) as router:
        router.route(host="graph.microsoft.com").mock(side_effect=graph)
        status = await script.main_async(script.parse_args(_argv("dev")))

    assert status == 2
    assert (GROUPS["test-norole"], DEFAULT) in graph.assigned("sp-test")
    assert (GROUPS["test-norole"], DEFAULT) not in graph.assigned("sp-api")
    printed = json.loads(capsys.readouterr().out)
    assert printed["refused"] == ["no-role group default access on the API"]
    uuid.UUID(printed["correlationId"])


async def test_a_missing_registration_fails_with_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph("dev")
    graph.applications.pop("docchat-dev-abc123-test")

    async def token() -> str:
        return "graph-token"

    monkeypatch.setattr(script, "graph_token", token)

    with respx.mock(assert_all_called=False) as router:
        router.route(host="graph.microsoft.com").mock(side_effect=graph)
        status = await script.main_async(script.parse_args(_argv("dev")))

    assert status == 1
    assert graph.writes() == []
