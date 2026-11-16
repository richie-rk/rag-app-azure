from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast, override

import pytest

from docchat_core.clients.search import (
    SEARCH_API_VERSION,
    SEARCH_CLIENTS_MAX,
    InFlightPolicy,
    SearchClientCache,
)

if TYPE_CHECKING:
    from azure.search.documents.aio import SearchClient


class _FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _FailingClient(_FakeClient):
    @override
    async def close(self) -> None:
        await super().close()
        raise OSError("close failed")


class _Next:
    """Stands for the rest of the pipeline: holds the request until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def send(self, request: Any) -> str:
        await self.release.wait()
        return "response"


class _Factory:
    def __init__(self, client_type: type[_FakeClient] = _FakeClient) -> None:
        self.client_type = client_type
        self.clients: dict[str, _FakeClient] = {}
        self.policies: dict[str, InFlightPolicy] = {}

    def __call__(self, name: str, policy: InFlightPolicy) -> SearchClient:
        client = self.client_type(name)
        self.clients[name] = client
        self.policies[name] = policy
        return cast("SearchClient", client)


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def test_the_api_version_and_the_bound() -> None:
    assert SEARCH_API_VERSION == "2026-04-01"
    assert SEARCH_CLIENTS_MAX == 16


async def test_the_same_name_gives_the_same_client() -> None:
    factory = _Factory()
    cache = SearchClientCache(factory)

    first = cache.get("docchat-a-g1")

    assert cache.get("docchat-a-g1") is first
    assert list(factory.clients) == ["docchat-a-g1"]
    await cache.close()


async def test_the_seventeenth_name_evicts_and_closes_the_least_recently_used() -> None:
    factory = _Factory()
    cache = SearchClientCache(factory)
    names = [f"docchat-{n}-g1" for n in range(17)]
    for name in names[:16]:
        cache.get(name)
    cache.get(names[0])  # now the most recently used

    cache.get(names[16])
    await _settle()

    assert len(cache) == 16
    assert names[1] not in cache
    assert names[0] in cache
    assert factory.clients[names[1]].closed == 1
    assert all(factory.clients[name].closed == 0 for name in names if name != names[1])
    await cache.close()


async def test_an_evicted_client_closes_only_after_its_request_in_flight_ends() -> None:
    factory = _Factory()
    cache = SearchClientCache(factory, capacity=1)
    cache.get("docchat-a-g1")
    policy = factory.policies["docchat-a-g1"]
    pipeline = _Next()
    policy.next = pipeline  # type: ignore[assignment]
    request = asyncio.create_task(policy.send(cast("Any", object())))
    await _settle()
    assert policy.in_flight == 1

    cache.get("docchat-b-g1")
    await _settle()
    assert factory.clients["docchat-a-g1"].closed == 0

    pipeline.release.set()
    assert cast("object", await request) == "response"
    await _settle()

    assert policy.in_flight == 0
    assert factory.clients["docchat-a-g1"].closed == 1
    await cache.close()


async def test_t235_seventeen_names_leave_sixteen_clients_and_shutdown_closes_them() -> None:
    factory = _Factory()
    cache = SearchClientCache(factory)
    names = [f"docchat-{n:032x}-g1" for n in range(17)]
    for name in names[:16]:
        cache.get(name)
    least_recent = factory.policies[names[0]]
    pipeline = _Next()
    least_recent.next = pipeline  # type: ignore[assignment]
    request = asyncio.create_task(least_recent.send(cast("Any", object())))
    await _settle()

    cache.get(names[16])
    await _settle()

    assert len(cache) == 16
    assert names[0] not in cache
    assert factory.clients[names[0]].closed == 0
    pipeline.release.set()
    await request
    await _settle()
    assert factory.clients[names[0]].closed == 1

    await cache.close()

    assert [factory.clients[name].closed for name in names[1:]] == [1] * 16
    assert factory.clients[names[0]].closed == 1


async def test_a_failed_request_still_ends_its_count() -> None:
    factory = _Factory()
    cache = SearchClientCache(factory)
    cache.get("docchat-a-g1")
    policy = factory.policies["docchat-a-g1"]

    class _Refusing:
        async def send(self, request: Any) -> str:
            raise TimeoutError

    policy.next = _Refusing()  # type: ignore[assignment]
    with pytest.raises(TimeoutError):
        await policy.send(cast("Any", object()))

    assert policy.in_flight == 0
    await cache.close()


async def test_close_closes_every_cached_client_and_refuses_new_ones() -> None:
    factory = _Factory()
    cache = SearchClientCache(factory)
    cache.get("docchat-a-g1")
    cache.get("docchat-b-g1")

    await cache.close()

    assert [client.closed for client in factory.clients.values()] == [1, 1]
    with pytest.raises(RuntimeError, match="closed"):
        cache.get("docchat-a-g1")


async def test_a_failed_close_is_raised_after_every_close_ran() -> None:
    factory = _Factory(_FailingClient)
    cache = SearchClientCache(factory)
    cache.get("docchat-a-g1")
    cache.get("docchat-b-g1")

    with pytest.raises(BaseExceptionGroup) as caught:
        await cache.close()

    assert len(caught.value.exceptions) == 2
    assert [client.closed for client in factory.clients.values()] == [1, 1]


async def test_a_failed_close_of_an_evicted_client_is_reported_at_shutdown() -> None:
    factory = _Factory(_FailingClient)
    cache = SearchClientCache(factory, capacity=1)
    cache.get("docchat-a-g1")
    cache.get("docchat-b-g1")
    await _settle()

    with pytest.raises(BaseExceptionGroup) as caught:
        await cache.close()

    assert len(caught.value.exceptions) == 2


def test_the_capacity_is_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SearchClientCache(_Factory(), capacity=0)
