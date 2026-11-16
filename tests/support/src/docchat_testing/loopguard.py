"""The event-loop guard (`84-testing.md` section 2.8; `01-repository.md` section 6.1).

Async end to end means no callback may hold the loop: a blocking call inside a coroutine stalls
every other request of the worker. asyncio's debug mode (on for every test) already measures each
callback and logs a warning for one slower than the loop's `slow_callback_duration`; the guard
turns that record, and the records of tasks lost while pending or whose exception nobody read,
into a test failure. The plugin creates every test loop through `guarded_event_loop`, so the
threshold is 100 ms on the loop each async test, its async fixtures and an in-process server run
on.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Final, override

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

SLOW_CALLBACK_SECONDS: Final = 0.1
ALLOW_BLOCKING_MARKER: Final = "allow_blocking"
ASYNCIO_LOGGER: Final = "asyncio"
TASK_MESSAGES: Final = (
    "Task was destroyed but it is pending",
    "Task exception was never retrieved",
)

# asyncio's debug-mode record: "Executing <callback> took 0.151 seconds".
_SLOW_CALLBACK_RE: Final = re.compile(
    r"\AExecuting (?P<callback>.+) took (?P<seconds>[0-9.]+) seconds\Z", re.DOTALL
)


def guarded_event_loop() -> asyncio.AbstractEventLoop:
    """A new event loop that reports any callback holding it for more than 100 ms (step 1)."""
    loop = asyncio.new_event_loop()
    loop.slow_callback_duration = SLOW_CALLBACK_SECONDS
    return loop


def describe(message: str) -> str | None:
    """The guard's line for one `asyncio` record, or `None` when the record isn't one it fails
    a test for."""
    if message.startswith("Executing ") and " took " in message:
        slow = _SLOW_CALLBACK_RE.match(message)
        if slow is None:
            return f"Event loop blocked > 100 ms: {message}"
        return (
            f"Event loop blocked > 100 ms: {slow.group('callback')} took {slow.group('seconds')} s"
        )
    if any(task_message in message for task_message in TASK_MESSAGES):
        return message
    return None


class BlockedLoopRecords(logging.Handler):
    """Collects the guard's lines from the `asyncio` logger (step 2)."""

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.occurrences: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        # Only the slow-callback warning counts as blocking; the task records come at error level.
        if message.startswith("Executing ") and record.levelno != logging.WARNING:
            return
        line = describe(message)
        if line is not None:
            self.occurrences.append(line)


def failure_message(occurrences: list[str]) -> str:
    """Every occurrence, one per line, in the order the loop reported them (step 3)."""
    return "\n".join(occurrences)


@pytest.fixture(autouse=True)
def blocked_loop_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fails the test when, while it ran, a callback held its loop for more than 100 ms, a task
    was destroyed while pending, or a task's exception was never retrieved.

    `@pytest.mark.allow_blocking(reason=…)` exempts one test; the plugin allows the marker only
    in the guard's own meta-tests under `tests/support/tests`.
    """
    if request.node.get_closest_marker(ALLOW_BLOCKING_MARKER) is not None:
        yield
        return
    handler = BlockedLoopRecords()
    logger = logging.getLogger(ASYNCIO_LOGGER)
    saved_level = logger.level
    # A test or library that raised the logger's threshold must not hide the warning.
    if logger.getEffectiveLevel() > logging.WARNING:
        logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
    if handler.occurrences:
        pytest.fail(failure_message(handler.occurrences), pytrace=False)
