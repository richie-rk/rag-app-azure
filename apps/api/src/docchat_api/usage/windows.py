"""The per-user request-rate windows: chat turns at P3 and cited-file downloads
(`12-chat-pipeline.md` sections 3.2, 3.6 and 16.5).

A two-bucket sliding window per user over the documents `rate:{epochMinute}` (chat) or
`dlrate:{epochMinute}` (downloads) in the caller's `usage` partition. At time `t` with `p` counted
requests in the previous minute and `c` in the current one, a request is allowed when
`(c + 1) + p * (1 - f) <= L`, `f` being the elapsed fraction of the current minute; an allowed
request increments `c` with an ETag-conditioned patch, or creates the minute's document. A conflict
re-reads and decides again, at most 5 times. A refused request is never counted, so a client that
keeps retrying isn't locked out longer. The estimate and the retry hint are the same computations
as the API's write window, so they are shared with it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from docchat_api.http.ratelimit import estimate, retry_after
from docchat_api.usage.container import UsageStore, etag_of
from docchat_api.usage.telemetry import RejectionReason, record_rejection
from docchat_core.cosmos.usage import (
    DownloadRateWindow,
    RateWindow,
    download_rate_window_id,
    rate_window_id,
)
from docchat_core.errors import DocChatProblem
from docchat_core.models.clock import Clock

WINDOW_SECONDS: Final = 60
WINDOW_ATTEMPTS: Final = 5
"""Bounds the ETag conflicts one user's concurrent requests can cause."""
CONFLICT_RETRY_AFTER_SECONDS: Final = 1


class RateLimited(DocChatProblem):
    """`429 rate_limited` with the computed `retryAfterSeconds` (1 to 120)."""

    code = "rate_limited"


@dataclass(frozen=True, slots=True)
class WindowKind:
    """One window's document type and the rejection reason it records."""

    model: type[RateWindow] | type[DownloadRateWindow]
    reason: RejectionReason

    def item_id(self, minute: int) -> str:
        if self.model is RateWindow:
            return rate_window_id(minute)
        return download_rate_window_id(minute)


CHAT_WINDOW: Final = WindowKind(RateWindow, "rate")
DOWNLOAD_WINDOW: Final = WindowKind(DownloadRateWindow, "download_rate")


def allows(*, previous: int, current: int, fraction: float, limit: int) -> bool:
    """The sliding-window decision for one more request."""
    return estimate(current, previous, fraction) <= limit


def window_retry_after(now: float, *, previous: int, current: int, limit: int) -> int:
    """The smallest whole second from 1 to 120 at which, with no further request, the window
    would allow one more request."""
    return retry_after(now, current, previous, limit)


def _count(item: Mapping[str, Any] | None) -> int:
    if item is None:
        return 0
    value = item.get("count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def count_request(
    store: UsageStore, oid: UUID, kind: WindowKind, limit: int, clock: Clock
) -> None:
    """Count one request against the window, or refuse it with `RateLimited`.

    Raises `UsageStoreUnavailable` when the store can't decide; the caller fails closed.
    """
    partition = str(oid)
    for _ in range(WINDOW_ATTEMPTS):
        now = clock.now().timestamp()
        minute = math.floor(now / WINDOW_SECONDS)
        fraction = (now - WINDOW_SECONDS * minute) / WINDOW_SECONDS
        previous_item, current_item = await store.read_pair(
            kind.item_id(minute - 1), kind.item_id(minute), partition
        )
        previous, current = _count(previous_item), _count(current_item)
        if not allows(previous=previous, current=current, fraction=fraction, limit=limit):
            record_rejection(kind.reason)
            raise RateLimited(
                retry_after_seconds=window_retry_after(
                    now, previous=previous, current=current, limit=limit
                )
            )
        etag = etag_of(current_item) if current_item is not None else None
        try:
            if etag is not None:
                await store.patch(
                    kind.item_id(minute),
                    partition,
                    [{"op": "incr", "path": "/count", "value": 1}],
                    etag=etag,
                )
            else:
                window = kind.model(id=kind.item_id(minute), user_id=oid, count=1)
                await store.create(window.to_item())
        except (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        ):
            # A 412 on the patch, a 409 on the create, or a window that expired in between:
            # another request of this user won the window; decide again on fresh counts.
            continue
        return
    record_rejection(kind.reason)
    raise RateLimited(retry_after_seconds=CONFLICT_RETRY_AFTER_SECONDS)
