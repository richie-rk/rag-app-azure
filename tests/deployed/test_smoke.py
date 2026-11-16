"""The deployed `smoke` suite (`84-testing.md` section 6.5): rows of this file run after every
`dev` deploy and after load seeding, in the token-holding `pytest` job, and again in the `full`
suite, which holds `smoke`.

Platform authentication answers before the app, so its `401` and `403` carry no problem body;
the app's own answers behind it are problems whose `correlationId` is the caller's trace ID
(`11-api.md` sections 1.2, 1.5, 1.6 and 1.10).

T-029 runs here as the registration properties the test identity can observe without Microsoft
Graph, which `id-docchat-citest` has no right to read (`83-infrastructure.md` section 6.8): every
alias's token, issued through the test client, and the web client's refusal of the password
grant. The property-by-property Graph comparison of the three registrations is T-029's row in the
infrastructure suite, `test_infrastructure.py`, run by the `dev` deploy identity.

The trace lookup check T-494 (`full` only) runs `docchat-ops trace show` on a smoke request's
trace ID, and T-022's health reading queries `HealthCheckStatus`. Both read the environment's
workspace through Log Analytics Reader, which the test identity holds on `log-docchat-dev` in
`dev` and at `rg-docchat-load` in `load` (`83-infrastructure.md` section 6.8; the product owner's
grant of 2026-11-06), so both run in either environment. `docchat-ops` takes the workspace only
from an azd project's `.azure/<env>/.env`, which the token-holding job doesn't have: T-494 writes
one holding the workspace ID the `endpoints` job published (`deployed-tests.yaml`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import httpx
import pytest

from docchat_testing.adminapi import problem_code
from docchat_testing.apiclient import DeployedApiClient, DeployedEnvironment
from docchat_testing.logquery import query_logs
from docchat_testing.seed import SeedData
from docchat_testing.tokens import ALIAS_ROLES, API_SCOPE_NAME, DeployedTokens, RopcError

pytestmark = pytest.mark.deployed

type ClientFactory = Callable[[str | None], DeployedApiClient]

CONFIDENTIAL_CLIENT_REFUSAL: Final = 7000218
"""AADSTS7000218: a client without public client flows must send a secret or an assertion, so
the password grant through it fails before any user is checked."""

HTTP_OK: Final = 200
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_NOT_FOUND: Final = 404
PROBLEM_TYPE: Final = "application/problem+json"
WORKSPACE_VARIABLE: Final = "DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"
HEALTHY_PERCENT: Final = 100.0

V1_OPERATIONS: Final = (
    ("GET", "/v1/me"),
    ("GET", "/v1/projects"),
    ("POST", "/v1/chat/turns"),
    ("GET", "/v1/sessions"),
    ("GET", "/v1/sessions/{id}"),
    ("GET", "/v1/sessions/{id}/turns"),
    ("PUT", "/v1/sessions/{id}/turns/{id}/feedback"),
    ("GET", "/v1/sessions/{id}/turns/{id}/citations/1"),
    ("GET", "/v1/sessions/{id}/turns/{id}/citations/1/content"),
    ("GET", "/v1/sessions/{id}/turns/{id}/citations/1/document"),
    ("GET", "/v1/memory"),
    ("PATCH", "/v1/memory"),
    ("PUT", "/v1/memory/facts/{id}"),
    ("DELETE", "/v1/memory/facts/{id}"),
    ("DELETE", "/v1/memory/facts"),
    ("GET", "/v1/admin/projects"),
    ("POST", "/v1/admin/projects"),
    ("GET", "/v1/admin/projects/{id}"),
    ("PATCH", "/v1/admin/projects/{id}"),
    ("GET", "/v1/admin/projects/{id}/members"),
    ("PUT", "/v1/admin/projects/{id}/members/{id}"),
    ("DELETE", "/v1/admin/projects/{id}/members/{id}"),
    ("GET", "/v1/admin/users"),
    ("GET", "/v1/admin/projects/{id}/indexes"),
    ("PUT", "/v1/admin/projects/{id}/indexes/{id}"),
    ("DELETE", "/v1/admin/projects/{id}/indexes/{id}"),
    ("GET", "/v1/admin/index-options"),
    ("GET", "/v1/admin/indexes"),
    ("POST", "/v1/admin/indexes"),
    ("GET", "/v1/admin/indexes/{id}"),
    ("PATCH", "/v1/admin/indexes/{id}"),
    ("DELETE", "/v1/admin/indexes/{id}"),
    ("POST", "/v1/admin/indexes/{id}/reingest"),
    ("GET", "/v1/admin/indexes/{id}/documents"),
    ("GET", "/v1/admin/indexes/{id}/documents/{id}"),
    ("GET", "/v1/admin/indexes/{id}/documents/{id}/pdf"),
    ("DELETE", "/v1/admin/indexes/{id}/documents/{id}"),
    ("POST", "/v1/admin/indexes/{id}/documents/{id}/reingest"),
    ("POST", "/v1/admin/indexes/{id}/uploads/preflight"),
    ("POST", "/v1/admin/indexes/{id}/uploads"),
    ("PUT", "/v1/admin/indexes/{id}/uploads/{id}/content"),
    ("POST", "/v1/admin/indexes/{id}/uploads/submit"),
    ("GET", "/v1/admin/ingestion-runs"),
    ("GET", "/v1/admin/ingestion-runs/{id}"),
)
"""The 44 `/v1` operations of `11-api.md` section 1.1; each `{id}` becomes a fresh UUID."""

EVERY_RESPONSE_HEADERS: Final = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "strict-transport-security": "max-age=31536000",
}
JSON_CSP: Final = "default-src 'none'; frame-ancestors 'none'"


@pytest.mark.smoke
@pytest.mark.full
async def test_t029_registration_properties_seen_by_the_test_client(
    deployed_environment: DeployedEnvironment, deployed_tokens: DeployedTokens
) -> None:
    environment = deployed_environment
    scope = re.fullmatch(
        rf"api://{re.escape(environment.tenant_id)}/docchat-{re.escape(environment.name)}"
        rf"-[a-z0-9]+-api/{API_SCOPE_NAME}",
        environment.api_scope,
    )
    assert scope is not None, "the API scope isn't the identifier URI of section 7.2"
    for role, alias in environment.roles.items():
        claims = deployed_tokens.claims(alias)
        # Version 2 tokens, issued to the pre-authorised test client without a consent prompt.
        assert claims["ver"] == "2.0", role
        assert claims["iss"] == f"https://login.microsoftonline.com/{environment.tenant_id}/v2.0"
        assert claims["tid"] == environment.tenant_id, role
        assert claims["aud"] == environment.api_client_id, role
        assert claims["azp"] == environment.test_client_id, role
        assert API_SCOPE_NAME in str(claims["scp"]).split(), role
        expected = ALIAS_ROLES[alias]
        assert sorted(claims.get("roles") or []) == sorted(expected or []), role

    web_client = environment.ropc_request(environment.web_client_id)
    with pytest.raises(RopcError) as refused:
        await deployed_tokens.acquire_with(environment.roles["member"], web_client)
    assert refused.value.aadsts == CONFIDENTIAL_CLIENT_REFUSAL, "public client flows are on"


def concrete(path: str) -> str:
    return path.replace("{id}", str(uuid.uuid4()))


def assert_platform_answer(response: httpx.Response, status: int, what: str) -> None:
    """A platform answer: the status, `WWW-Authenticate: Bearer` on a `401`, no problem body."""
    assert response.status_code == status, f"{what} answered {response.status_code}"
    assert PROBLEM_TYPE not in response.headers.get("content-type", ""), what
    if status == HTTP_UNAUTHORIZED:
        assert response.headers.get("www-authenticate", "").startswith("Bearer"), what


@pytest.mark.smoke
@pytest.mark.full
async def test_t022_anonymous_requests_get_the_platform_401(api_client: ClientFactory) -> None:
    anonymous = api_client(None)
    health = await anonymous.request("GET", "/healthz")
    assert_platform_answer(health, HTTP_UNAUTHORIZED, "anonymous GET /healthz")
    for method, path in V1_OPERATIONS:
        response = await anonymous.request(method, concrete(path))
        assert_platform_answer(response, HTTP_UNAUTHORIZED, f"anonymous {method} {path}")


@pytest.mark.smoke
@pytest.mark.full
async def test_t022_a_token_of_another_audience_gets_the_platform_401(
    deployed_environment: DeployedEnvironment, deployed_tokens: DeployedTokens
) -> None:
    # The password grant's ID token is a valid token of the tenant whose audience is the test
    # client, not the API.
    environment = deployed_environment
    id_token = await deployed_tokens.acquire_with(
        environment.roles["member"], environment.ropc_request(), returning="id_token"
    )

    async def foreign_audience() -> str:
        return id_token

    async with DeployedApiClient(environment.api_base_url, foreign_audience) as client:
        response = await client.request("GET", "/v1/me")
    assert_platform_answer(response, HTTP_UNAUTHORIZED, "a token for the test client")


def api_site_name(environment: DeployedEnvironment) -> str:
    """The API site's name, the first label of its default host name (`API_BASE_URL`)."""
    host = urlsplit(environment.api_base_url).hostname or ""
    return host.split(".", 1)[0]


