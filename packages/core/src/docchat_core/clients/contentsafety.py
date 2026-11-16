"""The Prompt Shields client: one `httpx.AsyncClient` per API process (`12-chat-pipeline.md`
sections 1.3, 8 and 8.8; `83-infrastructure.md` section 6.2; ADR 0130, ADR 0150).

The GA Content Safety SDK has no Prompt Shields operation, so the screening module posts
`/contentsafety/text:shieldPrompt` through this client. Its base URL is
`DOCCHAT_CONTENTSAFETY_ENDPOINT`, it never follows a redirect, and every request carries a bearer
token for `https://cognitiveservices.azure.com/.default` from the process credential.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from typing import Final, override

import httpx

from docchat_core.clients.tls import TLS_CONTEXT

CONTENTSAFETY_SCOPE: Final = "https://cognitiveservices.azure.com/.default"
CONTENTSAFETY_CONNECT_TIMEOUT_SECONDS: Final = 5.0

type BearerToken = str | Callable[[], Awaitable[str]]
"""A bearer-token provider, or a constant token for the local model stub."""


class BearerTokenAuth(httpx.Auth):
    """Sets `Authorization: Bearer <token>` on every request of an async client. The provider
    caches the token and renews it before it expires."""

    def __init__(self, token: BearerToken) -> None:
        self._token = token

    @override
    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = self._token if isinstance(self._token, str) else await self._token()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    @override
    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        # The token provider is async; a synchronous client never exists in first-party code.
        raise RuntimeError("BearerTokenAuth serves async clients only")


def contentsafety_client(
    endpoint: str, token: BearerToken, *, timeout_seconds: float
) -> httpx.AsyncClient:
    """5 s to connect and `DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS` for the rest of each call."""
    return httpx.AsyncClient(
        base_url=endpoint,
        auth=BearerTokenAuth(token),
        timeout=httpx.Timeout(timeout_seconds, connect=CONTENTSAFETY_CONNECT_TIMEOUT_SECONDS),
        follow_redirects=False,
        verify=TLS_CONTEXT,
    )
