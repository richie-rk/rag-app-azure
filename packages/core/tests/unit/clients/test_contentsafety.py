from __future__ import annotations

import httpx
import pytest

from docchat_core.clients.contentsafety import (
    CONTENTSAFETY_SCOPE,
    BearerTokenAuth,
    contentsafety_client,
)

_ENDPOINT = "https://cs-docchat-dev-x7.cognitiveservices.azure.com"


def test_the_token_audience() -> None:
    assert CONTENTSAFETY_SCOPE == "https://cognitiveservices.azure.com/.default"


async def test_the_client_options() -> None:
    client = contentsafety_client(_ENDPOINT, "token", timeout_seconds=7.0)
    try:
        assert str(client.base_url) == _ENDPOINT
        assert client.follow_redirects is False
        timeout = client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5.0, 7.0, 7.0, 7.0)
        assert isinstance(client.auth, BearerTokenAuth)
    finally:
        await client.aclose()


@pytest.mark.parametrize("from_provider", [True, False])
async def test_every_request_carries_the_bearer_token(from_provider: bool) -> None:
    calls = 0

    async def provider() -> str:
        nonlocal calls
        calls += 1
        return f"token-{calls}"

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    auth = BearerTokenAuth(provider if from_provider else "local-emulator")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=auth,
        timeout=httpx.Timeout(5.0),
        follow_redirects=False,
    ) as client:
        await client.post(_ENDPOINT + "/contentsafety/text:shieldPrompt", json={})
        await client.post(_ENDPOINT + "/contentsafety/text:shieldPrompt", json={})

    expected = (
        ["Bearer token-1", "Bearer token-2"] if from_provider else ["Bearer local-emulator"] * 2
    )
    assert seen == expected


def test_a_synchronous_client_is_refused() -> None:
    with pytest.raises(RuntimeError, match="async clients only"):
        next(BearerTokenAuth("token").sync_auth_flow(httpx.Request("GET", _ENDPOINT)))
