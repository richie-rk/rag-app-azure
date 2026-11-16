"""`FakeClock` (`84-testing.md` section 1.1 principle 4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from docchat_core.models import Clock
from docchat_testing.clock import DEFAULT_MONOTONIC_START, DEFAULT_START, FakeClock


def test_a_fake_clock_is_a_clock() -> None:
    assert isinstance(FakeClock(), Clock)


def test_it_starts_at_a_fixed_utc_instant() -> None:
    clock = FakeClock()

    assert clock.now() == DEFAULT_START
    assert clock.now().tzinfo is UTC
    assert clock.monotonic() == DEFAULT_MONOTONIC_START


def test_it_stands_still_until_advanced() -> None:
    clock = FakeClock(datetime(2026, 5, 1, 12, tzinfo=UTC), monotonic_start=5.0)

    assert clock.now() == clock.now()
    assert clock.monotonic() == 5.0


def test_advance_moves_both_clocks_together() -> None:
    clock = FakeClock(datetime(2026, 5, 1, 12, tzinfo=UTC), monotonic_start=5.0)

    clock.advance(90.5)

    assert clock.now() == datetime(2026, 5, 1, 12, 1, 30, 500_000, tzinfo=UTC)
    assert clock.monotonic() == 95.5


def test_advance_refuses_to_go_backwards() -> None:
    clock = FakeClock()

    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)

    assert clock.now() == DEFAULT_START


def test_advance_to_moves_forward_and_never_back() -> None:
    clock = FakeClock(datetime(2026, 5, 1, tzinfo=UTC), monotonic_start=0.0)

    clock.advance_to(datetime(2026, 5, 1, 0, 0, 30, tzinfo=UTC))
    clock.advance_to(datetime(2026, 5, 1, 0, 0, 10, tzinfo=UTC))

    assert clock.now() == datetime(2026, 5, 1, 0, 0, 30, tzinfo=UTC)
    assert clock.monotonic() == 30.0


def test_the_start_must_be_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        FakeClock(datetime(2026, 5, 1, tzinfo=timezone(timedelta(hours=2))))


def test_a_utc_offset_zero_start_is_normalised_to_utc() -> None:
    clock = FakeClock(datetime(2026, 5, 1, tzinfo=timezone(timedelta(0))))

    assert clock.now().tzinfo is UTC


async def test_sleep_passes_in_virtual_time() -> None:
    clock = FakeClock()
    loop = asyncio.get_running_loop()
    started = loop.time()

    await clock.sleep(3600)

    assert clock.now() == DEFAULT_START + timedelta(hours=1)
    assert loop.time() - started < 1


async def test_sleep_lets_other_tasks_run() -> None:
    clock = FakeClock()
    ran: list[str] = []

    async def other() -> None:
        ran.append("other")

    task = asyncio.create_task(other())
    await clock.sleep(1)

    assert ran == ["other"]
    await task
