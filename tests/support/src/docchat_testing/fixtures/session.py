"""The test session's 8-hex ID and its setup loop (`84-testing.md` section 1.4).

The SQL and Cosmos DB databases are `docchat_test_<id>` and every queue name ends in `-<id>`, so
two sessions on one machine (a developer's run beside an editor's test run) never share a
database or a queue.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final

import pytest

SESSION_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{8}$")
SETUP_TIMEOUT_SECONDS: Final = 120.0
"""One session-level setup or teardown on the emulators, all of its calls together."""

_SESSION_ID_KEY: Final = pytest.StashKey[str]()


def session_id_of(config: pytest.Config) -> str:
    """The session's ID: 8 random lower-case hex digits, the same for every caller."""
    value = config.stash.get(_SESSION_ID_KEY, None)
    if value is None:
        value = secrets.token_hex(4)
        config.stash[_SESSION_ID_KEY] = value
    return value


def run_on_private_loop[T](
    work: Callable[[], Awaitable[T]], *, timeout: float = SETUP_TIMEOUT_SECONDS
) -> T:
    """Run `work()` to completion on a new event loop in a worker thread, so a synchronous
    session fixture can await an emulator without touching any test's loop; the clients it
    builds live and die on that loop. `timeout` bounds the whole work (120 s by default; the
    deployed seed data, which waits for ingestion, takes a longer bound)."""

    async def bounded() -> T:
        async with asyncio.timeout(timeout):
            return await work()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="docchat-test-setup") as pool:
        return pool.submit(asyncio.run, bounded()).result()


@pytest.fixture(scope="session")
def session_id(pytestconfig: pytest.Config) -> str:
    return session_id_of(pytestconfig)