@pytest.mark.smoke
@pytest.mark.full
async def test_t022_the_health_check_reports_every_instance_healthy(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory
) -> None:
    # With a token the platform lets the request through to the app's own health route.
    health = await api_client("member").request("GET", "/healthz")
    assert health.status_code == HTTP_OK
    assert health.content == b""
    # Every instance's reading, not only the one that answered: the platform's HealthCheckStatus
    # in the workspace, which id-docchat-citest reads through Log Analytics Reader on
    # log-docchat-dev in dev and at rg-docchat-load in load (83-infrastructure.md section 6.8).
    kql = (
        f'AzureMetrics | where TimeGenerated > ago(15m) and Resource =~ "'
        f'{api_site_name(deployed_environment)}" and MetricName == "HealthCheckStatus"'
        " | summarize lowest = min(Minimum), readings = count()"
    )
    rows = await query_logs(os.environ[WORKSPACE_VARIABLE], kql, "PT15M")
    assert rows, "the HealthCheckStatus query answered no row"
    assert rows[0]["readings"], "no HealthCheckStatus reading in the last 15 minutes"
    assert float(rows[0]["lowest"]) == HEALTHY_PERCENT


def new_traceparent() -> tuple[str, str]:
    trace_id = secrets.token_hex(16)
    return trace_id, f"00-{trace_id}-{secrets.token_hex(8)}-01"


