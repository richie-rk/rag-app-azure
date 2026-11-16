from __future__ import annotations

from collections.abc import Callable

import pytest
from azure.core.exceptions import ServiceRequestError, ServiceResponseTimeoutError
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosClientTimeoutError,
    CosmosResourceExistsError,
)

from docchat_api.usage.container import (
    UsageStore,
    UsageStoreUnavailable,
    etag_of,
    is_timeout,
    required_etag,
    status_of,
)
from docchat_core.cosmos.containers import USAGE
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer, http_error

USER = "0a11ce00-0000-4000-8000-000000000001"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def container(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(USAGE, clock=clock)


def test_timeouts_are_recognised() -> None:
    assert is_timeout(TimeoutError())
    sdk_timeout: Callable[[], Exception] = CosmosClientTimeoutError
    assert is_timeout(sdk_timeout())
    assert is_timeout(ServiceResponseTimeoutError("slow"))
    assert is_timeout(http_error(408))
    assert not is_timeout(http_error(503))
    assert not is_timeout(ServiceRequestError("down"))
    assert status_of(http_error(429)) == 429
    assert status_of(ServiceRequestError("down")) is None


def test_etags() -> None:
    assert etag_of({"_etag": '"1"'}) == '"1"'
    assert etag_of({}) is None
    with pytest.raises(UsageStoreUnavailable):
        required_etag({})


async def test_reads_carry_2_s_and_writes_3_s(container: FakeContainer) -> None:
    store = UsageStore(container)

    assert await store.read("rate:1", USER) is None
    created = await store.create({"id": "rate:1", "userId": USER, "count": 1})
    await store.patch(
        "rate:1", USER, [{"op": "incr", "path": "/count", "value": 1}], etag=created["_etag"]
    )
    current = container.get(USER, "rate:1")
    assert current is not None
    await store.replace("rate:1", {"id": "rate:1", "userId": USER}, etag=current["_etag"])

    timeouts = {call.method: call.kwargs["timeout"] for call in container.calls}
    assert timeouts == {"read_item": 2, "create_item": 3, "patch_item": 3, "replace_item": 3}
    patch = container.calls_of("patch_item")[0]
    assert patch.kwargs["match_condition"].name == "IfNotModified"


async def test_a_timed_out_operation_is_retried_once(
    container: FakeContainer, clock: FakeClock
) -> None:
    container.put({"id": "rate:1", "userId": USER, "count": 2})
    container.fail("read_item", hang=True)
    start = clock.monotonic()

    item = await UsageStore(container).read("rate:1", USER)

    assert item is not None
    assert item["count"] == 2
    assert clock.monotonic() - start == 2
    assert len(container.calls_of("read_item")) == 2


async def test_a_second_timeout_is_unavailable(container: FakeContainer, clock: FakeClock) -> None:
    container.fail("create_item", hang=True, times=2)
    start = clock.monotonic()

    with pytest.raises(UsageStoreUnavailable):
        await UsageStore(container).create({"id": "rate:1", "userId": USER, "count": 1})

    assert clock.monotonic() - start == 6


@pytest.mark.parametrize(
    "error",
    [ServiceRequestError("down"), http_error(429), http_error(503), http_error(403)],
    ids=["connection", "429", "503", "403"],
)
async def test_other_failures_are_unavailable_at_once(
    container: FakeContainer, error: Exception
) -> None:
    container.fail("read_item", error=error)

    with pytest.raises(UsageStoreUnavailable):
        await UsageStore(container).read("rate:1", USER)

    assert len(container.calls_of("read_item")) == 1


async def test_the_decided_outcomes_pass_through(container: FakeContainer) -> None:
    store = UsageStore(container)
    created = await store.create({"id": "lease:1", "userId": USER})

    with pytest.raises(CosmosResourceExistsError):
        await store.create({"id": "lease:1", "userId": USER})
    container.touch(USER, "lease:1")
    with pytest.raises(CosmosAccessConditionFailedError):
        await store.replace("lease:1", {"id": "lease:1", "userId": USER}, etag=created["_etag"])


async def test_read_pair_waits_for_both_reads_before_raising(container: FakeContainer) -> None:
    container.put({"id": "b", "userId": USER, "count": 1})
    container.fail("read_item", error=ServiceRequestError("down"), item_id="a")
    store = UsageStore(container)

    with pytest.raises(UsageStoreUnavailable):
        await store.read_pair("a", "b", USER)
    first, second = await store.read_pair("missing", "b", USER)

    assert first is None
    assert second is not None
    assert second["count"] == 1
