from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from docchat_testing.adminapi import AdminApi, UnusableResourceError
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.clock import FakeClock
from docchat_testing.load.common import LOAD_USERS, LoadError
from docchat_testing.load.seeding import (
    CONFIG_SETTLE,
    INDEX_NAME,
    SESSIONS,
    ensure_default_project,
    provision_users,
    seed,
    seed_sessions,
    static_token,
)
from docchat_testing.seed import FIXTURE_DOCUMENTS

BASE = "https://app-docchat-api-load-abc123.azurewebsites.net"
TOKENS = {alias: f"tok-{alias}" for alias in (*LOAD_USERS, "load-admin")}


async def test_the_seed_builds_the_project_index_users_and_500_sessions(load_api: Any) -> None:
    applied_at = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    clock = FakeClock(applied_at + timedelta(seconds=10))

    result = await seed(
        BASE,
        TOKENS,
        ["Q1?", "Q2?"],
        applied_at,
        transport=load_api.transport,
        sleep=clock.sleep,
        now=clock.now,
        poll_seconds=0.0,
    )

    [project] = load_api.projects.values()
    assert (project["name"], project["isDefault"]) == ("Load test", True)
    [index] = load_api.indexes.values()
    assert index["displayName"] == INDEX_NAME
    assert index["methodKey"] == "pagewise"
    assert load_api.mappings == {(project["projectId"], index["indexId"]): True}
    assert sorted(v["name"] for v in load_api.versions.values()) == sorted(FIXTURE_DOCUMENTS)
    assert load_api.me_calls == [f"tok-{alias}" for alias in LOAD_USERS]
    assert result.sessions == SESSIONS == 500
    assert len(load_api.turns) == 500
    assert all(turn["sessionId"] is None for turn in load_api.turns)
    assert {turn["token"] for turn in load_api.turns} == {"tok-load-01"}
    # The sessions started no sooner than 70 s after the ops config was applied.
    assert clock.now() >= applied_at + CONFIG_SETTLE


async def test_a_session_failing_twice_fails_the_seed(load_api: Any) -> None:
    load_api.failing_questions = {"bad"}
    async with _client(load_api, "tok-load-01") as client:
        done = await seed_sessions(client, "p", ["bad", "Q?", "Q?"], count=3)
    assert done == 2
    assert [turn["question"] for turn in load_api.turns].count("bad") == 2
    load_api.outcomes = ["status_503"]
    async with _client(load_api, "tok-load-01") as client:
        assert await seed_sessions(client, "p", ["Q?"], count=1) == 1


async def test_the_seed_reports_sessions_that_failed(load_api: Any) -> None:
    load_api.failing_questions = {"bad"}
    clock = FakeClock(datetime(2026, 11, 3, 9, 5, tzinfo=UTC))
    with pytest.raises(LoadError, match="1 of the 500 sessions failed twice"):
        await seed(
            BASE,
            TOKENS,
            ["bad"] + ["Q?"] * 499,
            datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
            transport=load_api.transport,
            sleep=clock.sleep,
            now=clock.now,
            poll_seconds=0.0,
        )


async def test_an_existing_project_that_isnt_default_is_unusable(load_api: Any) -> None:
    load_api.projects["p1"] = {
        "projectId": "p1",
        "name": "Load test",
        "isDefault": False,
        "status": "active",
    }
    async with _client(load_api, "tok-load-admin") as client:
        with pytest.raises(UnusableResourceError):
            await ensure_default_project(AdminApi(client))
        load_api.projects["p1"]["isDefault"] = True
        assert (await ensure_default_project(AdminApi(client)))["projectId"] == "p1"


async def test_a_user_who_cant_sign_in_fails_the_seed() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(403, json={}))
    with pytest.raises(LoadError, match="getCurrentUser as load-01 answered 403"):
        await provision_users(BASE, TOKENS, transport)


async def test_a_static_token_is_returned_as_given() -> None:
    assert await static_token("abc")() == "abc"


def _client(api: Any, token: str) -> DeployedApiClient:
    return DeployedApiClient(BASE, static_token(token), transport=api.transport)
