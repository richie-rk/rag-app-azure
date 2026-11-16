"""The signing-key providers (`11-api.md` section 1.2 "Signing keys"; `82-security.md` SEC-05;
ADR 0017 item 3).

`TokenValidator` asks a `SigningKeyProvider` for the public key of a token's `kid`. In every
deployed and local process that is `EntraSigningKeyProvider`, which fetches the tenant's v2 key
set from the one URL built from `DOCCHAT_AUTH_TENANT_ID`; the test suites pass a
`StaticSigningKeyProvider` holding their own key set through the same protocol. No setting
changes the key source.

The Entra provider keeps one key set per process. A set stays valid for 24 hours after the
fetch that produced it, so a failed hourly refresh keeps serving; with no valid set the API
answers `503 authentication_unavailable` and accepts no token. An unknown `kid` (an emergency key
roll) triggers at most one on-demand fetch every 300 s while the set is valid, every 30 s while
none is, and every caller that arrives during a fetch waits for that one fetch.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, Final, Protocol, override
from uuid import UUID

import httpx
import jwt

from docchat_core.auth import telemetry
from docchat_core.auth.exceptions import AuthenticationUnavailable, InvalidToken
from docchat_core.auth.settings import LOGIN_HOST
from docchat_core.auth.telemetry import FetchTrigger
from docchat_core.clients.tls import TLS_CONTEXT
from docchat_core.models.clock import Clock, SystemClock
from docchat_core.telemetry import qualified_type_name

ALGORITHM: Final = "RS256"
FETCH_TIMEOUT_SECONDS: Final = 8.0
CONNECT_TIMEOUT_SECONDS: Final = 4.0
MAX_CONNECTIONS: Final = 4
MAX_RESPONSE_BYTES: Final = 1_048_576
MAX_KEYS: Final = 100
KEY_SET_LIFETIME_SECONDS: Final = 24 * 3600
REFRESH_INTERVAL_SECONDS: Final = 3600
ON_DEMAND_GAP_SECONDS: Final = 300
ON_DEMAND_GAP_WITHOUT_KEYS_SECONDS: Final = 30
HTTP_OK: Final = 200

REFRESH_TASK_NAME: Final = "docchat-signing-keys"


def jwks_uri(tenant_id: UUID) -> str:
    """`https://login.microsoftonline.com/<tenant ID>/discovery/v2.0/keys`, the only URL the
    provider fetches."""
    return f"{LOGIN_HOST}/{tenant_id}/discovery/v2.0/keys"


class SigningKeyProvider(Protocol):
    """What `TokenValidator` needs: the public key of a `kid`. `start` and `aclose` bracket the
    process's lifetime (the API lifespan)."""

    async def get_key(self, kid: str) -> jwt.PyJWK:
        """The key for `kid`; `InvalidToken("unknown_kid")` when the set has none,
        `AuthenticationUnavailable` when no valid set exists."""
        ...

    def start(self) -> None: ...

    async def aclose(self) -> None: ...


class StaticSigningKeyProvider:
    """A fixed key set, the injectable provider of the test suites (`docchat_testing.tokens`)."""

    def __init__(self, keys: Mapping[str, jwt.PyJWK]) -> None:
        self._keys = MappingProxyType(dict(keys))

    async def get_key(self, kid: str) -> jwt.PyJWK:
        key = self._keys.get(kid)
        if key is None:
            raise InvalidToken("unknown_kid")
        return key

    def start(self) -> None:
        return

    async def aclose(self) -> None:
        return


# --- Key-set responses ---------------------------------------------------------------------


class KeySetRefusedError(Exception):
    """A response that isn't a usable key set; the type name is what telemetry records."""


class UnexpectedStatusError(KeySetRefusedError):
    """The response's status isn't `200`."""


class ResponseTooLargeError(KeySetRefusedError):
    """The response body exceeds 1 MiB."""


class NotAKeySetError(KeySetRefusedError):
    """The body isn't a JSON object with a `keys` array."""


class NoUsableKeyError(KeySetRefusedError):
    """The `keys` array holds no RSA signing key that parses."""


def _usable_entry(entry: object) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or entry.get("kty") != "RSA":
        return None
    kid = entry.get("kid")
    if not isinstance(kid, str) or not kid:
        return None
    use = entry.get("use")
    if use is not None and use != "sig":
        return None
    return entry


def parse_key_set(body: bytes) -> dict[str, jwt.PyJWK]:
    """The public keys of a key-set document by `kid`: each RSA key with a `kid` and `use`
    absent or `sig`, as an RS256 key; keys that fail to parse are skipped and the first 100
    usable keys are kept. Raises `NotAKeySetError` or `NoUsableKeyError`."""
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise NotAKeySetError from None
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise NotAKeySetError
    keys: dict[str, jwt.PyJWK] = {}
    for item in document["keys"]:
        if len(keys) >= MAX_KEYS:
            break
        entry = _usable_entry(item)
        if entry is None or entry["kid"] in keys:
            continue
        try:
            keys[entry["kid"]] = jwt.PyJWK(entry, algorithm=ALGORITHM)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            continue
    if not keys:
        raise NoUsableKeyError
    return keys