@pytest.mark.smoke
@pytest.mark.full
async def test_t025_the_client_trace_id_comes_back_as_the_correlation_id(
    api_client: ClientFactory,
) -> None:
    trace_id, traceparent = new_traceparent()
    response = await api_client("member").request(
        "GET", "/v1/admin/projects", headers={"traceparent": traceparent}
    )
    assert response.status_code == HTTP_FORBIDDEN
    assert response.headers["content-type"].startswith(PROBLEM_TYPE)
    assert response.json()["correlationId"] == trace_id


@pytest.mark.smoke
@pytest.mark.full
async def test_t026_an_outsider_gets_project_not_found_on_the_test_project(
    api_client: ClientFactory, seed_data: SeedData
) -> None:
    body = {
        "projectId": seed_data.project_id,
        "sessionId": None,
        "clientTurnId": str(uuid.uuid4()),
        "question": "Which tables does the test document hold?",
    }
    async with api_client("outsider").stream("POST", "/v1/chat/turns", json=body) as response:
        await response.aread()
    assert response.status_code == HTTP_NOT_FOUND
    assert problem_code(response) == "project_not_found"


@pytest.mark.smoke
@pytest.mark.full
async def test_t026_a_member_gets_admin_required_on_an_admin_route(
    api_client: ClientFactory,
) -> None:
    response = await api_client("member").request("GET", "/v1/admin/projects")
    assert response.status_code == HTTP_FORBIDDEN
    assert problem_code(response) == "admin_required"


@pytest.mark.smoke
@pytest.mark.full
async def test_t026_get_current_user_shows_the_admin_as_admin(api_client: ClientFactory) -> None:
    response = await api_client("admin").request("GET", "/v1/me")
    assert response.status_code == HTTP_OK
    me = response.json()
    assert me["isAdmin"] is True
    assert "Admin" in me["roles"]


@pytest.mark.smoke
@pytest.mark.full
async def test_t026_a_user_token_gets_its_current_user(
    deployed_environment: DeployedEnvironment,
    deployed_tokens: DeployedTokens,
    api_client: ClientFactory,
) -> None:
    response = await api_client("member").request("GET", "/v1/me")
    assert response.status_code == HTTP_OK
    me = response.json()
    claims = deployed_tokens.claims(deployed_environment.roles["member"])
    assert me["userId"] == claims["oid"]
    assert me["roles"] == ["User"]
    assert me["isAdmin"] is False


