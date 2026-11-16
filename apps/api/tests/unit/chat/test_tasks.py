"""The task tracker and the shutdown drain (T-273's unit half; `12-chat-pipeline.md` sections 1.2
and 14.8; ADR 0007)."""

from __future__ import annotations

import asyncio
from typing import Any

from docchat_api.chat import tasks
from docchat_api.chat.citations import abort_running_downloads
from docchat_api.chat.tasks import DRAIN_DELAY_SECONDS, TaskTracker


class Turn:
    def __init__(self) -> None:
        self.drained = 0

    def drain(self) -> None:
        self.drained += 1


async def _forever() -> None:
    await asyncio.Event().wait()


async def test_turns_and_background_tasks_are_held_until_they_end() -> None:
    tracker = TaskTracker()
    turn_task = asyncio.create_task(asyncio.sleep(0))
    background = asyncio.create_task(asyncio.sleep(0))
    tracker.add_turn(turn_task, Turn())
    tracker.register_background_task(background)

    assert (tracker.turn_count(), tracker.background_count()) == (1, 1)
    await asyncio.gather(turn_task, background)
    await asyncio.sleep(0)
    assert (tracker.turn_count(), tracker.background_count()) == (0, 0)


async def test_begin_drain_arms_the_drain_point_once() -> None:
    tracker = TaskTracker()

    tracker.begin_drain()
    first = tracker.signalled_at
    tracker.begin_drain()

    assert tracker.draining
    assert tracker.signalled_at == first
    assert tracker._timer is not None
    assert round(tracker._timer.when() - (first or 0)) == DRAIN_DELAY_SECONDS
    tracker._timer.cancel()


async def test_the_drain_point_ends_turns_cancels_background_work_and_aborts_downloads(
    captured: Any,
) -> None:
    aborted: list[str] = []

    def abort_downloads() -> int:
        aborted.append("drain")
        return 2

    tracker = TaskTracker(abort_downloads)
    turn = Turn()
    turn_task = asyncio.create_task(_forever())
    background = asyncio.create_task(_forever())
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    tracker.add_turn(turn_task, turn)
    tracker.add_turn(finished, Turn())
    tracker.register_background_task(background)

    tracker.drain_point()
    await asyncio.sleep(0)

    assert (turn.drained, aborted) == (1, ["drain"])
    assert background.cancelled()
    assert tracker.drained
    (record,) = captured.records("chat_drained")
    assert (
        record.__dict__["docchat.chat.turn_tasks"],
        record.__dict__["docchat.chat.background_tasks"],
        record.__dict__["docchat.chat.downloads"],
    ) == (1, 1, 2)
    assert sum(captured.points("docchat.chat.drained_turns").values()) == 1
    turn_task.cancel()


async def test_held_finalisers_are_waited_for() -> None:
    tracker = TaskTracker()
    gate = asyncio.Event()
    finaliser = asyncio.create_task(gate.wait())
    tracker.hold(finaliser)
    waiting = asyncio.create_task(tracker.wait_held())
    await asyncio.sleep(0)

    assert not waiting.done()
    gate.set()
    await waiting


async def test_the_module_functions_use_the_process_tracker() -> None:
    task = asyncio.create_task(asyncio.sleep(0))

    tasks.register_background_task(task)

    assert tasks.tracker().background_count() == 1
    await task
    assert callable(tasks.begin_drain)


def test_the_worker_tracker_aborts_the_running_citation_downloads() -> None:
    assert tasks.tracker()._abort_downloads is abort_running_downloads
