"""The `usage` container's operations and their failure rules (`12-chat-pipeline.md` sections 1.5,
3.1 and 3.5).

Every operation passes the async SDK's per-operation `timeout` keyword, which covers the whole
operation with the SDK's own retries: 2 s for a point read, 3 s for a create, patch, replace or
delete. `UsageStore` makes the operations of the checks that run before the response starts (the
rate windows, the quota reads and the lease acquisitions, for chat and for downloads) and the
background quota gate: a timed-out operation is retried once, and a second timeout, a connection
failure, a `429` left after the SDK's retries or any other service error raises
`UsageStoreUnavailable`, so the limits fail closed. The outcomes the algorithms decide on (a `404`
on a read, a `409` on a create, a `412` or a `404` on a conditioned write) pass through.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any, Final, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    ServiceRequestTimeoutError,
    ServiceResponseTimeoutError,
)
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

POINT_READ_TIMEOUT_SECONDS: Final = 2
WRITE_TIMEOUT_SECONDS: Final = 3
PRESTREAM_ATTEMPTS: Final = 2
"""A timed-out operation before the response starts is retried once."""

ETAG_FIELD: Final = "_etag"


class UsageContainer(Protocol):
    """The calls the usage package makes on the `usage` container (`azure.cosmos.aio`)."""

    async def read_item(
        self, item: str, partition_key: str, **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...

    async def patch_item(
        self, item: str, partition_key: str, patch_operations: list[dict[str, Any]], **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def replace_item(
        self, item: str, body: dict[str, Any], **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def delete_item(self, item: str, partition_key: str, **kwargs: Any) -> None: ...


class UsageStoreUnavailable(Exception):  # noqa: N818 - the name 12-chat-pipeline.md section 1.3 gives
    """The `usage` container couldn't serve an operation: two timeouts, a connection failure, a
    throttled request after the SDK's retries or a service error. Before the response starts the
    route answers `503 dependency_unavailable`; the background quota gate raises it to the
    conversation store, which defers its work."""

    def __init__(self) -> None:
        super().__init__("the usage store is unavailable")


def status_of(exc: BaseException) -> int | None:
    """The HTTP status of a Cosmos DB failure, or `None` when no response arrived."""
    if isinstance(exc, CosmosHttpResponseError):
        status = exc.status_code
        return status if isinstance(status, int) else None
    return None


def is_timeout(exc: BaseException) -> bool:
    """Whether the operation ran out of time: the SDK's per-operation `timeout`, a transport
    timeout, the event loop's timeout or the service's `408`."""
    if isinstance(
        exc,
        TimeoutError
        | CosmosClientTimeoutError
        | ServiceRequestTimeoutError
        | ServiceResponseTimeoutError,
    ):
        return True
    return status_of(exc) == HTTPStatus.REQUEST_TIMEOUT


def etag_of(item: Mapping[str, Any]) -> str | None:
    etag = item.get(ETAG_FIELD)
    return etag if isinstance(etag, str) else None


def required_etag(item: Mapping[str, Any]) -> str:
    """The ETag of an item the service just wrote; a response without one isn't one the limits
    can act on, so it fails closed like an unreachable store."""
    etag = etag_of(item)
    if etag is None:
        raise UsageStoreUnavailable
    return etag


_DECIDED: Final = (
    CosmosResourceNotFoundError,
    CosmosResourceExistsError,
    CosmosAccessConditionFailedError,
)
"""The outcomes the algorithms decide on; they pass through every wrapper."""


class UsageStore:
    """The `usage` operations made before the response starts, and by the background quota gate."""

    def __init__(self, container: UsageContainer) -> None:
        self.container = container

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, PRESTREAM_ATTEMPTS + 1):
            try:
                return await operation()
            except _DECIDED:
                raise
            except (AzureError, TimeoutError) as exc:
                if is_timeout(exc) and attempt < PRESTREAM_ATTEMPTS:
                    continue
                raise UsageStoreUnavailable from exc
        # The loop returns or raises on its last attempt.
        raise AssertionError("unreachable")

    async def read(self, item_id: str, partition_key: str) -> Mapping[str, Any] | None:
        """The item, or `None` when it doesn't exist (a missing item counts 0)."""
        try:
            return await self._call(
                lambda: self.container.read_item(
                    item_id, partition_key, timeout=POINT_READ_TIMEOUT_SECONDS
                )
            )
        except CosmosResourceNotFoundError:
            return None

    async def read_pair(
        self, first_id: str, second_id: str, partition_key: str
    ) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        """Two point reads run concurrently. Both finish before a failure is raised, so neither
        is left running with its outcome unobserved."""
        first: Mapping[str, Any] | BaseException | None
        second: Mapping[str, Any] | BaseException | None
        first, second = await asyncio.gather(
            self.read(first_id, partition_key),
            self.read(second_id, partition_key),
            return_exceptions=True,
        )
        if isinstance(first, BaseException):
            raise first
        if isinstance(second, BaseException):
            raise second
        return first, second

    async def create(self, body: dict[str, Any]) -> Mapping[str, Any]:
        """Create `body`; a `409` raises `CosmosResourceExistsError`."""
        return await self._call(
            lambda: self.container.create_item(body, timeout=WRITE_TIMEOUT_SECONDS)
        )

    async def patch(
        self, item_id: str, partition_key: str, operations: list[dict[str, Any]], *, etag: str
    ) -> Mapping[str, Any]:
        """Patch the item conditioned on `etag`; a `412` raises
        `CosmosAccessConditionFailedError`, a vanished item `CosmosResourceNotFoundError`."""
        return await self._call(
            lambda: self.container.patch_item(
                item_id,
                partition_key,
                operations,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                timeout=WRITE_TIMEOUT_SECONDS,
            )
        )

    async def replace(self, item_id: str, body: dict[str, Any], *, etag: str) -> Mapping[str, Any]:
        """Replace the item conditioned on `etag`, with the same outcomes as `patch`."""
        return await self._call(
            lambda: self.container.replace_item(
                item_id,
                body,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                timeout=WRITE_TIMEOUT_SECONDS,
            )
        )
