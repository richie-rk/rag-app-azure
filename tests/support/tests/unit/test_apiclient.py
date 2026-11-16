from __future__ import annotations

import re

import httpx
import pytest
import respx

from docchat_testing.apiclient import (
    ENVIRONMENT_VARIABLES,
    MAX_RETRY_WAIT_SECONDS,
    DeployedApiClient,
    DeployedEnvironment,
    EnvironmentIncompleteError,
    new_traceparent,
    retry_wait,
)

BASE = "https://app-docchat-api-dev.example"
TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


class FakeClock:
    """Records the waits the client asks for instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)


def problem(code: str, retry_after: int | None = 3) -> httpx.Response:
    body: dict[str, object] = {
        "type": f"urn:docchat:problem:{code}",
        "title": "t",
        "status": 429,
        "detail": "d",
        "code": code,
        "correlationId": "0" * 31 + "1",
    }
    if retry_after is not None:
        body["retryAfterSeconds"] = retry_after
    return httpx.Response(429, json=body, headers={"Content-Type": "application/problem+json"})


async def token() -> str:
    return "token-1"


@pytest.mark.parametrize("code", ["too_many_requests", "write_rate_limited"])
@respx.mock
async def test_t582_retries_once_after_retry_after_seconds(code: str) -> None:
    clock = FakeClock()
    route = respx.get(f"{BASE}/v1/me").mock(
        side_effect=[problem(code, 7), httpx.Response(200, json={"isAdmin": False})]
    )
    async with DeployedApiClient(BASE, token, sleep=clock.sleep) as client:
        response = await client.request("GET", "/v1/me")
    assert response.status_code == 200
    assert route.call_count == 2
    assert clock.waits == [7.0]


@pytest.mark.parametrize("code", ["too_many_requests", "write_rate_limited"])
@respx.mock
async def test_t582_returns_the_second_429_unchanged(code: str) -> None:
    clock = FakeClock()
    second = problem(code, 9)
    route = respx.post(f"{BASE}/v1/chat/turns").mock(side_effect=[problem(code, 2), second])
    async with DeployedApiClient(BASE, token, sleep=clock.sleep) as client:
        response = await client.request("POST", "/v1/chat/turns", json={"q": 1})
    assert response.status_code == 429
    assert response.json()["code"] == code
    assert response.json()["retryAfterSeconds"] == 9
    assert route.call_count == 2
    assert clock.waits == [2.0]


@pytest.mark.parametrize(
    "code",
    [
        "rate_limited",
        "too_many_streams",
        "upload_busy",
        "token_quota_exceeded",
        "too_many_downloads",
    ],
)
@respx.mock
async def test_t582_never_retries_other_429_codes(code: str) -> None:
    clock = FakeClock()
    route = respx.get(f"{BASE}/v1/me").mock(side_effect=[problem(code), httpx.Response(200)])
    async with DeployedApiClient(BASE, token, sleep=clock.sleep) as client:
        response = await client.request("GET", "/v1/me")
    assert response.status_code == 429
    assert route.call_count == 1
    assert clock.waits == []


@respx.mock
async def test_t582_streamed_calls_follow_the_same_rule() -> None:
    clock = FakeClock()
    route = respx.post(f"{BASE}/v1/chat/turns").mock(
        side_effect=[problem("too_many_requests", 1), httpx.Response(200, text='{"type":"meta"}\n')]
    )
    client = DeployedApiClient(BASE, token, sleep=clock.sleep)
    async with client, client.stream("POST", "/v1/chat/turns", json={}) as response:
        body = await response.aread()
    assert body == b'{"type":"meta"}\n'
    assert route.call_count == 2
    assert clock.waits == [1.0]
    route.mock(side_effect=[problem("too_many_streams"), httpx.Response(200)])
    client = DeployedApiClient(BASE, token, sleep=clock.sleep)
    async with client, client.stream("POST", "/v1/chat/turns", json={}) as response:
        assert response.status_code == 429


@respx.mock
async def test_every_call_carries_the_token_and_a_fresh_traceparent() -> None:
    route = respx.get(f"{BASE}/v1/me").mock(
        side_effect=[problem("too_many_requests", 1), httpx.Response(200)]
    )
    async with DeployedApiClient(BASE, token, sleep=FakeClock().sleep) as client:
        await client.request("GET", "/v1/me", headers={"X-Extra": "1"})
    first, second = (call.request for call in route.calls)
    assert first.headers["Authorization"] == "Bearer token-1"
    assert first.headers["X-Extra"] == "1"
    assert TRACEPARENT.fullmatch(first.headers["traceparent"])
    assert first.headers["traceparent"] != second.headers["traceparent"]


@respx.mock
async def test_anonymous_client_sends_no_authorization() -> None:
    route = respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(401))
    async with DeployedApiClient(BASE) as client:
        response = await client.request("GET", "/healthz")
    assert response.status_code == 401
    assert "Authorization" not in route.calls.last.request.headers


def test_retry_wait_is_capped_and_needs_a_valid_problem() -> None:
    assert retry_wait(problem("too_many_requests", 600)) == MAX_RETRY_WAIT_SECONDS
    assert retry_wait(problem("too_many_requests", None)) is None
    assert retry_wait(problem("too_many_requests", 0)) is None
    assert retry_wait(httpx.Response(429, text="not json")) is None
    assert retry_wait(httpx.Response(429, json=["x"])) is None
    assert retry_wait(httpx.Response(503, json={"code": "too_many_requests"})) is None
    assert retry_wait(problem("write_rate_limited", True)) is None


def test_new_traceparent_is_w3c_shaped() -> None:
    assert TRACEPARENT.fullmatch(new_traceparent())
    assert new_traceparent() != new_traceparent()


JOB_ENVIRONMENT = {
    "AZURE_ENV_NAME": "dev",
    "AZURE_TENANT_ID": "tenant-1",
    "API_BASE_URL": BASE,
    "WEB_BASE_URL": "https://web.example",
    "WEB_CLIENT_ID": "web-1",
    "TEST_CLIENT_ID": "test-1",
    "API_CLIENT_ID": "api-1",
    "API_SCOPE": "api://tenant-1/docchat-dev-abc-api/access_as_user",
    "DOCCHAT_TEST_VAULT_URI": "https://kv-docchat-tst-abc123.vault.azure.net/",
    "DOCCHAT_TEST_USER_DOMAIN": "contoso.example",
}


def test_deployed_environment_reads_the_job_values() -> None:
    environment = DeployedEnvironment.from_environ(JOB_ENVIRONMENT)
    assert environment.api_client_id == "api-1"
    assert environment.roles == {
        "admin": "admin",
        "member": "member",
        "outsider": "outsider",
        "norole": "norole",
    }
    request = environment.ropc_request()
    assert (request.client_id, request.user_domain) == ("test-1", "contoso.example")
    assert environment.ropc_request("web-1").client_id == "web-1"
    skipped = DeployedEnvironment.from_environ(
        JOB_ENVIRONMENT | {"DOCCHAT_TEST_NOROLE_SKIP_REASON": "graph_default_access_refused"}
    )
    assert "norole" not in skipped.roles
    assert set(ENVIRONMENT_VARIABLES.values()) == set(JOB_ENVIRONMENT)


def test_deployed_environment_names_missing_values() -> None:
    incomplete = {k: v for k, v in JOB_ENVIRONMENT.items() if k != "API_CLIENT_ID"}
    with pytest.raises(EnvironmentIncompleteError, match="API_CLIENT_ID"):
        DeployedEnvironment.from_environ(incomplete | {"API_SCOPE": ""})
