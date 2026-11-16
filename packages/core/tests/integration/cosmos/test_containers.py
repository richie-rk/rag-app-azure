"""Retention by time-to-live against the Cosmos DB emulator (T-328; `13-conversations-and-memory.md`
section 9 and acceptance criterion 43; `80-data-model.md` section 3.13).

`conversationRetentionDays` and `memoryRetentionDays` set the two containers' `defaultTtl`
(`ContainerDefinition.default_ttl`): `-1` keeps everything, `N` days expire each item `N * 86,400`
seconds after its last change. Days can't be waited for, so the test gives a container of the
`conversations` definition a 5-second `defaultTtl` and checks the service's semantics the
retention relies on: an item vanishes from reads 5 s after its own last change (a change restarts
its clock), and under `-1` nothing expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final

import pytest
from azure.cosmos.aio import ContainerProxy, DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from docchat_core.cosmos.containers import CONVERSATIONS, NO_DEFAULT_EXPIRY
from docchat_testing.fixtures.cosmos import emulator_indexing_policy

TTL_SECONDS: Final = 5
CHANGE_AT: Final = 4.0
"""When the changed item is written again: its clock restarts, so it expires at 9 s."""
FIRST_CHECK_AT: Final = 7.5
"""2.5 s after the untouched item expired, 1.5 s before the changed one does."""
LAST_CHECK_AT: Final = 12.0
"""3 s after the changed item expired; expired items vanish from reads before the background
deletion removes them."""


async def _container(database: DatabaseProxy, default_ttl: int) -> ContainerProxy:
    return await database.create_container(
        id=f"retention-{uuid.uuid4().hex[:12]}",
        partition_key=CONVERSATIONS.partition_key(),
        indexing_policy=emulator_indexing_policy(CONVERSATIONS.indexing_policy()),
        default_ttl=default_ttl,
    )


@pytest.fixture
async def expiring(cosmos_database: DatabaseProxy) -> AsyncIterator[ContainerProxy]:
    container = await _container(cosmos_database, TTL_SECONDS)
    yield container
    with contextlib.suppress(CosmosResourceNotFoundError):
        await cosmos_database.delete_container(container.id)


@pytest.fixture
async def kept(cosmos_database: DatabaseProxy) -> AsyncIterator[ContainerProxy]:
    container = await _container(cosmos_database, NO_DEFAULT_EXPIRY)
    yield container
    with contextlib.suppress(CosmosResourceNotFoundError):
        await cosmos_database.delete_container(container.id)


def _item(name: str) -> dict[str, Any]:
    user, session = str(uuid.uuid4()), str(uuid.uuid4())
    return {"id": name, "userId": user, "sessionId": session, "type": "session", "n": 0}


async def _present(container: ContainerProxy, item: dict[str, Any]) -> bool:
    try:
        await container.read_item(item["id"], [item["userId"], item["sessionId"]])
    except CosmosResourceNotFoundError:
        return False
    return True


async def test_t328_items_expire_after_their_last_change_and_never_under_minus_one(
    expiring: ContainerProxy, kept: ContainerProxy
) -> None:
    changed, untouched, forever = _item("changed"), _item("untouched"), _item("forever")
    for item in (changed, untouched):
        await expiring.create_item(item)
    await kept.create_item(forever)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.sleep(CHANGE_AT)
    await expiring.upsert_item({**changed, "n": 1})
    await asyncio.sleep(started + FIRST_CHECK_AT - loop.time())

    assert not await _present(expiring, untouched)
    assert await _present(expiring, changed)
    assert await _present(kept, forever)

    await asyncio.sleep(started + LAST_CHECK_AT - loop.time())

    assert not await _present(expiring, changed)
    assert await _present(kept, forever)
    assert CONVERSATIONS.default_ttl(0) == NO_DEFAULT_EXPIRY
