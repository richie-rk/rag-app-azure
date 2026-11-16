"""The `prod` smoke suite T-588 (`84-testing.md` sections 5.13 and 6.5), marker `prod_smoke`.

Job `prod-smoke` of `deployed-tests.yaml` runs it after every `prod` deploy (`deploy.yaml` job
`test-prod`): `uv run pytest tests/deployed --deployed -m prod_smoke`. `prod` has no test client
and no test user (`83-infrastructure.md` section 7.4), so these are anonymous checks only, in a job
with no GitHub environment and no OIDC token, reading only `API_BASE_URL` and `WEB_BASE_URL` (the
`deploy-prod` job's public outputs). An anonymous `GET /healthz` is answered by platform
authentication with `401` and `WWW-Authenticate: Bearer` and no problem body (section 2.4: the
platform's answers carry none, the API's own `401` does), and the CORS preflight from the web
client's origin is answered by platform CORS with a `2xx` naming that origin (ADR 0004).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Final
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from docchat_testing.apiclient import DeployedApiClient

pytestmark = [pytest.mark.deployed, pytest.mark.prod_smoke]

HTTP_UNAUTHORIZED: Final = 401
ALLOW_ORIGIN: Final = "access-control-allow-origin"
PROBLEM_JSON: Final = "application/problem+json"
OIDC_REQUEST_VARIABLES: Final = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")


def public_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.startswith("https://"):
        pytest.fail(f"{name} isn't set to an https URL; prod-smoke passes it", pytrace=False)
    return value.rstrip("/")


@pytest.fixture(scope="session")
def web_origin() -> str:
    """The web client's origin, `scheme://host[:port]` of `WEB_BASE_URL`."""
    parts = urlsplit(public_value("WEB_BASE_URL"))
    return f"{parts.scheme}://{parts.netloc}"


@pytest_asyncio.fixture
async def anonymous() -> AsyncIterator[DeployedApiClient]:
    """A client of the `prod` API holding no token."""
    async with DeployedApiClient(public_value("API_BASE_URL")) as client:
        yield client


def test_t588_the_suite_holds_no_oidc_token() -> None:
    # prod-smoke grants only contents: read, so GitHub hands it no way to mint a token.
    assert [name for name in OIDC_REQUEST_VARIABLES if name in os.environ] == []


async def test_t588_anonymous_healthz_gets_401_from_platform_authentication(
    anonymous: DeployedApiClient,
) -> None:
    response = await anonymous.request("GET", "/healthz")

    assert response.status_code == HTTP_UNAUTHORIZED
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")
    assert PROBLEM_JSON not in response.headers.get("content-type", "")


async def test_t588_the_cors_preflight_from_the_web_origin_gets_2xx(
    anonymous: DeployedApiClient, web_origin: str
) -> None:
    response = await anonymous.request(
        "OPTIONS",
        "/v1/me",
        headers={
            "Origin": web_origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,traceparent",
        },
    )

    assert 200 <= response.status_code < 300, f"the preflight answered {response.status_code}"
    assert response.headers.get(ALLOW_ORIGIN) == web_origin
