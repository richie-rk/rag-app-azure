"""Concurrency leases: stream leases at P5 and citation-download leases (`12-chat-pipeline.md`
sections 3.3 and 16.5).

Each running stream holds one item `lease:{slot}` (slot 1 to `ratelimit.max_concurrent_streams`),
each running download one item `dllease:{slot}` (slot 1 to `ratelimit.max_concurrent_downloads`),
in the caller's `usage` partition. Acquisition tries the slots in order: a create wins a free slot;
a `409` reads the slot's item and takes it over with an ETag-conditioned replace when it has
expired; a live lease, a lost replace or an item that vanished moves on to the next slot. With every
slot live the request is refused with `retryAfterSeconds` 10. The lease is released by an
ETag-conditioned delete, shielded from cancellation, in one attempt: a `404`, a `412` or a timeout
is ignored because the item's TTL ends it, so a crashed instance strands a slot for at most the
lease's lifetime and a release with an old ETag never removes a newer holder's lease.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from azure.core import MatchConditions
from azure.core.exceptions import AzureError
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from docchat_api.usage.container import (
    WRITE_TIMEOUT_SECONDS,
    UsageContainer,
    UsageStore,
    etag_of,
    required_etag,
)
from docchat_api.usage.tasks import PendingTasks
from docchat_api.usage.telemetry import RejectionReason, record_rejection
from docchat_core.cosmos.usage import (
    DOWNLOAD_LEASE_SECONDS,
    STREAM_LEASE_OFFSET_SECONDS,
    DownloadLease,
    StreamLease,
    download_lease_id,
    stream_lease_id,
)
from docchat_core.errors import DocChatProblem
from docchat_core.models.clock import Clock

LEASE_RETRY_AFTER_SECONDS: Final = 10
"""A stream or a download usually ends within it."""
DOWNLOAD_LEASE_TTL_SECONDS: Final = DOWNLOAD_LEASE_SECONDS
"""A download lease lives 300 s, the download's duration cap."""


class TooManyStreams(DocChatProblem):
    """`429 too_many_streams` (`retryAfterSeconds` 10): every stream slot of the user is live."""

    code = "too_many_streams"


class TooManyDownloads(DocChatProblem):
    """`429 too_many_downloads` (`retryAfterSeconds` 10): every download slot is live."""

    code = "too_many_downloads"


@dataclass(frozen=True, slots=True)
class LeaseKind:
    """One lease type: its item IDs, its document, its refusal and its rejection reason."""

    item_id: Callable[[int], str]
    model: type[StreamLease] | type[DownloadLease]
    refusal: type[TooManyStreams] | type[TooManyDownloads]
    reason: RejectionReason


STREAM_LEASE: Final = LeaseKind(stream_lease_id, StreamLease, TooManyStreams, "concurrency")
DOWNLOAD_LEASE: Final = LeaseKind(
    download_lease_id, DownloadLease, TooManyDownloads, "download_concurrency"
)


@dataclass(slots=True)
class Lease:
    """A held lease: its slot and the ETag the holder wrote, which alone may delete it."""

    oid: UUID
    slot: int
    item_id: str
    etag: str
    _container: UsageContainer = field(repr=False)
    _tasks: PendingTasks = field(repr=False)
    _release: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def released(self) -> bool:
        """Whether the release has started."""
        return self._release is not None

    async def release(self) -> None:
        """Delete the lease conditioned on its ETag, once. The delete runs as its own task, so a
        cancelled caller still frees the slot; a second call waits for the first delete."""
        if self._release is None:
            self._release = self._tasks.spawn(self._delete())
        await asyncio.shield(self._release)

    async def _delete(self) -> None:
        try:
            await self._container.delete_item(
                self.item_id,
                str(self.oid),
                etag=self.etag,
                match_condition=MatchConditions.IfNotModified,
                timeout=WRITE_TIMEOUT_SECONDS,
            )
        # A 404, a 412 or a timeout leaves the item to its TTL, as does any other failure of the
        # one attempt: the release happens on a refusal or after the stream, where nothing can be
        # done about it but wait for the item to expire.
        except (AzureError, TimeoutError):
            return


def _expires_at(item: Mapping[str, Any]) -> datetime | None:
    value = item.get("expiresAt")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


async def acquire_lease(
    store: UsageStore,
    tasks: PendingTasks,
    oid: UUID,
    kind: LeaseKind,
    *,
    slots: int,
    ttl_seconds: int,
    session_id: UUID | None,
    turn_id: UUID | None,
    clock: Clock,
) -> Lease:
    """Hold one of the user's `slots` slots, or refuse with the kind's `429`.

    Raises `UsageStoreUnavailable` when the store can't decide; the caller fails closed.
    """
    partition = str(oid)
    acquired_at = clock.now()
    for slot in range(1, slots + 1):
        item_id = kind.item_id(slot)
        document = kind.model.model_validate(
            {
                "id": item_id,
                "user_id": oid,
                "turn_id": turn_id,
                "session_id": session_id,
                "acquired_at": acquired_at,
                "expires_at": acquired_at + timedelta(seconds=ttl_seconds),
                "ttl": ttl_seconds,
            }
        ).to_item()
        try:
            written = await store.create(document)
        except CosmosResourceExistsError:
            existing = await store.read(item_id, partition)
            if existing is None:
                continue
            expires_at = _expires_at(existing)
            etag = etag_of(existing)
            # An item whose expiry can't be read is treated as live: its TTL still ends it.
            if expires_at is None or etag is None or expires_at >= acquired_at:
                continue
            try:
                written = await store.replace(item_id, document, etag=etag)
            except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError):
                continue
        return Lease(
            oid=oid,
            slot=slot,
            item_id=item_id,
            etag=required_etag(written),
            _container=store.container,
            _tasks=tasks,
        )
    record_rejection(kind.reason)
    raise kind.refusal(retry_after_seconds=LEASE_RETRY_AFTER_SECONDS)


def stream_lease_ttl(max_turn_seconds: int) -> int:
    """A stream lease lives `chat.max_turn_seconds` + 30 s, the turn deadline's offset, so it
    never expires while its session is still blocked."""
    return max_turn_seconds + STREAM_LEASE_OFFSET_SECONDS
