from __future__ import annotations

from typing import Any, cast

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from docchat_core.cosmos.containers import CONTAINERS
from docchat_testing.fixtures import cosmos
from docchat_testing.fixtures.cosmos import (
    COSMOS_EMULATOR_ENDPOINT,
    CosmosTestDatabase,
    EmulatorContainers,
    database_name,
    emulator_client,
    emulator_indexing_policy,
)


class FakeDatabase:
    def __init__(self) -> None:
        self.containers: list[dict[str, Any]] = []

    async def create_container_if_not_exists(self, **options: Any) -> None:
        self.containers.append(options)


class FakeClient:
    instances: list[FakeClient] = []  # noqa: RUF012 - one list shared by the test's doubles

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.database = FakeDatabase()
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.closed = False
        self.missing = False
        FakeClient.instances.append(self)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def create_database_if_not_exists(self, name: str) -> FakeDatabase:
        self.created.append(name)
        return self.database

    async def delete_database(self, name: str) -> None:
        if self.missing:
            raise CosmosResourceNotFoundError(message="gone")  # type: ignore[no-untyped-call]
        self.deleted.append(name)


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> list[FakeClient]:
    FakeClient.instances = []
    monkeypatch.setattr(cosmos, "emulator_client", FakeClient)
    return FakeClient.instances


def test_create_builds_the_three_production_containers(clients: list[FakeClient]) -> None:
    database = CosmosTestDatabase(database_name("0123abcd"))
    database.create_blocking()
    (client,) = clients
    assert client.endpoint == COSMOS_EMULATOR_ENDPOINT
    assert client.created == ["docchat_test_0123abcd"]
    assert client.closed
    assert [options["id"] for options in client.database.containers] == [
        "conversations",
        "memories",
        "usage",
    ]
    for options, definition in zip(client.database.containers, CONTAINERS, strict=True):
        assert options["default_ttl"] == -1
        assert options["indexing_policy"] == emulator_indexing_policy(definition.indexing_policy())
        assert options["partition_key"] == definition.partition_key()


def test_the_emulator_policy_drops_only_array_wildcard_paths() -> None:
    policy = CONTAINERS[0].indexing_policy()
    emulated = emulator_indexing_policy(policy)
    dropped = [path for path in policy["includedPaths"] if path not in emulated["includedPaths"]]

    assert [path["path"] for path in dropped] == ["/retrieval/kept/[]/documentId/?"]
    assert {key: value for key, value in emulated.items() if key != "includedPaths"} == {
        key: value for key, value in policy.items() if key != "includedPaths"
    }


def test_delete_tolerates_a_missing_database(
    clients: list[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = CosmosTestDatabase("docchat_test_0123abcd")
    database.delete_blocking()
    assert clients[0].deleted == ["docchat_test_0123abcd"]

    class Missing(FakeClient):
        def __init__(self, endpoint: str) -> None:
            super().__init__(endpoint)
            self.missing = True

    monkeypatch.setattr(cosmos, "emulator_client", Missing)
    database.delete_blocking()
    assert clients[-1].deleted == []


async def test_emulator_client_uses_the_published_key() -> None:
    client = emulator_client()
    try:
        assert client.client_connection.url_connection.startswith("http://127.0.0.1:8081")
    finally:
        await client.close()


class FakeContainerOf:
    def __init__(self, name: str) -> None:
        self.id = name


class FakeEmulatorDatabase:
    def get_container_client(self, name: str) -> FakeContainerOf:
        return FakeContainerOf(name)


def test_the_emulator_containers_replace_each_container_by_name() -> None:
    wrapper = EmulatorContainers()
    built = cast("Any", FakeContainerOf("usage"))

    with pytest.raises(RuntimeError):
        wrapper(built)

    wrapper.database = cast("Any", FakeEmulatorDatabase())
    replaced = wrapper(built)
    assert replaced is not built
    assert replaced.id == "usage"

    audited: list[str] = []

    def audit(container: Any) -> Any:
        audited.append(container.id)
        return container

    wrapper.audit = audit
    assert wrapper(cast("Any", FakeContainerOf("conversations"))).id == "conversations"
    assert audited == ["conversations"]
