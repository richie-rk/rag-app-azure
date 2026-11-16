"""The per-index AI Search clients: an LRU of at most 16 per process (`01-repository.md` section
2.3 rule 3; `12-chat-pipeline.md` section 6.6; ADR 0113 item 5).

Physical index names are data (a rebuild creates a new physical index), so their clients can't be
built at start-up. Each client holds its own connection pool; the bound and the close on eviction
keep a retired index's client from leaking after a repoint. An evicted client may still be serving
a request, so a pipeline policy counts each client's requests in flight, and the client is closed
only once they have finished.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, override

from azure.core.pipeline import PipelineRequest, PipelineResponse
from azure.core.pipeline.policies import AsyncHTTPPolicy
from azure.core.rest import AsyncHttpResponse, HttpRequest

if TYPE_CHECKING:
    from azure.search.documents.aio import SearchClient

SEARCH_API_VERSION: Final = "2026-04-01"
SEARCH_CLIENTS_MAX: Final = 16


class _Entry:
    __slots__ = ("client", "evicted", "in_flight")

    def __init__(self) -> None:
        self.client: SearchClient | None = None
        self.in_flight = 0
        self.evicted = False


class InFlightPolicy(AsyncHTTPPolicy[HttpRequest, AsyncHttpResponse]):
    """Counts one client's requests in flight, retries included, and hands an evicted client to
    its cache for closing when its last request ends."""

    def __init__(self, cache: SearchClientCache, entry: _Entry) -> None:
        super().__init__()
        self._cache = cache
        self._entry = entry

    @property
    def in_flight(self) -> int:
        return self._entry.in_flight

    @override
    async def send(
        self, request: PipelineRequest[HttpRequest]
    ) -> PipelineResponse[HttpRequest, AsyncHttpResponse]:
        self._entry.in_flight += 1
        try:
            return await self.next.send(request)
        finally:
            self._entry.in_flight -= 1
            self._cache.release(self._entry)


def _succeeded(task: asyncio.Task[None]) -> bool:
    # A finished close needs no keeping; a failed one stays until `close` reports it.
    return task.done() and not task.cancelled() and task.exception() is None


type SearchClientFactory = Callable[[str, InFlightPolicy], SearchClient]
"""Builds the client of one physical index with the counting policy as a per-call policy."""


class SearchClientCache:
    """The process's search clients, keyed by physical index name, least recently used first."""

    def __init__(self, factory: SearchClientFactory, *, capacity: int = SEARCH_CLIENTS_MAX) -> None:
        if capacity < 1:
            raise ValueError("the cache holds at least one client")
        self._factory = factory
        self._capacity = capacity
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._closing: set[asyncio.Task[None]] = set()
        self._closed = False

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, physical_index_name: object) -> bool:
        return physical_index_name in self._entries

    def get(self, physical_index_name: str) -> SearchClient:
        """The client of one physical index, created on first use. Creating the seventeenth
        evicts the least recently used one. Call it on the event loop."""
        if self._closed:
            raise RuntimeError("the search clients are closed")
        entry = self._entries.get(physical_index_name)
        if entry is not None:
            self._entries.move_to_end(physical_index_name)
        else:
            entry = _Entry()
            entry.client = self._factory(physical_index_name, InFlightPolicy(self, entry))
            self._entries[physical_index_name] = entry
            while len(self._entries) > self._capacity:
                _, evicted = self._entries.popitem(last=False)
                evicted.evicted = True
                self.release(evicted)
        if entry.client is None:
            raise RuntimeError("a search client entry has no client")
        return entry.client

    def release(self, entry: _Entry) -> None:
        """Close an evicted client once nothing is in flight on it. The close runs as its own
        task, never inside the request whose end released the client."""
        if not entry.evicted or entry.in_flight or entry.client is None:
            return
        client, entry.client = entry.client, None
        self._closing = {task for task in self._closing if not _succeeded(task)}
        self._closing.add(asyncio.get_running_loop().create_task(client.close()))

    async def close(self) -> None:
        """Close every cached client and wait for the closes of evicted ones; failures are
        raised together once every close has ended."""
        self._closed = True
        clients = [entry.client for entry in self._entries.values() if entry.client is not None]
        self._entries.clear()
        closing, self._closing = self._closing, set()
        results = await asyncio.gather(
            *closing, *(client.close() for client in clients), return_exceptions=True
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup("closing the search clients failed", failures)
