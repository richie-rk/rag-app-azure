"""The worker's usage service and the package's two entry points `record_model_call` and
`check_token_quota` (`12-chat-pipeline.md` sections 1.3, 2.2, 3, 15.7 and 16.5;
`13-conversations-and-memory.md` section 6.4 rule 5).

`UsageService` is built once per worker by the lifespan over the `usage` container and installed
as the process's service, so the conversation store's after-response work reaches the same
counters through the two module functions. The route uses its methods for the checks before the
response: the request rate (P3), the token quota (P4) and the stream lease (P5), and the download
window and lease of the citation files. Each check fails closed: a store that can't decide refuses
the request with `503 dependency_unavailable` (`retryAfterSeconds` 5).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ClassVar, Protocol, Self
from uuid import UUID

from docchat_api.http.problems import DependencyUnavailable
from docchat_api.usage.container import UsageContainer, UsageStore, UsageStoreUnavailable
from docchat_api.usage.leases import (
    DOWNLOAD_LEASE,
    DOWNLOAD_LEASE_TTL_SECONDS,
    STREAM_LEASE,
    Lease,
    acquire_lease,
    stream_lease_ttl,
)
from docchat_api.usage.quotas import QuotaCheck, TokenQuotaExceeded, evaluate_quota
from docchat_api.usage.recorder import Recorder
from docchat_api.usage.tasks import PendingTasks
from docchat_api.usage.telemetry import record_rejection
from docchat_api.usage.windows import CHAT_WINDOW, DOWNLOAD_WINDOW, count_request
from docchat_core.cosmos.conversations import ModelCallUsage
from docchat_core.models.clock import Clock, SystemClock
from docchat_core.opsconfig import ChatOps, OpsConfig, QuotaOps, RateLimitOps

type Sleep = Callable[[float], Awaitable[None]]


class OpsSource(Protocol):
    """Where the background gate takes its quota values: the worker's `OpsConfigStore`."""

    async def snapshot(self) -> OpsConfig: ...


