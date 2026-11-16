from __future__ import annotations

import asyncio

from docchat_api.usage.tasks import PendingTasks


async def test_a_spawned_task_is_held_until_it_ends() -> None:
    tasks = PendingTasks()
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "done"

    task = tasks.spawn(work())
    await asyncio.sleep(0)
    assert len(tasks) == 1
    gate.set()

    assert await task == "done"
    assert len(tasks) == 0


async def test_drain_waits_for_tasks_spawned_while_draining() -> None:
    tasks = PendingTasks()
    finished: list[str] = []

    async def second() -> None:
        await asyncio.sleep(0)
        finished.append("second")

    async def first() -> None:
        await asyncio.sleep(0)
        _ = tasks.spawn(second())
        finished.append("first")

    _ = tasks.spawn(first())
    await tasks.drain()

    assert finished == ["first", "second"]
    await asyncio.sleep(0)  # the done callbacks run on the next turn of the loop
    assert len(tasks) == 0


async def test_drain_with_nothing_running_returns() -> None:
    await PendingTasks().drain()