@pytest.mark.smoke
@pytest.mark.full
async def test_t027_a_token_without_roles_gets_role_not_assigned(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory
) -> None:
    if deployed_environment.name == "load":
        pytest.skip("the load set has no user without a role (84-testing.md section 6.5)")
    if deployed_environment.norole_skip_reason:
        pytest.skip(deployed_environment.norole_skip_reason)
    response = await api_client("norole").request("GET", "/v1/me")
    assert response.status_code == HTTP_FORBIDDEN
    assert problem_code(response) == "role_not_assigned"


TRACE_POLL_SECONDS: Final = 60.0
TRACE_WAIT_SECONDS: Final = 13 * 60
"""Records reach the workspace minutes after the request (`85-observability.md` section 2)."""
TRACE_FOUND: Final = 0
TRACE_NOT_FOUND: Final = 4
TRACE_COMMAND_TIMEOUT_SECONDS: Final = 120.0


async def trace_show(environment: str, trace_id: str, project: Path) -> tuple[int, bytes]:
    """`docchat-ops --env <environment> --json trace show <trace ID>` run from `project`."""
    command = shutil.which("docchat-ops")
    assert command is not None, "the docchat-ops console script isn't installed"
    process = await asyncio.create_subprocess_exec(
        command, "--env", environment, "--json", "trace", "show", trace_id,
        cwd=project, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    async with asyncio.timeout(TRACE_COMMAND_TIMEOUT_SECONDS):
        output, _ = await process.communicate()
    return int(process.returncode or 0), output


@pytest.mark.full
async def test_t494_trace_show_lists_the_spans_of_a_smoke_request(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory, tmp_path: Path
) -> None:
    environment = deployed_environment.name
    trace_id, traceparent = new_traceparent()
    response = await api_client("member").request(
        "GET", "/v1/me", headers={"traceparent": traceparent}
    )
    assert response.status_code == HTTP_OK
    # docchat-ops takes endpoints only from an azd project's .azure/<env>/.env, which this job
    # (unlike the operator's machine) doesn't have; trace show reads one value from it.
    (tmp_path / "azure.yaml").write_text("name: docchat\n", encoding="utf-8")
    values = tmp_path / ".azure" / environment
    values.mkdir(parents=True)
    workspace = os.environ[WORKSPACE_VARIABLE]
    (values / ".env").write_text(f'{WORKSPACE_VARIABLE}="{workspace}"\n', encoding="utf-8")

    deadline = asyncio.get_running_loop().time() + TRACE_WAIT_SECONDS
    while True:
        code, output = await trace_show(environment, trace_id, tmp_path)
        if code != TRACE_NOT_FOUND or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(TRACE_POLL_SECONDS)
    assert code == TRACE_FOUND, f"trace show exited {code}"
    report = json.loads(output)
    assert report["traceId"] == trace_id
    requests = [
        record
        for record in report["records"]
        if record["type"] == "AppRequests" and record["roleName"] == "docchat-api"
    ]
    assert requests, "trace show lists no request span of docchat-api"


def assert_api_headers(response: httpx.Response, what: str, *, json_body: bool) -> None:
    for name, value in EVERY_RESPONSE_HEADERS.items():
        assert response.headers.get(name) == value, f"{what}: {name}"
    if json_body:
        assert response.headers.get("content-security-policy") == JSON_CSP, what
    for absent in ("server", "etag", "location", "content-encoding"):
        assert absent not in response.headers, f"{what} carries {absent}"


@pytest.mark.smoke
@pytest.mark.full
async def test_t059_health_and_problem_answers_carry_the_api_headers(
    api_client: ClientFactory,
) -> None:
    member = api_client("member")
    health = await member.request("GET", "/healthz")
    assert health.status_code == HTTP_OK
    assert_api_headers(health, "getHealth", json_body=False)
    refused = await member.request("GET", "/v1/admin/projects")
    assert refused.status_code == HTTP_FORBIDDEN
    assert refused.headers["content-type"].startswith(PROBLEM_TYPE)
    assert_api_headers(refused, "a problem", json_body=True)
