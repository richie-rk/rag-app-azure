"""The usage package's shielded writes (`12-chat-pipeline.md` sections 3.3 and 15.6).

A lease release and a usage record's counter writes run as tasks of their own, so a cancelled
request or turn still frees its slot and still counts the call it caused. The event loop keeps only
weak references to tasks, so `PendingTasks` holds each one until it ends; the lifespan awaits the
remaining ones at shutdown, before the Cosmos DB client closes. Every task spawned here handles its
own failures and never raises.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class PendingTasks:
    """Strong references to the package's running tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def __len__(self) -> int:
        return len(self._tasks)

    def spawn[T](self, work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Start `work` as a task held until it ends."""
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for every task running now; each is bounded by its own operation timeouts."""
        # A finished task leaves the set only when its done callback runs, so the loop waits for
        # the unfinished ones; a task spawned meanwhile is found on the next pass.
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
