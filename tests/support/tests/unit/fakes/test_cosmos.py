from __future__ import annotations

import asyncio
from typing import Any

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ServiceRequestError
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from docchat_core.cosmos.containers import CONVERSATIONS, USAGE
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer, batch_error, http_error

USER = "0a11ce00-0000-4000-8000-000000000001"
SESSION = "5e550000-0000-4000-8000-000000000001"


def _usage(clock: FakeClock | None = None) -> FakeContainer:
    return FakeContainer.of(USAGE, clock=clock)


def _conversations() -> FakeContainer:
    return FakeContainer.of(CONVERSATIONS)


async def test_a_created_item_reads_back_with_an_etag() -> None:
    container = _usage()

    created = await container.create_item({"id": "rate:1", "userId": USER, "count": 1})
    read = await container.read_item("rate:1", USER)

    assert read == created
    assert read["_etag"]
    assert container.calls_of("read_item")[0].partition_key == (USER,)


async def test_404_409_and_412_are_the_sdk_exceptions() -> None:
    container = _usage()
    item = await container.create_item({"id": "a", "userId": USER})

    with pytest.raises(CosmosResourceNotFoundError):
        await container.read_item("b", USER)
    with pytest.raises(CosmosResourceExistsError):
        await container.create_item({"id": "a", "userId": USER})
    with pytest.raises(CosmosAccessConditionFailedError):
        await container.replace_item(
            "a",
            {"id": "a", "userId": USER},
            etag='"stale"',
            match_condition=MatchConditions.IfNotModified,
        )
    replaced = await container.replace_item(
        "a",
        {"id": "a", "userId": USER},
        etag=item["_etag"],
        match_condition=MatchConditions.IfNotModified,
    )
    assert replaced["_etag"] != item["_etag"]


async def test_patch_increments_sets_and_honours_its_etag() -> None:
    container = _usage()
    item = await container.create_item({"id": "a", "userId": USER, "count": 1, "nested": {}})

    patched = await container.patch_item(
        "a",
        USER,
        [
            {"op": "incr", "path": "/count", "value": 2},
            {"op": "set", "path": "/nested/state", "value": "x"},
            {"op": "incr", "path": "/fresh", "value": 5},
        ],
        etag=item["_etag"],
        match_condition=MatchConditions.IfNotModified,
    )

    assert (patched["count"], patched["nested"], patched["fresh"]) == (3, {"state": "x"}, 5)
    with pytest.raises(CosmosAccessConditionFailedError):
        await container.patch_item(
            "a",
            USER,
            [{"op": "incr", "path": "/count", "value": 1}],
            etag=item["_etag"],
            match_condition=MatchConditions.IfNotModified,
        )
    with pytest.raises(CosmosResourceNotFoundError):
        await container.patch_item("missing", USER, [{"op": "set", "path": "/x", "value": 1}])


async def test_delete_with_an_old_etag_keeps_the_item() -> None:
    container = _usage()
    old = await container.create_item({"id": "lease:1", "userId": USER})
    new_etag = container.touch(USER, "lease:1", turnId="t")

    with pytest.raises(CosmosAccessConditionFailedError):
        await container.delete_item(
            "lease:1", USER, etag=old["_etag"], match_condition=MatchConditions.IfNotModified
        )
    await container.delete_item(
        "lease:1", USER, etag=new_etag, match_condition=MatchConditions.IfNotModified
    )

    assert container.get(USER, "lease:1") is None


async def test_a_batch_is_all_or_nothing() -> None:
    container = _conversations()
    existing = container.put({"id": "t1", "userId": USER, "sessionId": SESSION, "type": "turn"})

    with pytest.raises(CosmosBatchOperationError) as caught:
        await container.execute_item_batch(
            [
                ("create", ({"id": "s", "userId": USER, "sessionId": SESSION},)),
                ("create", ({"id": "t1", "userId": USER, "sessionId": SESSION},)),
            ],
            [USER, SESSION],
        )

    assert (caught.value.error_index, caught.value.status_code) == (1, 409)
    assert container.get([USER, SESSION], "s") is None
    results = await container.execute_item_batch(
        [
            ("create", ({"id": "s", "userId": USER, "sessionId": SESSION},)),
            (
                "replace",
                ("t1", {"id": "t1", "userId": USER, "sessionId": SESSION, "status": "done"}),
                {"if_match_etag": existing["_etag"]},
            ),
        ],
        [USER, SESSION],
    )
    assert [result["statusCode"] for result in results] == [201, 200]
    assert results[1]["eTag"] == container.items[((USER, SESSION), "t1")]["_etag"]


async def test_a_batch_replace_with_a_stale_etag_fails_with_412() -> None:
    container = _conversations()
    container.put({"id": "t1", "userId": USER, "sessionId": SESSION})

    with pytest.raises(CosmosBatchOperationError) as caught:
        await container.execute_item_batch(
            [
                (
                    "replace",
                    ("t1", {"id": "t1", "userId": USER, "sessionId": SESSION}),
                    {"if_match_etag": '"stale"'},
                )
            ],
            [USER, SESSION],
        )

    assert caught.value.status_code == 412


