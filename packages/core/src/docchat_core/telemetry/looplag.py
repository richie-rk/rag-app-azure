"""The loop-lag monitor (`85-observability.md` section 4.7; ADR 0132).

A blocked event loop shows as lag in production, beside the test suite's loop guard and the load
test: the monitor sleeps for its interval and records how much later than asked it woke. Each API
worker starts it in its lifespan and each ingestion worker process at its first async invocation,
as one task in a lifespan-owned `asyncio.TaskGroup`, cancelled at shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Final

from docchat_core.telemetry.instruments import EVENT_LOOP_LAG
from docchat_core.telemetry.settings import LOOP_LAG_INTERVAL_MAX_MS, LOOP_LAG_INTERVAL_MIN_MS

_MS_PER_S: Final = 1000.0


async def monitor_loop_lag(interval_ms: int) -> None:
    """Record the loop's lag every `interval_ms` (`DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS`) in
    `docchat.runtime.event_loop_lag`, in milliseconds, until cancelled."""
    if not LOOP_LAG_INTERVAL_MIN_MS <= interval_ms <= LOOP_LAG_INTERVAL_MAX_MS:
        raise ValueError("the loop-lag interval is 100 to 5,000 ms")
    loop = asyncio.get_running_loop()
    interval = interval_ms / _MS_PER_S
    while True:
        started = loop.time()
        await asyncio.sleep(interval)
        lag = loop.time() - started - interval
        # The loop may wake a timer up to its clock resolution early; that isn't negative lag.
        EVENT_LOOP_LAG.record(max(lag, 0.0) * _MS_PER_S)
