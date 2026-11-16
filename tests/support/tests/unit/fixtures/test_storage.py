from __future__ import annotations

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.queue import TextBase64EncodePolicy

from docchat_testing.fixtures import storage
from docchat_testing.fixtures.session import run_on_private_loop
from docchat_testing.fixtures.storage import (
    BLOB_CONTAINERS,
    StorageClients,
    StorageTestAccount,
    queue_client,
    queue_name,
)


class FakeService:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.created: list[str] = []

    async def __aenter__(self) -> FakeService:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def create_container(self, name: str) -> None:
        if name in self.existing:
            raise ResourceExistsError(message="exists")
        self.created.append(name)


class FakeQueue:
    log: list[tuple[str, str]] = []  # noqa: RUF012 - one log shared by the test's doubles
    existing: set[str] = set()  # noqa: RUF012

    def __init__(self, name: str) -> None:
        self.name = name

    async def __aenter__(self) -> FakeQueue:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def create_queue(self) -> None:
        if self.name in FakeQueue.existing:
            raise ResourceExistsError(message="exists")
        FakeQueue.log.append(("create", self.name))

    async def delete_queue(self) -> None:
        if self.name not in {name for action, name in FakeQueue.log if action == "create"}:
            raise ResourceNotFoundError(message="gone")
        FakeQueue.log.append(("delete", self.name))


def test_queue_names_carry_the_session_id() -> None:
    account = StorageTestAccount.for_session("0123abcd")
    assert account.ingestion_queue == "ingestion-requests-0123abcd"
    assert account.poison_queue == "ingestion-requests-poison-0123abcd"
    assert len(queue_name("ingestion-requests-poison", "ffffffff")) <= 63


def test_create_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService({"documents"})
    FakeQueue.log = []
    FakeQueue.existing = {"ingestion-requests-poison-0123abcd"}
    monkeypatch.setattr(storage, "blob_service_client", lambda: service)
    monkeypatch.setattr(storage, "queue_client", FakeQueue)
    account = StorageTestAccount.for_session("0123abcd")
    run_on_private_loop(account.create)
    assert service.created == ["renditions", "ingestion-work"]
    assert FakeQueue.log == [("create", "ingestion-requests-0123abcd")]
    run_on_private_loop(account.delete)
    assert FakeQueue.log[-1] == ("delete", "ingestion-requests-0123abcd")


async def test_clients_on_the_test_loop() -> None:
    clients = StorageClients(StorageTestAccount.for_session("0123abcd"))
    try:
        assert clients.container("renditions").container_name == "renditions"
        with pytest.raises(ValueError, match="no container"):
            clients.container("other")
        queue = clients.queue()
        assert queue.queue_name == "ingestion-requests-0123abcd"
        assert clients.queue() is queue
        assert clients.queue("ingestion-requests-poison").queue_name.startswith(
            "ingestion-requests-poison-"
        )
    finally:
        await clients.close()
    assert BLOB_CONTAINERS == ("documents", "renditions", "ingestion-work")


async def test_queue_client_encodes_base64() -> None:
    client = queue_client("ingestion-requests-0123abcd")
    try:
        assert isinstance(client._message_encode_policy, TextBase64EncodePolicy)
        assert client.url.startswith("http://127.0.0.1:10001/devstoreaccount1/")
    finally:
        await client.close()
