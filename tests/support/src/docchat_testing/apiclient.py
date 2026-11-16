"""`DeployedApiClient`: the one HTTP client of the deployed suites and `docchat-load`
(`84-testing.md` section 6.4 item 7; T-582).

Every call sends the caller's bearer token (fetched for each attempt, so a token re-acquired near
its expiry is used at once) and a fresh W3C `traceparent`. A `429` whose problem `code` is
`too_many_requests` or `write_rate_limited` is retried once after its `retryAfterSeconds`, waiting
at most 120 s; a second `429`, or a `429` with any other code (`rate_limited`, `too_many_streams`,
`token_quota_exceeded`, `too_many_downloads`, `upload_busy`), goes back to the test as its outcome.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

from docchat_core.clients.tls import TLS_CONTEXT
from docchat_testing.tokens import RopcRequest, suite_aliases

RETRIED_CODES: Final = frozenset({"too_many_requests", "write_rate_limited"})
MAX_RETRY_WAIT_SECONDS: Final = 120
HTTP_TOO_MANY_REQUESTS: Final = 429
DEFAULT_TIMEOUT: Final = httpx.Timeout(60.0, connect=5.0)
"""Connect 5 s as every tool client; 60 s between reads, longer than the chat stream's heartbeat
interval, so a quiet stream isn't cut."""

type TokenSource = Callable[[], Awaitable[str]]
type Sleep = Callable[[float], Awaitable[None]]


def new_traceparent() -> str:
    """A fresh sampled W3C `traceparent`: version 00, a random trace ID and span ID."""
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def retry_wait(response: httpx.Response) -> float | None:
    """The seconds to wait before the one retry of `response`, or `None` when it isn't retried:
    only a `429` problem with a retried code and a positive integer `retryAfterSeconds`."""
    if response.status_code != HTTP_TOO_MANY_REQUESTS:
        return None
    try:
        problem = response.json()
    except ValueError:
        return None
    if not isinstance(problem, dict) or problem.get("code") not in RETRIED_CODES:
        return None
    seconds = problem.get("retryAfterSeconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        return None
    return float(min(seconds, MAX_RETRY_WAIT_SECONDS))


class DeployedApiClient:
    """Calls a deployed API at `base_url` as the holder of `token` (`None`: anonymous)."""

    def __init__(
        self,
        base_url: str,
        token: TokenSource | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._token = token
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            verify=TLS_CONTEXT,
        )

    async def _headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        sent = {"traceparent": new_traceparent()}
        if self._token is not None:
            sent["Authorization"] = f"Bearer {await self._token()}"
        return sent | dict(headers or {})

    async def request(
        self, method: str, path: str, *, headers: Mapping[str, str] | None = None, **kwargs: Any
    ) -> httpx.Response:
        """One API call with at most one retry; `kwargs` are `httpx`'s (`json`, `content`,
        `params`, ...). A body passed as an async stream can't be sent twice, so stream uploads
        go through `stream`."""
        response = await self._client.request(
            method, path, headers=await self._headers(headers), **kwargs
        )
        wait = retry_wait(response)
        if wait is None:
            return response
        await self._sleep(wait)
        return await self._client.request(
            method, path, headers=await self._headers(headers), **kwargs
        )

    @asynccontextmanager
    async def stream(
        self, method: str, path: str, *, headers: Mapping[str, str] | None = None, **kwargs: Any
    ) -> AsyncIterator[httpx.Response]:
        """A streamed call (a chat turn, a citation download), retried the same way; the body of
        a `429` is read before the retry decision and the response is closed on exit."""
        for attempt in range(2):
            request = self._client.build_request(
                method, path, headers=await self._headers(headers), **kwargs
            )
            response = await self._client.send(request, stream=True)
            try:
                wait = None
                if attempt == 0 and response.status_code == HTTP_TOO_MANY_REQUESTS:
                    await response.aread()
                    wait = retry_wait(response)
                if wait is None:
                    yield response
                    return
            finally:
                await response.aclose()
            await self._sleep(wait)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


ENVIRONMENT_VARIABLES: Final = {
    "name": "AZURE_ENV_NAME",
    "tenant_id": "AZURE_TENANT_ID",
    "api_base_url": "API_BASE_URL",
    "web_base_url": "WEB_BASE_URL",
    "web_client_id": "WEB_CLIENT_ID",
    "test_client_id": "TEST_CLIENT_ID",
    "api_client_id": "API_CLIENT_ID",
    "api_scope": "API_SCOPE",
    "vault_uri": "DOCCHAT_TEST_VAULT_URI",
    "user_domain": "DOCCHAT_TEST_USER_DOMAIN",
}
"""What the token-holding job of `deployed-tests.yaml` passes: the `endpoints` job's outputs and
the variables of `dev-tests` or `load-tests` (`84-testing.md` section 6.3)."""


class EnvironmentIncompleteError(Exception):
    """A value the deployed suites need is missing from the job's environment."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DeployedEnvironment:
    """The deployed environment the token-holding job tests: base URLs, registration IDs and
    the test variables, none of them secret."""

    name: str
    tenant_id: str
    api_base_url: str
    web_base_url: str
    web_client_id: str
    test_client_id: str
    api_client_id: str
    api_scope: str
    vault_uri: str
    user_domain: str
    norole_skip_reason: str = ""

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> DeployedEnvironment:
        missing = [name for name in ENVIRONMENT_VARIABLES.values() if not environ.get(name)]
        if missing:
            raise EnvironmentIncompleteError(f"not set: {', '.join(missing)}")
        return cls(
            **{field: environ[name] for field, name in ENVIRONMENT_VARIABLES.items()},
            norole_skip_reason=environ.get("DOCCHAT_TEST_NOROLE_SKIP_REASON", ""),
        )

    @property
    def roles(self) -> dict[str, str]:
        """Suite role (`admin`, `member`, `outsider`, `norole`) to the alias it signs in as."""
        return suite_aliases(self.name, self.norole_skip_reason)

    def ropc_request(self, client_id: str | None = None) -> RopcRequest:
        """The password grant's target: the test client, or `client_id` for a check that
        expects another client to be refused."""
        return RopcRequest(
            tenant_id=self.tenant_id,
            client_id=self.test_client_id if client_id is None else client_id,
            api_scope=self.api_scope,
            user_domain=self.user_domain,
        )