async def test_queries_filter_order_project_and_page() -> None:
    container = _conversations()
    for seq in range(1, 26):
        container.put(
            {
                "id": f"t{seq}",
                "userId": USER,
                "sessionId": SESSION,
                "type": "turn",
                "status": "completed" if seq % 5 else "failed",
                "seq": seq,
                "question": f"q{seq}",
            }
        )
    container.put({"id": "other", "userId": USER, "sessionId": "x", "type": "turn", "seq": 99})

    pager = container.query_items(
        "SELECT c.seq, c.question FROM c WHERE c.userId = @userId AND c.type = @type "
        "AND c.status = @status AND c.seq > @covered ORDER BY c.seq DESC",
        parameters=[
            {"name": "@userId", "value": USER},
            {"name": "@type", "value": "turn"},
            {"name": "@status", "value": "completed"},
            {"name": "@covered", "value": 2},
        ],
        partition_key=[USER, SESSION],
        max_item_count=10,
    )
    pages = pager.by_page()
    first = [row async for row in await anext(pages)]

    assert [row["seq"] for row in first] == [24, 23, 22, 21, 19, 18, 17, 16, 14, 13]
    assert first[0] == {"seq": 24, "question": "q24"}
    assert container.pages_served == 1
    assert pages.continuation_token is not None
    rest = [row async for page in pages async for row in page]
    assert [row["seq"] for row in rest] == [12, 11, 9, 8, 7, 6, 4, 3]
    assert container.pages_served == 2


async def test_a_prefix_query_and_array_contains() -> None:
    container = _conversations()
    container.put({"id": "a", "userId": USER, "sessionId": "s1", "state": "pending", "n": 2})
    container.put({"id": "b", "userId": USER, "sessionId": "s2", "state": "done", "n": 1})

    rows = [
        row
        async for row in container.query_items(
            "SELECT TOP @top * FROM c WHERE ARRAY_CONTAINS(@states, c.state) ORDER BY c.n ASC",
            parameters=[{"name": "@states", "value": ["pending"]}, {"name": "@top", "value": 5}],
            partition_key=[USER],
        )
    ]

    assert [row["id"] for row in rows] == ["a"]


async def test_a_keyset_query_orders_by_two_paths_and_reads_a_parenthesised_or() -> None:
    container = _conversations()
    sessions = [("s1", "2026-10-30T10:00:00.000Z"), ("s2", "2026-10-30T11:00:00.000Z")]
    sessions += [("s3", "2026-10-30T11:00:00.000Z"), ("s4", "2026-10-30T09:00:00.000Z")]
    for session, last_turn_at in sessions:
        container.put(
            {
                "id": session,
                "userId": USER,
                "sessionId": session,
                "type": "session",
                "projectId": "p",
                "lastTurnAt": last_turn_at,
            }
        )
    query = (
        "SELECT TOP @take c.sessionId FROM c WHERE c.userId = @userId AND c.type = @type "
        "AND ARRAY_CONTAINS(@projectIds, c.projectId) "
        "AND (c.lastTurnAt < @t OR (c.lastTurnAt = @t AND c.sessionId < @s)) "
        "ORDER BY c.lastTurnAt DESC, c.sessionId DESC"
    )
    parameters: list[dict[str, Any]] = [
        {"name": "@take", "value": 2},
        {"name": "@userId", "value": USER},
        {"name": "@type", "value": "session"},
        {"name": "@projectIds", "value": ["p"]},
        {"name": "@t", "value": "2026-10-30T11:00:00.000Z"},
        {"name": "@s", "value": "s3"},
    ]

    rows = [
        row
        async for row in container.query_items(query, parameters=parameters, partition_key=[USER])
    ]

    assert [row["sessionId"] for row in rows] == ["s2", "s1"]


def test_an_unsupported_query_is_refused() -> None:
    with pytest.raises(NotImplementedError):
        _conversations().query_items("SELECT VALUE COUNT(1) FROM c GROUP BY c.x")


async def test_faults_raise_before_or_after_the_commit() -> None:
    container = _usage()
    container.fail("create_item", error=ServiceRequestError("down"))
    container.fail("create_item", status=503, after_commit=True)

    with pytest.raises(ServiceRequestError):
        await container.create_item({"id": "a", "userId": USER})
    assert container.get(USER, "a") is None
    with pytest.raises(CosmosHttpResponseError) as caught:
        await container.create_item({"id": "a", "userId": USER})
    assert caught.value.status_code == 503
    assert container.get(USER, "a") is not None
    await container.create_item({"id": "b", "userId": USER})


async def test_a_hung_call_honours_its_timeout_in_virtual_time() -> None:
    clock = FakeClock()
    container = _usage(clock)
    container.fail("read_item", hang=True, times=2)
    start = clock.monotonic()

    for _ in range(2):
        with pytest.raises(CosmosClientTimeoutError):
            await container.read_item("a", USER, timeout=2)

    assert clock.monotonic() - start == 4


async def test_a_hung_call_without_a_timeout_never_answers() -> None:
    container = _usage()
    container.fail("read_item", hang=True)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await container.read_item("a", USER)


async def test_a_fault_can_target_one_item_and_a_hook_runs_before_a_call() -> None:
    container = _usage()
    seen: list[str] = []
    container.fail("read_item", status=429, item_id="b")
    container.before("create_item", lambda: seen.append("before"))
    await container.create_item({"id": "a", "userId": USER})
    await container.create_item({"id": "b", "userId": USER})

    await container.read_item("a", USER)
    with pytest.raises(CosmosHttpResponseError):
        await container.read_item("b", USER)
    assert seen == ["before"]


async def test_the_response_hook_gets_the_request_charge() -> None:
    container = _usage()
    charges: list[Any] = []

    await container.create_item(
        {"id": "a", "userId": USER},
        response_hook=lambda headers, _body: charges.append(headers["x-ms-request-charge"]),
    )

    assert charges == ["1.0"]


def test_the_error_helpers_carry_their_status() -> None:
    assert http_error(408).status_code == 408
    error = batch_error(2, 412, 3)
    assert (error.error_index, error.status_code) == (2, 412)
    assert [response["statusCode"] for response in error.operation_responses] == [424, 424, 412]
