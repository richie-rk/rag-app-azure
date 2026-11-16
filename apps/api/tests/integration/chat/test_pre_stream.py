"""T-061, the chat pre-stream integration tests (`11-api.md` sections 1.3, 1.12 and 3.3.1 and
acceptance criterion 18; `12-chat-pipeline.md` sections 2.2, 2.3 and 4.3).

Each test acts as a fresh user (a token whose `oid` is new, a member of project A), so the usage
documents it lays out in the emulator's `usage` container are its own: a full request window, a
reached day quota or a held stream lease. Two refusal conditions laid out together show which
chat step answers first. The app runs with one stream slot per user, so a lease left behind by a
refused request would refuse the next turn with `429 too_many_streams`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from docchat_core.cosmos.containers import USAGE
from docchat_core.cosmos.conversations import ActiveTurn, TurnError, TurnStatus, turn_id
from docchat_core.cosmos.usage import epoch_minute, rate_window_id, stream_lease_id
from docchat_core.sql.tables.access import project_members, users
from docchat_testing.builders import project_member_row, user_row
from docchat_testing.builders.cosmos import (
    cosmos_now,
    rate_window,
    session_document,
    stream_lease,
    turn_document,
    usage_day_counter,
)
from docchat_testing.chatflow import QUESTION, ChatFlow, stream_events
from docchat_testing.isolation import seed_project

CHAT_LIMIT: Final = 1_200
"""`ratelimit.chat_requests_per_minute` as the chat suites raise it."""
DAY_QUOTA: Final = 2_000_000
"""`quota.tokens_per_day` at its default."""


@pytest.fixture
def chat_ops() -> dict[str, Any]:
    return {"ratelimit.max_concurrent_streams": 1}


@dataclass(frozen=True)
class Caller:
    """A fresh user, a member of project A."""

    oid: UUID
    headers: dict[str, str]


@pytest.fixture
def caller(flow: ChatFlow) -> Caller:
    oid = uuid4()
    flow.world.put(
        (users, user_row(object_id=oid)),
        (project_members, project_member_row(oid, flow.world.ids["A"])),
    )
    return Caller(oid=oid, headers=flow.world.principals["alice"].authorization(oid=str(oid)))


class Usage:
    """The fresh user's partition of the emulator's `usage` container."""

    def __init__(self, cosmos: DatabaseProxy, oid: UUID) -> None:
        self.container = cosmos.get_container_client(USAGE.name)
        self.oid = oid

    async def full_window(self) -> None:
        """This minute's and the next minute's windows at the limit, so the estimate refuses
        the next request even when the minute turns during the test."""
        minute = epoch_minute(cosmos_now())
        for each in (minute, minute + 1):
            window = rate_window(user_id=self.oid, id=rate_window_id(each), count=CHAT_LIMIT)
            await self.container.upsert_item(window.to_item())

    async def quota_reached(self) -> None:
        counter = usage_day_counter(user_id=self.oid, total_tokens=DAY_QUOTA)
        await self.container.upsert_item(counter.to_item())

    async def slot_taken(self) -> None:
        await self.container.upsert_item(stream_lease(user_id=self.oid, slot=1).to_item())

    async def lease_held(self) -> bool:
        try:
            await self.container.read_item(stream_lease_id(1), str(self.oid))
        except CosmosResourceNotFoundError:
            return False
        return True


@pytest.fixture
def usage(cosmos_database: DatabaseProxy, caller: Caller) -> Usage:
    return Usage(cosmos_database, caller.oid)


def code_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


# --- The chat order of section 1.3 ----------------------------------------------------------------


async def test_t061_body_validation_answers_before_the_request_window(
    flow: ChatFlow, caller: Caller, usage: Usage
) -> None:
    await usage.full_window()

    response = await flow.ask(headers=caller.headers, question="   ")

    assert code_of(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.question", "reason": "empty"}]


async def test_t061_the_request_window_answers_before_the_quota(
    flow: ChatFlow, caller: Caller, usage: Usage
) -> None:
    await usage.full_window()
    await usage.quota_reached()

    response = await flow.ask(headers=caller.headers)

    assert code_of(response) == (429, "rate_limited")
    assert 1 <= response.json()["retryAfterSeconds"] <= 120
    assert response.headers["retry-after"] == str(response.json()["retryAfterSeconds"])


async def test_t061_the_quota_answers_before_the_stream_lease(
    flow: ChatFlow, caller: Caller, usage: Usage
) -> None:
    await usage.quota_reached()
    await usage.slot_taken()

    response = await flow.ask(headers=caller.headers)

    assert code_of(response) == (429, "token_quota_exceeded")
    assert response.headers["retry-after"] == str(response.json()["retryAfterSeconds"])


async def test_t061_the_stream_lease_answers_before_project_access(
    flow: ChatFlow, caller: Caller, usage: Usage
) -> None:
    seed_project(flow.world, "B")
    await usage.slot_taken()

    response = await flow.ask(headers=caller.headers, project="B")

    assert code_of(response) == (429, "too_many_streams")
    assert response.json()["retryAfterSeconds"] >= 1


async def test_t061_project_access_answers_before_index_resolution_and_frees_the_lease(
    flow: ChatFlow, caller: Caller, usage: Usage
) -> None:
    # B has no index and the caller isn't a member: the membership check comes first.
    seed_project(flow.world, "B")

    refused = await flow.ask(headers=caller.headers, project="B")

    assert code_of(refused) == (404, "project_not_found")
    assert not await usage.lease_held()
    assert (await stream_events(await flow.ask(headers=caller.headers)))[-1]["type"] == "done"


async def test_t061_index_resolution_answers_before_the_session_and_frees_the_lease(
    flow: ChatFlow, caller: Caller, usage: Usage
) -> None:
    seed_project(flow.world, "N")
    flow.world.put((project_members, project_member_row(caller.oid, flow.world.ids["N"])))

    refused = await flow.ask(headers=caller.headers, project="N", session=uuid4())

    assert code_of(refused) == (409, "project_index_unavailable")
    assert not await usage.lease_held()
    assert (await stream_events(await flow.ask(headers=caller.headers)))[-1]["type"] == "done"


async def test_t061_an_unknown_session_is_refused_last_and_frees_the_lease(
    flow: ChatFlow, caller: Caller, usage: Usage
) -> None:
    refused = await flow.ask(headers=caller.headers, session=uuid4())

    assert code_of(refused) == (404, "session_not_found")
    assert not await usage.lease_held()
    assert (await stream_events(await flow.ask(headers=caller.headers)))[-1]["type"] == "done"


# --- Duplicates of each turn status (section 1.12) ------------------------------------------------


async def _seeded_turn(
    flow: ChatFlow, caller: Caller, status: TurnStatus, *, deadline: timedelta
) -> tuple[UUID, UUID, UUID]:
    """A session of the caller in A holding one turn of `status` whose deadline is `deadline`
    from now (negative: passed); its session ID, client turn ID and turn ID."""
    session_id, client_turn_id = uuid4(), uuid4()
    now = cosmos_now()
    deadline_at = now + deadline
    ended = status is not TurnStatus.STREAMING
    turn = turn_document(
        user_id=caller.oid,
        session_id=session_id,
        client_turn_id=client_turn_id,
        project_id=flow.world.ids["A"],
        status=status,
        question=QUESTION,
        answer="" if status is not TurnStatus.CANCELLED else "Partly answered",
        finish_reason=None,
        error=TurnError(code="model_unavailable", retryable=True)
        if status is TurnStatus.FAILED
        else None,
        deadline_at=deadline_at,
        created_at=now - timedelta(minutes=1),
        ended_at=now if ended else None,
        updated_at=now,
    )
    session = session_document(
        user_id=caller.oid,
        session_id=session_id,
        project_id=flow.world.ids["A"],
        active_turn=None if ended else ActiveTurn(turn_id=turn.id, deadline_at=deadline_at),
    )
    await flow.conversations.upsert_item(session.to_item())
    await flow.conversations.upsert_item(turn.to_item())
    return session_id, client_turn_id, turn.id


def _carries_the_turn(response: Any, session_id: UUID, turn: UUID) -> None:
    body = response.json()
    assert (body["sessionId"], body["turnId"]) == (str(session_id), str(turn))


async def test_t061_a_duplicate_of_a_completed_turn_replays_it(
    flow: ChatFlow, caller: Caller
) -> None:
    client_turn_id = uuid4()
    first = await stream_events(
        await flow.ask(headers=caller.headers, client_turn_id=client_turn_id)
    )

    again = await stream_events(
        await flow.ask(
            headers=caller.headers, session=first[0]["sessionId"], client_turn_id=client_turn_id
        )
    )

    assert again[0]["replayed"] is True
    assert (again[0]["sessionId"], again[0]["turnId"]) == (
        first[0]["sessionId"],
        first[0]["turnId"],
    )
    assert again[-1]["type"] == "done"


async def test_t061_a_duplicate_of_a_streaming_turn_is_in_progress(
    flow: ChatFlow, caller: Caller
) -> None:
    session_id, client_turn_id, turn = await _seeded_turn(
        flow, caller, TurnStatus.STREAMING, deadline=timedelta(minutes=3)
    )

    response = await flow.ask(
        headers=caller.headers, session=session_id, client_turn_id=client_turn_id
    )

    assert code_of(response) == (409, "turn_in_progress")
    _carries_the_turn(response, session_id, turn)
    assert 1 <= response.json()["retryAfterSeconds"] <= 210
    assert response.headers["retry-after"] == str(response.json()["retryAfterSeconds"])


@pytest.mark.parametrize(
    ("status", "deadline"),
    [
        (TurnStatus.STREAMING, timedelta(minutes=-1)),
        (TurnStatus.FAILED, timedelta(minutes=3)),
        (TurnStatus.CANCELLED, timedelta(minutes=3)),
    ],
    ids=["abandoned", "failed", "cancelled"],
)
async def test_t061_a_duplicate_of_an_ended_turn_is_already_ended(
    flow: ChatFlow, caller: Caller, status: TurnStatus, deadline: timedelta
) -> None:
    session_id, client_turn_id, turn = await _seeded_turn(flow, caller, status, deadline=deadline)

    response = await flow.ask(
        headers=caller.headers, session=session_id, client_turn_id=client_turn_id
    )

    assert code_of(response) == (409, "turn_already_ended")
    _carries_the_turn(response, session_id, turn)
    assert "retryAfterSeconds" not in response.json()
    if status is TurnStatus.STREAMING:
        stored = await flow.conversations.read_item(str(turn), [str(caller.oid), str(session_id)])
        assert (stored["status"], stored["error"]["code"]) == ("failed", "turn_abandoned")


async def test_t061_a_duplicate_with_another_question_conflicts(
    flow: ChatFlow, caller: Caller
) -> None:
    client_turn_id = uuid4()
    first = await stream_events(
        await flow.ask(headers=caller.headers, client_turn_id=client_turn_id)
    )

    response = await flow.ask(
        headers=caller.headers,
        session=first[0]["sessionId"],
        client_turn_id=client_turn_id,
        question="A different question?",
    )

    assert code_of(response) == (409, "client_turn_id_conflict")
    assert response.json()["sessionId"] == first[0]["sessionId"]
    assert response.json()["turnId"] == str(turn_id(UUID(first[0]["sessionId"]), client_turn_id))


# --- The question's own rules ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("", "empty"),
        (" \u200b\u2060 \r\n ", "empty"),
        ("q" * 4_001, "too_long"),
        ("Which claim\u0000 is late?", "invalid_characters"),
        ("Bell \u0007 rings?", "invalid_characters"),
    ],
    ids=["empty", "format_only", "too_long", "nul", "control"],
)
async def test_t061_a_refused_question_answers_422_at_body_question(
    flow: ChatFlow, caller: Caller, question: str, reason: str
) -> None:
    response = await flow.ask(headers=caller.headers, question=question)

    assert code_of(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.question", "reason": reason}]
