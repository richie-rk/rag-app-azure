"""The Cosmos DB emulator fixture (`84-testing.md` sections 1.4 and 2.1; `80-data-model.md`
section 3).

One database `docchat_test_<session ID>` per session holding the three production containers
`conversations`, `memories` and `usage`, each with the partition key, `defaultTtl` and indexing
policy of `docchat_core.cosmos.containers`, deleted at session end. Tests isolate themselves with
fresh random user IDs, which are the partition keys.

The database is created and deleted on a private event loop in a worker thread, because a
session-scoped client can't serve the per-test loops; each test gets its own client on its own
loop through `cosmos_database`.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import pytest
import pytest_asyncio
from azure.cosmos.aio import CosmosClient, DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from docchat_core.clients.emulators import COSMOS_EMULATOR_KEY
from docchat_core.cosmos.containers import CONTAINERS
from docchat_testing.fixtures.session import run_on_private_loop, session_id_of

if TYPE_CHECKING:
    from azure.cosmos.aio import ContainerProxy

COSMOS_EMULATOR_ENDPOINT: Final = "http://127.0.0.1:8081"
DATABASE_PREFIX: Final = "docchat_test_"
CONNECTION_TIMEOUT_SECONDS: Final = 5


def emulator_client(endpoint: str = COSMOS_EMULATOR_ENDPOINT) -> CosmosClient:
    """A client of the emulator with its published key, as local-emulator mode builds one."""
    return CosmosClient(
        endpoint,
        COSMOS_EMULATOR_KEY,
        consistency_level="Session",
        connection_timeout=CONNECTION_TIMEOUT_SECONDS,
    )


def emulator_indexing_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """The production indexing policy without its array-wildcard included paths, which the Linux
    emulator (vNext) can't create: a path such as `/retrieval/kept/[]/documentId/?` fails the
    container's creation with a PostgreSQL syntax error (SQLSTATE 42601). The emulator doesn't
    need the index to answer the query that uses it."""
    return {
        **policy,
        "includedPaths": [
            path for path in policy.get("includedPaths", []) if "[]" not in path["path"]
        ],
    }


@dataclass(frozen=True, slots=True)
class CosmosTestDatabase:
    """The session's database on the emulator."""

    name: str
    endpoint: str = COSMOS_EMULATOR_ENDPOINT

    async def create(self, client: CosmosClient) -> None:
        """Create the database and the three containers as production declares them."""
        database = await client.create_database_if_not_exists(self.name)
        for definition in CONTAINERS:
            await database.create_container_if_not_exists(
                id=definition.name,
                partition_key=definition.partition_key(),
                indexing_policy=emulator_indexing_policy(definition.indexing_policy()),
                default_ttl=definition.default_ttl(),
            )

    async def delete(self, client: CosmosClient) -> None:
        with contextlib.suppress(CosmosResourceNotFoundError):
            await client.delete_database(self.name)

    async def _with_client(self, action: Callable[[CosmosClient], Awaitable[None]]) -> None:
        async with emulator_client(self.endpoint) as client:
            await action(client)

    def create_blocking(self) -> None:
        run_on_private_loop(lambda: self._with_client(self.create))

    def delete_blocking(self) -> None:
        run_on_private_loop(lambda: self._with_client(self.delete))


@dataclass(slots=True)
class EmulatorContainers:
    """A `Ports.container_wrapper` serving the in-process API's Cosmos DB containers from the
    session's emulator database.

    The API's settings always name the database `docchat`, while the session's database has a
    name of its own, so each container the lifespan builds is replaced by the emulator container of
    the same name. Set `database` (the `cosmos_database` fixture, on the test's loop) before the
    app starts; `audit`, when set, wraps each container once more (a `CosmosAudit`)."""

    database: DatabaseProxy | None = None
    audit: Callable[[ContainerProxy], ContainerProxy] | None = None

    def __call__(self, container: ContainerProxy) -> ContainerProxy:
        if self.database is None:
            raise RuntimeError("set the emulator database before the app starts")
        replaced = self.database.get_container_client(container.id)
        return replaced if self.audit is None else self.audit(replaced)


def database_name(session_id: str) -> str:
    return f"{DATABASE_PREFIX}{session_id}"


@pytest.fixture(scope="session")
def cosmos_server(pytestconfig: pytest.Config) -> Iterator[CosmosTestDatabase]:
    """The session's database with its three containers; deleted at session end."""
    database = CosmosTestDatabase(database_name(session_id_of(pytestconfig)))
    try:
        database.create_blocking()
        yield database
    finally:
        database.delete_blocking()


@pytest_asyncio.fixture
async def cosmos_database(cosmos_server: CosmosTestDatabase) -> AsyncIterator[DatabaseProxy]:
    """The session's database through a client on this test's loop, closed after the test."""
    async with emulator_client(cosmos_server.endpoint) as client:
        yield client.get_database_client(cosmos_server.name)
