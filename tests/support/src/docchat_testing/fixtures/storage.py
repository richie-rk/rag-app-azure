"""The Azurite fixture (`84-testing.md` sections 1.4 and 2.1; `80-data-model.md` sections 4 and 5).

Azurite through `UseDevelopmentStorage=true`. The data account's containers `documents`,
`renditions` and `ingestion-work` are shared by every session (created when absent, never
deleted), and tests keep apart by writing under fresh IDs, as every production path is built
from generated IDs. The queues `ingestion-requests` and `ingestion-requests-poison` exist once
per session with the session's 8-hex ID as a suffix, because a queue is read whole and a
message another session left would reach this session's reader; they are deleted at session end.

Session-level creation and deletion run on a private loop (`run_on_private_loop`); each test
builds its clients on its own loop through `storage`.
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Final

import pytest
import pytest_asyncio
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobBlock
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from azure.storage.queue import TextBase64DecodePolicy, TextBase64EncodePolicy
from azure.storage.queue.aio import QueueClient

from docchat_core.clients.emulators import AZURITE_CONNECTION_STRING
from docchat_testing.fixtures.session import run_on_private_loop, session_id_of

BLOB_CONTAINERS: Final = ("documents", "renditions", "ingestion-work")
INGESTION_QUEUE: Final = "ingestion-requests"
POISON_QUEUE: Final = "ingestion-requests-poison"
QUEUES: Final = (INGESTION_QUEUE, POISON_QUEUE)
STAGED_BLOCK_BYTES: Final = 4_194_304
CONNECTION_TIMEOUT_SECONDS: Final = 10
READ_TIMEOUT_SECONDS: Final = 60


def queue_name(queue: str, session_id: str) -> str:
    """`<queue>-<session ID>`, at most 63 characters as queue names allow."""
    return f"{queue}-{session_id}"


def blob_service_client() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(
        AZURITE_CONNECTION_STRING,
        connection_timeout=CONNECTION_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
    )


def queue_client(name: str) -> QueueClient:
    """A queue client encoding messages as base64, as the API's client and the trigger's
    `messageEncoding: base64` do."""
    return QueueClient.from_connection_string(
        AZURITE_CONNECTION_STRING,
        name,
        connection_timeout=CONNECTION_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        message_encode_policy=TextBase64EncodePolicy(),
        message_decode_policy=TextBase64DecodePolicy(),
    )


async def write_blob(container: ContainerClient, name: str, data: bytes) -> None:
    """`data` at `name`, replacing any blob there, staged in blocks of at most 4 MiB, each sent
    as a file object, and committed. `upload_blob` sends a blob under its single-put size as one
    bytes body, which aiohttp warns about above 1 MiB, and warnings fail the tests."""
    blob = container.get_blob_client(name)
    blocks: list[BlobBlock] = []
    for start in range(0, len(data), STAGED_BLOCK_BYTES):
        block = BlobBlock(block_id=f"{len(blocks):08d}")
        await blob.stage_block(block.id, io.BytesIO(data[start : start + STAGED_BLOCK_BYTES]))
        blocks.append(block)
    await blob.commit_block_list(blocks)


@dataclass(frozen=True, slots=True)
class StorageTestAccount:
    """The session's view of Azurite: the shared containers and its own queue names."""

    session_id: str
    queues: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_session(cls, session_id: str) -> StorageTestAccount:
        return cls(session_id, {queue: queue_name(queue, session_id) for queue in QUEUES})

    @property
    def ingestion_queue(self) -> str:
        return self.queues[INGESTION_QUEUE]

    @property
    def poison_queue(self) -> str:
        return self.queues[POISON_QUEUE]

    async def create(self) -> None:
        async with blob_service_client() as service:
            for container in BLOB_CONTAINERS:
                with contextlib.suppress(ResourceExistsError):
                    await service.create_container(container)
        for name in self.queues.values():
            async with queue_client(name) as client:
                with contextlib.suppress(ResourceExistsError):
                    await client.create_queue()

    async def delete(self) -> None:
        for name in self.queues.values():
            async with queue_client(name) as client:
                with contextlib.suppress(ResourceNotFoundError):
                    await client.delete_queue()


@dataclass(slots=True)
class StorageClients:
    """One test's clients on its own loop; `close()` closes every one of them."""

    account: StorageTestAccount
    blob_service: BlobServiceClient = field(default_factory=blob_service_client)
    _queues: dict[str, QueueClient] = field(default_factory=dict)

    def container(self, name: str) -> ContainerClient:
        if name not in BLOB_CONTAINERS:
            raise ValueError(f"no container {name!r} in the data account")
        return self.blob_service.get_container_client(name)

    def queue(self, queue: str = INGESTION_QUEUE) -> QueueClient:
        """The client of this session's `queue` (`ingestion-requests` or its poison queue)."""
        name = self.account.queues[queue]
        if name not in self._queues:
            self._queues[name] = queue_client(name)
        return self._queues[name]

    async def close(self) -> None:
        for client in self._queues.values():
            await client.close()
        await self.blob_service.close()


@pytest.fixture(scope="session")
def storage_account(pytestconfig: pytest.Config) -> Iterator[StorageTestAccount]:
    """The shared containers and the session's queues; the queues are deleted at session end."""
    account = StorageTestAccount.for_session(session_id_of(pytestconfig))
    try:
        run_on_private_loop(account.create)
        yield account
    finally:
        run_on_private_loop(account.delete)


@pytest_asyncio.fixture
async def storage(storage_account: StorageTestAccount) -> AsyncIterator[StorageClients]:
    """Blob and queue clients on this test's loop, closed after the test."""
    clients = StorageClients(storage_account)
    try:
        yield clients
    finally:
        await clients.close()
