"""The task tracker and the shutdown drain (`12-chat-pipeline.md` sections 1.2 and 14.8; ADR 0007).

The event loop keeps only weak references to tasks, so the tracker holds a strong one to every
turn task, to every task added with `register_background_task(task)` (the conversation store's
post-turn work) and to every shielded finaliser, until each ends.

On the first SIGTERM the worker class schedules `begin_drain()` on the worker's loop. It is a
plain function: it records the signal time, arms the drain point 90 s later and returns at once; a
later call does nothing, and a call before the lifespan has finished starting arms the same timer.
At the drain point every turn task still running is ended (a turn finalised `completed` writes
`citations` if it hasn't and then `done`; any other is stored `failed` with `service_restarting`),
every registered background task is cancelled (the next turn start's re-check recovers its work),
every running citation download is aborted with reason `drain` (`citations.abort_running_downloads`,
which keeps the worker's running downloads), and one `chat_drained` warning records the three
counts. 90 s plus the 15-second finaliser stays inside Gunicorn's 110-second
graceful timeout and App Service's 120-second stop window.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final, Protocol

from docchat_api.chat import telemetry
from docchat_api.chat.citations import abort_running_downloads

DRAIN_DELAY_SECONDS: Final = 90.0


class Drainable(Protocol):
    """A running turn, which the drain point ends."""

    def drain(self) -> None: ...


class TaskTracker:
    """The worker's running turn tasks, background tasks and finalisers; `abort_downloads` aborts
    the running citation downloads at the drain point and returns how many there were."""

    def __init__(self, abort_downloads: Callable[[], int] = abort_running_downloads) -> None:
        self._turns: dict[asyncio.Task[Any], Drainable] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._held: set[asyncio.Task[Any]] = set()
        self._abort_downloads = abort_downloads
        self._timer: asyncio.TimerHandle | None = None
        self.signalled_at: float | None = None
        self.drained = False

    @property
    def draining(self) -> bool:
        """Whether the stop signal has arrived."""
        return self.signalled_at is not None

    def turn_count(self) -> int:
        return sum(1 for task in self._turns if not task.done())

    def background_count(self) -> int:
        return sum(1 for task in self._background if not task.done())

    def add_turn(self, task: asyncio.Task[Any], turn: Drainable) -> None:
        """Hold a turn task until it ends; the drain point ends it through `turn.drain()`."""
        self._turns[task] = turn
        task.add_done_callback(self._turns_discard)

    def _turns_discard(self, task: asyncio.Task[Any]) -> None:
        self._turns.pop(task, None)

    def register_background_task(self, task: asyncio.Task[Any]) -> None:
        """Hold a task that runs after a response (the post-turn work) until it ends; the drain
        point cancels it."""
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def hold(self, task: asyncio.Task[Any]) -> None:
        """Hold a shielded task (a turn's finaliser) until it ends; the drain never cancels it."""
        self._held.add(task)
        task.add_done_callback(self._held.discard)

    def begin_drain(self) -> None:
        """Record the stop signal and arm the drain point; a second call does nothing. Runs on
        the loop thread, so the timer belongs to the worker's loop."""
        if self.signalled_at is not None:
            return
        loop = asyncio.get_running_loop()
        self.signalled_at = loop.time()
        self._timer = loop.call_later(DRAIN_DELAY_SECONDS, self.drain_point)

    def drain_point(self) -> None:
        """End every running turn, cancel every background task, abort every download, and
        write `chat_drained`."""
        self.drained = True
        turns = [turn for task, turn in list(self._turns.items()) if not task.done()]
        background = [task for task in self._background if not task.done()]
        for turn in turns:
            telemetry.record_drained_turn()
            turn.drain()
        for task in background:
            task.cancel()
        downloads = self._abort_downloads()
        telemetry.report_drained(
            turn_tasks=len(turns), background_tasks=len(background), downloads=downloads
        )

    async def wait_held(self) -> None:
        """Wait for the finalisers still running (lifespan shutdown); each has its own budget."""
        while pending := [task for task in self._held if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)


_TRACKER: Final = TaskTracker()
"""The worker process's tracker, which the module functions use."""


def tracker() -> TaskTracker:
    return _TRACKER


def register_background_task(task: asyncio.Task[Any]) -> None:
    """Hold `task` until it ends; the shutdown drain cancels it."""
    _TRACKER.register_background_task(task)


def begin_drain() -> None:
    """The worker's stop signal: arm the drain point 90 s from now, once."""
    _TRACKER.begin_drain()