# --- The Entra provider ----------------------------------------------------------------------


class _Fetch:
    """What one fetch saw, for its span and its failure record."""

    def __init__(self) -> None:
        self.status_code: int | None = None


class EntraSigningKeyProvider:
    """The tenant's key set, fetched at start-up, every 3,600 s and on demand for an unknown
    `kid`. One per worker process: created in the lifespan, started without being awaited and
    closed at shutdown, which cancels the refresh task and closes the HTTP client."""

    def __init__(
        self,
        tenant_id: UUID,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = jwks_uri(tenant_id)
        self._clock: Clock = SystemClock() if clock is None else clock
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=MAX_CONNECTIONS),
            verify=TLS_CONTEXT,
        )
        self._keys: Mapping[str, jwt.PyJWK] | None = None
        self._fetched_at: float | None = None
        self._last_on_demand_at: float | None = None
        # One lock makes every fetch single flight: the start-up and hourly refreshes and the
        # on-demand fetches never overlap, and a caller arriving during one waits for it.
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._forget_age = telemetry.observe_key_age(self.age_seconds)

    @property
    def url(self) -> str:
        return self._url

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    def valid(self) -> bool:
        """A key set exists and is younger than 24 hours."""
        age = self.age_seconds()
        return age is not None and age < KEY_SET_LIFETIME_SECONDS

    def age_seconds(self) -> float | None:
        """Seconds since the fetch that produced the current set; `None` before the first."""
        if self._keys is None or self._fetched_at is None:
            return None
        return self._clock.monotonic() - self._fetched_at

    def _held(self, kid: str) -> jwt.PyJWK | None:
        if not self.valid() or self._keys is None:
            return None
        return self._keys.get(kid)

    def _on_demand_due(self) -> bool:
        gap = ON_DEMAND_GAP_SECONDS if self.valid() else ON_DEMAND_GAP_WITHOUT_KEYS_SECONDS
        last = self._last_on_demand_at
        return last is None or self._clock.monotonic() - last >= gap

    async def get_key(self, kid: str) -> jwt.PyJWK:
        key = self._held(kid)
        if key is not None:
            return key
        if self._lock.locked() or self._on_demand_due():
            async with self._lock:
                # The fetch this caller waited for may have brought the key, or used the gap.
                if self._held(kid) is None and self._on_demand_due():
                    self._last_on_demand_at = self._clock.monotonic()
                    await self._fetch_locked("unknown_kid")
        key = self._held(kid)
        if key is not None:
            return key
        if not self.valid():
            raise AuthenticationUnavailable()
        raise InvalidToken("unknown_kid")

    async def refresh(self, trigger: FetchTrigger) -> None:
        """One fetch under the lock; a failure keeps the current set and is reported."""
        async with self._lock:
            await self._fetch_locked(trigger)

    async def _fetch_locked(self, trigger: FetchTrigger) -> None:
        progress = _Fetch()
        with telemetry.fetch_span(trigger) as span:
            try:
                keys = await self._fetch(progress)
            except Exception as error:  # noqa: BLE001 - every failure keeps the last set
                telemetry.record_fetch(
                    span, trigger, "error", key_count=None, status_code=progress.status_code
                )
                telemetry.report_fetch_failure(
                    trigger, progress.status_code, qualified_type_name(type(error))
                )
                return
            self._keys = MappingProxyType(keys)
            self._fetched_at = self._clock.monotonic()
            telemetry.record_fetch(
                span, trigger, "ok", key_count=len(keys), status_code=progress.status_code
            )

    async def _fetch(self, progress: _Fetch) -> dict[str, jwt.PyJWK]:
        # The limit covers the connection, the headers and the whole body, so no fetch holds
        # the lock (and the callers waiting on it) longer than 8 s.
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            async with self._client.stream("GET", self._url) as response:
                progress.status_code = response.status_code
                if response.status_code != HTTP_OK:
                    raise UnexpectedStatusError
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise ResponseTooLargeError
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ResponseTooLargeError
        return parse_key_set(bytes(body))

    async def run(self) -> None:
        """The background task: one fetch at once, then one every 3,600 s."""
        await self.refresh("startup")
        while True:
            await self._sleep(REFRESH_INTERVAL_SECONDS)
            await self.refresh("scheduled")

    def start(self) -> None:
        """Start the refresh task without awaiting it, so start-up never waits for Entra."""
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=REFRESH_TASK_NAME)

    async def aclose(self) -> None:
        """Cancel the refresh task, stop the age gauge and close the HTTP client."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # `wait` doesn't re-raise the task's cancellation, so a cancelled shutdown still
            # propagates its own.
            await asyncio.wait({task})
        self._forget_age()
        await self._client.aclose()

    @override
    def __repr__(self) -> str:
        return f"EntraSigningKeyProvider(url={self._url!r}, valid={self.valid()})"
