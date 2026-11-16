"""The deployed CORS preflight test T-024 (`84-testing.md` section 5.2; ADR 0004 item 7; SP-8 of
`90-build-order.md` section 2.10), in every `smoke` run and so in `full`.

App Service platform CORS answers preflights for the web client's origin before platform
authentication and the app (`11-api.md` section 1.10). Five cases: (1) a preflight of `GET /v1/me`
from the web origin without a token, (2) a preflight of `PATCH` on an administrator path with the
headers the web client sends, each answered `200` or `204` with `Access-Control-Allow-Origin` equal
to that origin; (3) a real call from that origin without a token gets `401`; (4) a preflight from
another origin gets no `Access-Control-Allow-Origin`; (5) a real call with a token gets `200`
carrying the header. Cases 1 and 2 failing is stop SC-4 for the product owner.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

import httpx
import pytest

from docchat_testing.apiclient import DeployedApiClient, DeployedEnvironment

pytestmark = [pytest.mark.deployed, pytest.mark.smoke, pytest.mark.full]

type ClientFactory = Callable[[str | None], DeployedApiClient]

PREFLIGHT_OK: Final = frozenset({200, 204})
HTTP_OK: Final = 200
HTTP_UNAUTHORIZED: Final = 401
OTHER_ORIGIN: Final = "https://not-allowed.example.invalid"
ALLOW_ORIGIN: Final = "access-control-allow-origin"


def web_origin(environment: DeployedEnvironment) -> str:
    """The web client's origin, `scheme://host[:port]` of `WEB_BASE_URL`."""
    parts = urlsplit(environment.web_base_url)
    return f"{parts.scheme}://{parts.netloc}"


def preflight(origin: str, method: str, headers: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    }


def assert_allowed(response: httpx.Response, origin: str, what: str) -> None:
    assert response.status_code in PREFLIGHT_OK, f"{what} answered {response.status_code}"
    assert response.headers.get(ALLOW_ORIGIN) == origin, what


async def test_t024_case_1_a_preflight_of_get_current_user_is_allowed(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory
) -> None:
    origin = web_origin(deployed_environment)
    response = await api_client(None).request(
        "OPTIONS", "/v1/me", headers=preflight(origin, "GET", "authorization,traceparent")
    )
    assert_allowed(response, origin, "OPTIONS /v1/me")


async def test_t024_case_2_a_preflight_of_an_admin_patch_is_allowed(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory
) -> None:
    origin = web_origin(deployed_environment)
    response = await api_client(None).request(
        "OPTIONS",
        f"/v1/admin/projects/{uuid.uuid4()}",
        headers=preflight(origin, "PATCH", "authorization,content-type,if-match,traceparent"),
    )
    assert_allowed(response, origin, "OPTIONS /v1/admin/projects/<id>")


async def test_t024_case_3_a_real_call_without_a_token_gets_401(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory
) -> None:
    response = await api_client(None).request(
        "GET", "/v1/me", headers={"Origin": web_origin(deployed_environment)}
    )
    assert response.status_code == HTTP_UNAUTHORIZED


async def test_t024_case_4_a_preflight_from_another_origin_is_not_allowed(
    api_client: ClientFactory,
) -> None:
    response = await api_client(None).request(
        "OPTIONS", "/v1/me", headers=preflight(OTHER_ORIGIN, "GET", "authorization,traceparent")
    )
    assert ALLOW_ORIGIN not in response.headers


async def test_t024_case_5_a_real_call_with_a_token_gets_200_with_the_header(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory
) -> None:
    origin = web_origin(deployed_environment)
    response = await api_client("member").request("GET", "/v1/me", headers={"Origin": origin})
    assert response.status_code == HTTP_OK
    assert response.headers.get(ALLOW_ORIGIN) == origin
