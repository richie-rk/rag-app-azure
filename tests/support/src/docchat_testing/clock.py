"""`FakeClock`, the virtual clock of the tests (`84-testing.md` section 1.1 principle 4).

Leases, deadlines, TTL arithmetic, backoff and cache lifetimes are tested by passing a
`FakeClock` to each component's clock port (`docchat_core.models.Clock`), so no test sleeps to
wait for a deadline: the test moves time with `advance`. `sleep` is the clock's own wait, for
components that take an injectable sleep beside their clock (the SQL layer's retry backoff, the
timer functions under the orchestration harness): their waits pass in virtual time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final

DEFAULT_START: Final = datetime(2026, 1, 1, tzinfo=UTC)
"""A fixed instant, so a test that doesn't choose its start time is still repeatable."""

DEFAULT_MONOTONIC_START: Final = 1_000.0
"""Non-zero, so code that treats 0 as "never" can't pass by accident."""


class FakeClock:
    """A `docchat_core.models.Clock` that moves only when told to.

    `now()` and `monotonic()` move together by the same number of seconds, as the two real
    clocks do while nobody adjusts the wall clock.
    """

    def __init__(
        self, start: datetime = DEFAULT_START, *, monotonic_start: float = DEFAULT_MONOTONIC_START
    ) -> None:
        if start.utcoffset() != timedelta(0):
            raise ValueError("a FakeClock starts at a timezone-aware UTC instant")
        self._now = start.astimezone(UTC)
        self._monotonic = monotonic_start

    def now(self) -> datetime:
        """The current virtual UTC time, timezone-aware."""
        return self._now

    def monotonic(self) -> float:
        """Virtual seconds that only move forward."""
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by `seconds`; time never runs backwards."""
        if seconds < 0:
            raise ValueError("a clock never moves backwards")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def advance_to(self, instant: datetime) -> None:
        """Move both clocks forward to `instant`; an instant already passed changes nothing."""
        if instant.utcoffset() != timedelta(0):
            raise ValueError("instants are timezone-aware UTC")
        if instant > self._now:
            self.advance((instant - self._now).total_seconds())

    async def sleep(self, seconds: float) -> None:
        """Wait `seconds` of virtual time: the clock moves at once, then the loop gets one turn,
        so other tasks still interleave with the sleeper as they would around a real sleep."""
        self.advance(seconds)
        await asyncio.sleep(0)
