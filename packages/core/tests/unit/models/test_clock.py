from __future__ import annotations

from datetime import UTC, timedelta

from docchat_core.models.clock import Clock, SystemClock


def test_system_clock_is_a_clock() -> None:
    assert isinstance(SystemClock(), Clock)


def test_now_is_aware_utc() -> None:
    now = SystemClock().now()

    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)


def test_monotonic_never_goes_back() -> None:
    clock = SystemClock()

    first = clock.monotonic()

    assert clock.monotonic() >= first
