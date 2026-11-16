"""The request limits of every route: the per-user burst limit and the write window (`11-api.md`
section 1.11; `82-security.md` SEC-88, SEC-89 and SEC-93; ADR 0143).

- `BurstLimiter` (step 3a, every `/v1` route except `createChatTurn`): an in-process token bucket
  per user and worker process, 40 tokens refilled at 10 per second, the least recently used user
  evicted above 50,000. It runs in memory on the event loop, takes no lock (each worker serves its
  loop alone) and holds no access decision, so losing a bucket only loosens the flood guard.
- `WriteRateLimiter` (step 4a, the write-limited routes): a sliding minute of
  `api.write_requests_per_minute` per user, shared by every instance through the `wrate:{minute}`
  documents in the caller's partition of the Cosmos DB container `usage`. It fails closed: a window
  that can't be read or written refuses the request with `503 dependency_unavailable`.

`create_app` takes both as ports (`BurstLimiterPort`, `WriteLimiterPort`); the test suites pass
permissive implementations unless a limiter's answer is the test's subject. No setting disables a
limiter.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from uuid import UUID

from azure.core import MatchConditions
from azure.core.exceptions import AzureError
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from docchat_api.http import telemetry
from docchat_api.http.problems import DependencyUnavailable
from docchat_core.auth import Principal
from docchat_core.cosmos.usage import WriteRateWindow, write_rate_window_id
from docchat_core.errors import DocChatProblem
from docchat_core.models.clock import Clock, SystemClock

BURST_CAPACITY: Final = 40.0
BURST_REFILL_PER_SECOND: Final = 10.0
BURST_MAX_USERS: Final = 50_000

WRITE_ATTEMPTS: Final = 5
WRITE_READ_TIMEOUT_SECONDS: Final = 2
WRITE_WRITE_TIMEOUT_SECONDS: Final = 3
WINDOW_SECONDS: Final = 60
RETRY_AFTER_MAX_SECONDS: Final = 120


class TooManyRequests(DocChatProblem):
    """`429 too_many_requests` (`retryAfterSeconds` 1): the user's burst bucket is empty."""

    code = "too_many_requests"


class WriteRateLimited(DocChatProblem):
    """`429 write_rate_limited` (`retryAfterSeconds` 1 to 120): the user's write window is
    full."""

    code = "write_rate_limited"


class BurstLimiterPort(Protocol):
    """Step 3a: refuse the caller with `TooManyRequests`, or count the request."""

    def check(self, oid: UUID) -> None: ...


class WriteLimiterPort(Protocol):
    """Step 4a: refuse with `WriteRateLimited` or `DependencyUnavailable`, or count the write
    against `limit` (the request's `api.write_requests_per_minute`)."""

    async def check(self, principal: Principal, limit: int) -> None: ...


# --- The burst limit ------------------------------------------------------------------------


@dataclass(slots=True)
class _Bucket:
    tokens: float
    at: float


class BurstLimiter:
    """One per worker process (section 1.11 "Burst limit"); the three values are code constants,
    because a page load issues about 10 requests and the web client polls at most every 5 s."""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        capacity: float = BURST_CAPACITY,
        refill_per_second: float = BURST_REFILL_PER_SECOND,
        max_users: int = BURST_MAX_USERS,
    ) -> None:
        self._clock: Clock = SystemClock() if clock is None else clock
        self._capacity = capacity
        self._refill = refill_per_second
        self._max_users = max_users
        self._buckets: OrderedDict[UUID, _Bucket] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def __contains__(self, oid: object) -> bool:
        return oid in self._buckets

    def check(self, oid: UUID) -> None:
        now = self._clock.monotonic()
        bucket = self._buckets.get(oid)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, at=now)
            self._buckets[oid] = bucket
        self._buckets.move_to_end(oid)
        while len(self._buckets) > self._max_users:
            self._buckets.popitem(last=False)
        bucket.tokens = min(self._capacity, bucket.tokens + self._refill * (now - bucket.at))
        bucket.at = now
        if bucket.tokens < 1:
            telemetry.record_rejection("api")
            raise TooManyRequests(retry_after_seconds=1)
        bucket.tokens -= 1


# --- The write window -----------------------------------------------------------------------


