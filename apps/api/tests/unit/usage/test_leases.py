from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from azure.core.exceptions import ServiceRequestError

from docchat_api.usage.container import UsageStore, UsageStoreUnavailable
from docchat_api.usage.leases import (
    DOWNLOAD_LEASE,
    STREAM_LEASE,
    Lease,
    TooManyDownloads,
    TooManyStreams,
    acquire_lease,
    stream_lease_ttl,
)
from docchat_api.usage.tasks import PendingTasks
from docchat_core.cosmos.containers import USAGE
from docchat_core.models import format_utc
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer

USER = UUID("0a11ce00-0000-4000-8000-000000000001")
START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
TTL = 210


class Captured(Protocol):
    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def container(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(USAGE, clock=clock)


async def _acquire(
    container: FakeContainer, clock: FakeClock, tasks: PendingTasks, slots: int = 2
) -> Lease:
    return await acquire_lease(
        UsageStore(container),
        tasks,
        USER,
        STREAM_LEASE,
        slots=slots,
        ttl_seconds=TTL,
        session_id=uuid4(),
        turn_id=uuid4(),
        clock=clock,
    )


def test_a_stream_lease_lives_the_turn_cap_plus_30_s() -> None:
    assert stream_lease_ttl(180) == TTL


async def test_leases_take_the_slots_in_order(container: FakeContainer, clock: FakeClock) -> None:
    tasks = PendingTasks()
    first = await _acquire(container, clock, tasks)
    second = await _acquire(container, clock, tasks)

    assert (first.slot, second.slot) == (1, 2)
    stored = container.get(str(USER), "lease:1")
    assert stored is not None
    assert stored["type"] == "stream_lease"
    assert stored["ttl"] == TTL
    assert stored["acquiredAt"] == format_utc(START)
    assert stored["expiresAt"] == format_utc(START + timedelta(seconds=TTL))
    assert first.etag == stored["_etag"]


async def test_every_live_slot_refuses_with_10_s(
    container: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    tasks = PendingTasks()
    await _acquire(container, clock, tasks)
    await _acquire(container, clock, tasks)

    with pytest.raises(TooManyStreams) as caught:
        await _acquire(container, clock, tasks)

    assert caught.value.retry_after_seconds == 10
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "concurrency"),): 1}


async def test_t219_an_expired_slot_is_taken_over(
    container: FakeContainer, clock: FakeClock
) -> None:
    tasks = PendingTasks()
    old = await _acquire(container, clock, tasks, slots=1)
    clock.advance(TTL + 1)

    taken = await _acquire(container, clock, tasks, slots=1)

    assert taken.slot == 1
    assert taken.etag != old.etag
    replace = container.calls_of("replace_item")[0]
    assert replace.kwargs["etag"] == old.etag
    assert replace.kwargs["match_condition"].name == "IfNotModified"


async def test_a_lease_at_its_expiry_instant_is_still_live(
    container: FakeContainer, clock: FakeClock
) -> None:
    tasks = PendingTasks()
    await _acquire(container, clock, tasks, slots=1)
    clock.advance(TTL)

    with pytest.raises(TooManyStreams):
        await _acquire(container, clock, tasks, slots=1)


async def test_a_lost_takeover_moves_to_the_next_slot(
    container: FakeContainer, clock: FakeClock
) -> None:
    tasks = PendingTasks()
    await _acquire(container, clock, tasks, slots=1)
    clock.advance(TTL + 1)
    container.fail("replace_item", status=412)

    lease = await _acquire(container, clock, tasks, slots=2)

    assert lease.slot == 2


async def test_a_slot_freed_between_the_409_and_the_read_is_skipped(
    container: FakeContainer, clock: FakeClock
) -> None:
    tasks = PendingTasks()
    held = await _acquire(container, clock, tasks, slots=1)

    async def free_slot_1() -> None:
        del container.items[((str(USER),), held.item_id)]

    container.before("read_item", free_slot_1)
    lease = await _acquire(container, clock, tasks, slots=2)

    assert lease.slot == 2


async def test_t219_a_release_frees_the_slot(container: FakeContainer, clock: FakeClock) -> None:
    tasks = PendingTasks()
    lease = await _acquire(container, clock, tasks, slots=1)

    await lease.release()
    await lease.release()

    assert lease.released
    assert container.get(str(USER), "lease:1") is None
    (delete,) = container.calls_of("delete_item")
    assert delete.kwargs["etag"] == lease.etag
    assert delete.kwargs["timeout"] == 3
    assert (await _acquire(container, clock, tasks, slots=1)).slot == 1


async def test_a_release_with_an_old_etag_never_removes_the_new_holder(
    container: FakeContainer, clock: FakeClock
) -> None:
    tasks = PendingTasks()
    old = await _acquire(container, clock, tasks, slots=1)
    clock.advance(TTL + 1)
    new = await _acquire(container, clock, tasks, slots=1)

    await old.release()

    stored = container.get(str(USER), "lease:1")
    assert stored is not None
    assert stored["_etag"] == new.etag


@pytest.mark.parametrize("fault", ["missing", "timeout", "connection"])
async def test_a_failed_release_is_left_to_the_ttl(
    container: FakeContainer, clock: FakeClock, fault: str
) -> None:
    tasks = PendingTasks()
    lease = await _acquire(container, clock, tasks, slots=1)
    if fault == "missing":
        container.items.clear()
    elif fault == "timeout":
        container.fail("delete_item", hang=True)
    else:
        container.fail("delete_item", error=ServiceRequestError("down"))

    await lease.release()

    assert len(container.calls_of("delete_item")) == 1


async def test_a_cancelled_caller_still_releases(
    container: FakeContainer, clock: FakeClock
) -> None:
    tasks = PendingTasks()
    lease = await _acquire(container, clock, tasks, slots=1)
    gate = asyncio.Event()
    container.before("delete_item", gate.wait)

    releasing = asyncio.create_task(lease.release())
    await asyncio.sleep(0)
    releasing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    gate.set()
    await tasks.drain()

    assert container.get(str(USER), "lease:1") is None


async def test_t223_a_lease_create_that_never_returns_is_unavailable_after_2_x_3_s(
    container: FakeContainer, clock: FakeClock
) -> None:
    container.fail("create_item", hang=True, times=2)
    start = clock.monotonic()

    with pytest.raises(UsageStoreUnavailable):
        await _acquire(container, clock, PendingTasks())

    assert clock.monotonic() - start == 6


async def test_download_leases_name_the_cited_turn_or_nothing(
    container: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    store, tasks = UsageStore(container), PendingTasks()
    lease = await acquire_lease(
        store,
        tasks,
        USER,
        DOWNLOAD_LEASE,
        slots=1,
        ttl_seconds=300,
        session_id=None,
        turn_id=None,
        clock=clock,
    )

    with pytest.raises(TooManyDownloads) as caught:
        await acquire_lease(
            store,
            tasks,
            USER,
            DOWNLOAD_LEASE,
            slots=1,
            ttl_seconds=300,
            session_id=uuid4(),
            turn_id=uuid4(),
            clock=clock,
        )

    stored = container.get(str(USER), lease.item_id)
    assert stored is not None
    assert (lease.item_id, stored["type"], stored["turnId"]) == (
        "dllease:1",
        "download_lease",
        None,
    )
    assert caught.value.retry_after_seconds == 10
    assert captured.points("docchat.ratelimit.rejections") == {
        (("reason", "download_concurrency"),): 1
    }