class UsageService:
    """The usage limits, counters and recorder of one worker process."""

    _current: ClassVar[UsageService | None] = None

    def __init__(
        self,
        container: UsageContainer,
        *,
        ops: OpsSource,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._store = UsageStore(container)
        self._ops = ops
        self._clock: Clock = SystemClock() if clock is None else clock
        self._tasks = PendingTasks()
        self._recorder = Recorder(container, clock=self._clock, tasks=self._tasks, sleep=sleep)

    # The process's service -------------------------------------------------------------------

    def install(self) -> Self:
        """Make this the process's service, which the module functions use."""
        if UsageService._current is not None:
            raise RuntimeError("the process's usage service is already installed")
        UsageService._current = self
        return self

    @classmethod
    def current(cls) -> UsageService:
        service = cls._current
        if service is None:
            raise RuntimeError("no usage service is installed")
        return service

    async def aclose(self) -> None:
        """Wait for the running releases and counter writes, then uninstall."""
        await self._tasks.drain()
        if UsageService._current is self:
            UsageService._current = None

    # Before the response ---------------------------------------------------------------------

    async def check_chat_rate(self, oid: UUID, ratelimit: RateLimitOps) -> None:
        """P3: count the request against `ratelimit.chat_requests_per_minute`, or raise
        `RateLimited` (`429`) or `DependencyUnavailable` (`503`)."""
        try:
            await count_request(
                self._store, oid, CHAT_WINDOW, ratelimit.chat_requests_per_minute, self._clock
            )
        except UsageStoreUnavailable as exc:
            raise DependencyUnavailable() from exc

    async def check_quota(self, oid: UUID, quota: QuotaOps) -> None:
        """P4: raise `TokenQuotaExceeded` (`429`) when the day or month quota is reached, or
        `DependencyUnavailable` (`503`) when the counters can't be read."""
        try:
            check = await evaluate_quota(self._store, oid, quota, self._clock.now())
        except UsageStoreUnavailable as exc:
            raise DependencyUnavailable() from exc
        if not check.allowed:
            record_rejection("quota_month" if check.exceeded == "month" else "quota_day")
            raise TokenQuotaExceeded(retry_after_seconds=check.retry_after_seconds)

    async def acquire_stream_lease(
        self,
        oid: UUID,
        *,
        session_id: UUID,
        turn_id: UUID,
        ratelimit: RateLimitOps,
        chat: ChatOps,
    ) -> Lease:
        """P5: hold one of `ratelimit.max_concurrent_streams` slots for `chat.max_turn_seconds`
        + 30 s, or raise `TooManyStreams` (`429`) or `DependencyUnavailable` (`503`). The caller
        releases the lease at `citations`, in the finaliser, or before a later refusal."""
        try:
            return await acquire_lease(
                self._store,
                self._tasks,
                oid,
                STREAM_LEASE,
                slots=ratelimit.max_concurrent_streams,
                ttl_seconds=stream_lease_ttl(chat.max_turn_seconds),
                session_id=session_id,
                turn_id=turn_id,
                clock=self._clock,
            )
        except UsageStoreUnavailable as exc:
            raise DependencyUnavailable() from exc

    async def check_download_rate(self, oid: UUID, ratelimit: RateLimitOps) -> None:
        """The download window: count the request against
        `ratelimit.citation_downloads_per_minute`, or raise `RateLimited` or
        `DependencyUnavailable`."""
        try:
            await count_request(
                self._store,
                oid,
                DOWNLOAD_WINDOW,
                ratelimit.citation_downloads_per_minute,
                self._clock,
            )
        except UsageStoreUnavailable as exc:
            raise DependencyUnavailable() from exc

    async def acquire_download_lease(
        self,
        oid: UUID,
        *,
        session_id: UUID | None,
        turn_id: UUID | None,
        ratelimit: RateLimitOps,
    ) -> Lease:
        """The download lease: one of `ratelimit.max_concurrent_downloads` slots for 300 s, or
        `TooManyDownloads` or `DependencyUnavailable`. `session_id` and `turn_id` name the cited
        turn, both `None` for an `adminGetDocumentPdf` download."""
        try:
            return await acquire_lease(
                self._store,
                self._tasks,
                oid,
                DOWNLOAD_LEASE,
                slots=ratelimit.max_concurrent_downloads,
                ttl_seconds=DOWNLOAD_LEASE_TTL_SECONDS,
                session_id=session_id,
                turn_id=turn_id,
                clock=self._clock,
            )
        except UsageStoreUnavailable as exc:
            raise DependencyUnavailable() from exc

    # After the response and in the background -------------------------------------------------

    async def token_quota(self, oid: UUID) -> QuotaCheck:
        """The background gate over the current ops-config snapshot; raises
        `UsageStoreUnavailable` when the counters can't be read."""
        snapshot = await self._ops.snapshot()
        return await evaluate_quota(self._store, oid, snapshot.quota, self._clock.now())

    def record(
        self,
        oid: UUID,
        record: ModelCallUsage,
        *,
        session_id: UUID | None = None,
        turn_id: UUID | None = None,
    ) -> Awaitable[None]:
        """The recorder: metrics now, the two counter writes as a shielded task."""
        return self._recorder.record(oid, record, session_id=session_id, turn_id=turn_id)


def record_model_call(
    oid: UUID,
    record: ModelCallUsage,
    *,
    session_id: UUID | None = None,
    turn_id: UUID | None = None,
) -> Awaitable[None]:
    """The one entry point for every usage record: emits the record's metrics and starts its day
    and month counter writes as shielded tasks, returning an awaitable that completes when both
    have ended. It appends nothing to a turn. `session_id` and `turn_id` only label the failure
    record when the writes fail."""
    return UsageService.current().record(oid, record, session_id=session_id, turn_id=turn_id)


async def check_token_quota(oid: UUID) -> QuotaCheck:
    """The background quota gate of the conversation store's summary and memory calls; raises
    `UsageStoreUnavailable` when the counters can't be read (two timeouts or a failure), and the
    caller defers its work without a model call."""
    return await UsageService.current().token_quota(oid)