class WindowStore(Protocol):
    """The calls the write window makes on the `usage` container (`azure.cosmos.aio`)."""

    async def read_item(
        self, item: str, partition_key: str, **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def patch_item(
        self, item: str, partition_key: str, patch_operations: list[dict[str, Any]], **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _Window:
    count: int
    etag: str | None


_EMPTY: Final = _Window(count=0, etag=None)


class _Limited(Exception):  # noqa: N818 - a control-flow signal, never raised past the limiter
    """The window is full; `seconds` is the wait the refusal states."""

    def __init__(self, seconds: int) -> None:
        super().__init__("write window full")
        self.seconds = seconds


def estimate(current: int, previous: int, fraction: float) -> float:
    """The sliding-minute estimate of section 1.11 for one more request: this minute's count
    plus one, plus last minute's count weighted by the part of the window it still covers."""
    return (current + 1) + previous * (1 - fraction)


def retry_after(now: float, current: int, previous: int, limit: int) -> int:
    """The smallest whole second from 1 to 120 after which, with no further requests, the
    estimate allows one more request, stepping one second at a time."""
    minute = math.floor(now / WINDOW_SECONDS)
    for wait in range(1, RETRY_AFTER_MAX_SECONDS + 1):
        later = now + wait
        later_minute = math.floor(later / WINDOW_SECONDS)
        fraction = (later - WINDOW_SECONDS * later_minute) / WINDOW_SECONDS
        if later_minute == minute:
            counts = (current, previous)
        elif later_minute == minute + 1:
            counts = (0, current)
        else:
            counts = (0, 0)
        if estimate(counts[0], counts[1], fraction) <= limit:
            return wait
    return RETRY_AFTER_MAX_SECONDS


class WriteRateLimiter:
    """The write window over the caller's `usage` partition; an allowed write costs two point
    reads and one patch or create."""

    def __init__(self, store: WindowStore, *, clock: Clock | None = None) -> None:
        self._store = store
        self._clock: Clock = SystemClock() if clock is None else clock

    async def check(self, principal: Principal, limit: int) -> None:
        attempts = 0
        with telemetry.write_check_span() as span:
            try:
                for attempts in range(1, WRITE_ATTEMPTS + 1):
                    if await self._attempt(principal, limit):
                        telemetry.record_write_check(span, "allowed", attempts)
                        return
            except _Limited as limited:
                telemetry.record_write_check(span, "limited", attempts)
                problem: DocChatProblem = WriteRateLimited(retry_after_seconds=limited.seconds)
            # Any failure after the SDK's own retries: a connection error, a timeout, a 429 or
            # a 5xx. The limit fails closed rather than let an unenforced flood through.
            except (AzureError, TimeoutError):
                telemetry.record_write_check(span, "unavailable", max(attempts, 1))
                problem = DependencyUnavailable()
            else:
                # Five conflicts in a row: other requests of this user keep winning the window.
                telemetry.record_write_check(span, "limited", WRITE_ATTEMPTS)
                problem = WriteRateLimited(retry_after_seconds=1)
        if isinstance(problem, WriteRateLimited):
            telemetry.record_rejection("write")
        raise problem

    async def _attempt(self, principal: Principal, limit: int) -> bool:
        """One read-decide-write round; `False` when another write won the window."""
        now = self._clock.now().timestamp()
        minute = math.floor(now / WINDOW_SECONDS)
        fraction = (now - WINDOW_SECONDS * minute) / WINDOW_SECONDS
        partition = principal.partition_key
        previous, current = await asyncio.gather(
            self._read(write_rate_window_id(minute - 1), partition),
            self._read(write_rate_window_id(minute), partition),
        )
        if estimate(current.count, previous.count, fraction) > limit:
            raise _Limited(retry_after(now, current.count, previous.count, limit))
        try:
            if current.etag is not None:
                await self._store.patch_item(
                    write_rate_window_id(minute),
                    partition,
                    [{"op": "incr", "path": "/count", "value": 1}],
                    etag=current.etag,
                    match_condition=MatchConditions.IfNotModified,
                    timeout=WRITE_WRITE_TIMEOUT_SECONDS,
                )
            else:
                window = WriteRateWindow(
                    id=write_rate_window_id(minute), user_id=principal.oid, count=1
                )
                await self._store.create_item(window.to_item(), timeout=WRITE_WRITE_TIMEOUT_SECONDS)
        except (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        ):
            # A 412 on the patch, a 409 on the create, or a window that expired in between.
            return False
        return True

    async def _read(self, window_id: str, partition: str) -> _Window:
        try:
            item = await self._store.read_item(
                window_id, partition, timeout=WRITE_READ_TIMEOUT_SECONDS
            )
        except CosmosResourceNotFoundError:
            return _EMPTY
        count = item.get("count")
        etag = item.get("_etag")
        return _Window(
            count=count if isinstance(count, int) and not isinstance(count, bool) else 0,
            etag=etag if isinstance(etag, str) else None,
        )
