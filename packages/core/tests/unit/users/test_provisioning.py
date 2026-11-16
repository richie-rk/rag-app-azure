"""Unit tests of first-sign-in provisioning (T-013's throttle; `11-api.md` section 3.2.1; ADR
0021), on a data-access layer without SQL."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from docchat_core.auth.principal import Principal
from docchat_core.sql.engine import SqlDeadlock, SqlTimeout, SqlUnavailable, SqlUniqueViolation
from docchat_core.sql.repositories.users import UPDATE_USER, ProvisionResult
from docchat_core.users.provisioning import (
    THROTTLE_CAPACITY,
    THROTTLE_SECONDS,
    LastSeenThrottle,
    ensure_user,
)
from docchat_testing.clock import FakeClock

NEW = ProvisionResult(provisioned=True, default_project_count=2)
KNOWN = ProvisionResult(provisioned=False, default_project_count=0)


def principal(*, admin: bool = False, display_name: str = "Ada") -> Principal:
    return Principal(
        oid=uuid4(),
        tid=uuid4(),
        azp=uuid4(),
        client="web",
        roles=frozenset({"Admin"} if admin else {"User"}),
        display_name=display_name,
        email="ada@example.test",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def throttle(clock: FakeClock) -> LastSeenThrottle:
    return LastSeenThrottle(clock)


def test_the_throttle_holds_50000_users_for_15_minutes() -> None:
    assert (THROTTLE_CAPACITY, THROTTLE_SECONDS) == (50_000, 900)


async def test_a_first_request_provisions_the_user(
    fake_sql: Any, throttle: LastSeenThrottle, captured: Any
) -> None:
    caller = principal()
    fake_sql.queue("ensure_user", NEW)

    assert await ensure_user(fake_sql, caller, throttle) == "provisioned"

    assert throttle.is_fresh(caller.oid)
    assert captured.span_attributes("docchat.users.ensure") == [
        {"docchat.users.outcome": "provisioned", "docchat.users.default_project_count": 2}
    ]
    assert captured.points("docchat.users.provisioned") == {(): 1}
    assert captured.events("user_provisioned") == [
        {"oid": str(caller.oid), "defaultProjectCount": 2}
    ]


async def test_the_token_values_are_written(fake_sql: Any, throttle: LastSeenThrottle) -> None:
    caller = principal(admin=True, display_name="")
    connection = fake_sql.connection(-1, 1)
    fake_sql.queue("ensure_user", connection)

    assert await ensure_user(fake_sql, caller, throttle) == "touched"

    update = connection.executed[1]
    assert update[0] is UPDATE_USER
    assert update[1] == {
        "oid": caller.oid,
        "tid": caller.tid,
        "display_name": "",
        "email": "ada@example.test",
        "role": "Admin",
    }


async def test_t013_no_sql_runs_while_the_last_seen_write_is_under_15_minutes_old(
    fake_sql: Any, throttle: LastSeenThrottle, clock: FakeClock, captured: Any
) -> None:
    caller = principal()
    fake_sql.queue("ensure_user", NEW, KNOWN)

    await ensure_user(fake_sql, caller, throttle)
    clock.advance(THROTTLE_SECONDS - 0.001)
    assert await ensure_user(fake_sql, caller, throttle) == "skipped"
    assert fake_sql.names() == ["ensure_user"]

    clock.advance(0.001)
    assert await ensure_user(fake_sql, caller, throttle) == "touched"
    assert fake_sql.names() == ["ensure_user", "ensure_user"]
    outcomes = [
        attributes["docchat.users.outcome"]
        for attributes in captured.span_attributes("docchat.users.ensure")
    ]
    assert outcomes == ["provisioned", "skipped", "touched"]
    assert captured.points("docchat.users.provisioned") == {(): 1}


async def test_another_user_is_not_throttled(fake_sql: Any, throttle: LastSeenThrottle) -> None:
    fake_sql.queue("ensure_user", NEW, NEW)
    await ensure_user(fake_sql, principal(), throttle)
    assert await ensure_user(fake_sql, principal(), throttle) == "provisioned"


@pytest.mark.parametrize(
    "failure", [SqlDeadlock(1205, "40001"), SqlUniqueViolation(2627, "23000", "PK_users")]
)
async def test_a_deadlock_or_duplicate_key_retries_the_transaction_once(
    fake_sql: Any, throttle: LastSeenThrottle, failure: Exception
) -> None:
    caller = principal()
    fake_sql.queue("ensure_user", failure, KNOWN)

    assert await ensure_user(fake_sql, caller, throttle) == "touched"
    assert fake_sql.names() == ["ensure_user", "ensure_user"]
    assert throttle.is_fresh(caller.oid)


async def test_a_second_failure_is_unavailable_and_leaves_the_throttle(
    fake_sql: Any, throttle: LastSeenThrottle, captured: Any
) -> None:
    caller = principal()
    fake_sql.queue(
        "ensure_user", SqlUniqueViolation(2627, "23000", "PK_users"), SqlDeadlock(1205, "40001")
    )

    with pytest.raises(SqlUnavailable) as caught:
        await ensure_user(fake_sql, caller, throttle)

    assert (caught.value.reason, caught.value.number) == ("exhausted", 1205)
    assert not throttle.is_fresh(caller.oid)
    assert len(throttle) == 0
    assert captured.span_attributes("docchat.users.ensure") == [{"docchat.users.outcome": "failed"}]


@pytest.mark.parametrize("failure", [SqlUnavailable("exhausted", 40613), SqlTimeout(0, "HYT00")])
async def test_sql_out_of_reach_fails_without_another_attempt(
    fake_sql: Any, throttle: LastSeenThrottle, failure: Exception
) -> None:
    caller = principal()
    fake_sql.queue("ensure_user", failure)

    with pytest.raises(type(failure)):
        await ensure_user(fake_sql, caller, throttle)
    assert fake_sql.names() == ["ensure_user"]
    assert not throttle.is_fresh(caller.oid)


def test_the_throttle_evicts_the_oldest_entry(clock: FakeClock) -> None:
    throttle = LastSeenThrottle(clock, capacity=2)
    first, second, third = uuid4(), uuid4(), uuid4()
    throttle.record(first)
    clock.advance(1)
    throttle.record(second)
    clock.advance(1)
    throttle.record(first)
    throttle.record(third)

    assert len(throttle) == 2
    assert not throttle.is_fresh(second)
    assert throttle.is_fresh(first)
    assert throttle.is_fresh(third)
