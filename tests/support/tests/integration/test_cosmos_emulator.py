"""T-571, the hierarchical partition key emulator test (`84-testing.md` sections 2.1 and 5.13;
`80-data-model.md` sections 1.3 and 3.3).

The pinned Cosmos DB emulator is the first tag with working hierarchical partition keys, which
the `conversations` container needs (`/userId`, `/sessionId`). The test creates a container with
that key of its own, writes items of one user in two sessions, reads one back by its full key and
queries the `/userId` prefix, which must reach both sessions and no other user.
"""

from __future__ import annotations

import contextlib
from uuid import uuid4

from azure.cosmos import PartitionKey
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from docchat_core.cosmos.containers import CONTAINERS


async def test_t571_the_emulator_serves_a_hierarchical_partition_key(
    cosmos_database: DatabaseProxy,
) -> None:
    name = f"t571-{uuid4().hex[:8]}"
    container = await cosmos_database.create_container(
        id=name, partition_key=PartitionKey(path=["/userId", "/sessionId"], kind="MultiHash")
    )
    try:
        user, other_user = str(uuid4()), str(uuid4())
        first, second = str(uuid4()), str(uuid4())
        wanted = str(uuid4())
        items: list[dict[str, object]] = [
            {"id": str(uuid4()), "userId": user, "sessionId": first, "n": 1},
            {"id": wanted, "userId": user, "sessionId": second, "n": 2},
            {"id": str(uuid4()), "userId": other_user, "sessionId": first, "n": 3},
        ]
        for item in items:
            await container.create_item(item)

        properties = await container.read()
        read = await container.read_item(wanted, partition_key=[user, second])
        prefix = [
            row
            async for row in container.query_items(
                "SELECT c.id, c.sessionId FROM c WHERE c.userId = @userId",
                parameters=[{"name": "@userId", "value": user}],
                partition_key=[user],
            )
        ]

        assert properties["partitionKey"]["paths"] == ["/userId", "/sessionId"]
        assert properties["partitionKey"]["kind"] == "MultiHash"
        assert read["n"] == 2
        assert sorted(row["sessionId"] for row in prefix) == sorted([first, second])
    finally:
        with contextlib.suppress(CosmosResourceNotFoundError):
            await cosmos_database.delete_container(name)


async def test_t571_the_session_conversations_container_has_the_hierarchical_key(
    cosmos_database: DatabaseProxy,
) -> None:
    (definition,) = [item for item in CONTAINERS if item.name == "conversations"]

    properties = await cosmos_database.get_container_client(definition.name).read()

    assert properties["partitionKey"]["paths"] == ["/userId", "/sessionId"]
    assert properties["partitionKey"]["kind"] == "MultiHash"
