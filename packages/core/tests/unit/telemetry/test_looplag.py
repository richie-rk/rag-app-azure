"""The loop-lag monitor (T-496).

Each scenario runs on a private event loop without debug mode, because one of them blocks the
loop on purpose and the test suite's loop guard fails any test whose own loop is blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

import pytest

from docchat_core.telemetry.looplag import monitor_loop_lag

type MetricPoints = Callable[[str], list[Any]]

_INTERVAL_MS = 100


def _lag(metric_points: MetricPoints) -> Any:
    (point,) = metric_points("docchat.runtime.event_loop_lag")
    return point


async def _run(seconds: float, block_after: float | None = None) -> asyncio.Task[None]:
    task = asyncio.create_task(monitor_loop_lag(_INTERVAL_MS))
    # One turn lets the monitor take its first start time and begin its first sleep, so
    # `block_after` is measured from that start.
    await asyncio.sleep(0)
    if block_after is not None:
        await asyncio.sleep(block_after)
        # A plain callback, so the loop runs the 200 ms blocking call between two of its turns.
        asyncio.get_running_loop().call_soon(time.sleep, 0.2)
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task


def test_t496_an_idle_loop_shows_about_no_lag(
    metric_points: MetricPoints, api_service: None
) -> None:
    task = asyncio.run(_run(0.45), debug=False)

    point = _lag(metric_points)
    assert task.cancelled()
    assert point.count >= 3
    assert point.min >= 0
    assert point.max < 50
    assert dict(point.attributes) == {"service": "api"}


def test_t496_a_blocking_call_shows_as_lag(metric_points: MetricPoints) -> None:
    # The block starts 75 ms into the monitor's first 100 ms sleep, so that timer wakes about
    # 175 ms late: the block's phase inside the interval decides the lag (a block at the start
    # of a sleep delays its timer by only 100 ms), and 75 ms leaves 25 ms of margin each way.
    task = asyncio.run(_run(0.45, block_after=0.075), debug=False)

    assert task.cancelled()
    assert _lag(metric_points).max >= 150


def test_t496_the_monitor_stops_when_cancelled(metric_points: MetricPoints) -> None:
    task = asyncio.run(_run(0.25), debug=False)
    count = _lag(metric_points).count

    async def later() -> None:
        await asyncio.sleep(0.25)

    asyncio.run(later(), debug=False)

    assert task.cancelled()
    assert _lag(metric_points).count == count


@pytest.mark.parametrize("interval_ms", [99, 5001])
async def test_an_interval_outside_its_range_is_refused(interval_ms: int) -> None:
    with pytest.raises(ValueError, match="100 to 5,000"):
        await monitor_loop_lag(interval_ms)
