"""The session, feedback and memory routes against SQL Server and the Cosmos DB emulator (T-303,
T-304, T-326, T-020, the route halves of T-318, T-321, T-322 and T-323, and these routes' entries
of the Cosmos DB data-access audit T-301; `13-conversations-and-memory.md` sections 4, 7 and 8).

The in-process API runs as the `docchat_api` role of the session's database with the permissive
limiters; its Cosmos DB containers are the session's emulator database, each wrapped in
`CosmosAudit`, whose check ends every test: `listSessions` is the one route allowed a query on
the `[userId]` prefix, and every other call names the caller's full key. Prompt Shields is the
chat doubles' endpoint and the fact tokens are counted one per code point. The shared principals
keep their partitions for the whole session, so each test works in fresh projects (a session of
an earlier test's project is unreachable, hence hidden) and starts from no memory document.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import httpx
import pytest
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from sqlalchemy import Engine, insert, select, update

from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES
from docchat_core.cosmos.conversations import TurnError, TurnStatus
from docchat_core.opsconfig import encode_value
from docchat_core.sql.engine import SqlEngine, SqlUnavailable
from docchat_core.sql.tables.access import (
    audit_events,
    ops_config,
    project_members,
    projects,
    users,
)
from docchat_testing.builders import project_member_row, project_row, user_row
from docchat_testing.builders.cosmos import (
    cosmos_now,
    fact,
    memory_document,
    memory_if_match,
    session_document,
    turn_document,
)
from docchat_testing.data_audit import CosmosAudit, CosmosFaults
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    BuiltApp,
    build_app,
    operation_map,
    ports_under_test,
    serving,
)
from docchat_testing.fixtures.cosmos import EmulatorContainers
from docchat_testing.fixtures.identities import PrincipalName, SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.telemetry_capture import TelemetryCapture
from docchat_testing.tokens import SigningKeys

NEW_TEXT: Final = "Works on supplier contract renewals"
ATTACK: Final = httpx.Response(
    200, json={"userPromptAnalysis": {"attackDetected": True}, "documentsAnalysis": []}
)
CUSTOM_EVENT: Final = "microsoft.custom_event.name"
CALLERS: Final[tuple[PrincipalName, ...]] = ("alice", "bob", "carol")


# --- The app -----------------------------------------------------------------------------------


@pytest.fixture
def admin(
    sql_database: SqlTestDatabase, principals: dict[PrincipalName, SharedPrincipal]
) -> Iterator[Engine]:
    engine = sql_database.engine()
    with engine.begin() as connection:
        for name in CALLERS:
            connection.execute(insert(users), user_row(object_id=principals[name].oid))
    yield engine
    engine.dispose()


@pytest.fixture
def audit() -> CosmosAudit:
    return CosmosAudit(operations=operation_map())


@pytest.fixture
def emulator(audit: CosmosAudit) -> EmulatorContainers:
    return EmulatorContainers(audit=audit)


def build(
    api_sql_engine: SqlEngine,
    signing_keys: SigningKeys,
    emulator: EmulatorContainers,
    chat_fakes: ChatFakes,
) -> BuiltApp:
    ports = replace(
        ports_under_test(sql_engine=api_sql_engine, chat=chat_fakes.ports()),
        container_wrapper=emulator,
    )
    return build_app(ports=ports, signing_keys=signing_keys)


@pytest.fixture
def built(
    api_sql_engine: SqlEngine,
    signing_keys: SigningKeys,
    emulator: EmulatorContainers,
    chat_fakes: ChatFakes,
) -> BuiltApp:
    return build(api_sql_engine, signing_keys, emulator, chat_fakes)


async def clear_memories(
    cosmos: DatabaseProxy, principals: dict[PrincipalName, SharedPrincipal]
) -> None:
    memories = cosmos.get_container_client(MEMORIES.name)
    for name in CALLERS:
        oid = str(principals[name].oid)
        with contextlib.suppress(CosmosResourceNotFoundError):
            await memories.delete_item(oid, oid)


@pytest.fixture
async def api(
    *,
    built: BuiltApp,
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    emulator: EmulatorContainers,
    audit: CosmosAudit,
    principals: dict[PrincipalName, SharedPrincipal],
    admin: Engine,
) -> AsyncIterator[ApiUnderTest]:
    emulator.database = cosmos_database
    await clear_memories(cosmos_database, principals)
    try:
        async with serving(built) as started:
            started.operations = operation_map()
            yield started
    finally:
        await api_sql_engine.dispose()
    audit.check()


# --- The world ---------------------------------------------------------------------------------


def member_project(admin: Engine, *members: SharedPrincipal) -> UUID:
    project = project_row()
    with admin.begin() as connection:
        connection.execute(insert(projects), project)
        for member in members:
            connection.execute(
                insert(project_members), project_member_row(member.oid, project["id"])
            )
    return UUID(str(project["id"]))


async def write_session(
    cosmos: DatabaseProxy,
    owner: SharedPrincipal,
    project: UUID,
    *,
    turns: int = 2,
    minutes: int = 0,
    **turn_changes: Any,
) -> tuple[UUID, list[UUID]]:
    """`owner`'s session in `project` with `turns` turns, its newest turn started `minutes`
    after now; the session and turn IDs."""
    conversations = cosmos.get_container_client(CONVERSATIONS.name)
    now = cosmos_now()
    session = session_document(
        user_id=owner.oid,
        project_id=project,
        turn_count=turns,
        created_at=now,
        last_turn_at=now + timedelta(minutes=minutes),
    )
    await conversations.upsert_item(session.to_item())
    turn_ids: list[UUID] = []
    for seq in range(1, turns + 1):
        turn = turn_document(
            user_id=owner.oid,
            session_id=session.session_id,
            project_id=project,
            seq=seq,
            **turn_changes,
        )
        await conversations.upsert_item(turn.to_item())
        turn_ids.append(turn.id)
    return session.session_id, turn_ids


async def read_turn(
    cosmos: DatabaseProxy, owner: SharedPrincipal, session: UUID, turn: UUID
) -> dict[str, Any]:
    conversations = cosmos.get_container_client(CONVERSATIONS.name)
    return dict(await conversations.read_item(str(turn), [str(owner.oid), str(session)]))


async def write_memory(cosmos: DatabaseProxy, owner: SharedPrincipal, *facts: Any) -> str:
    """`owner`'s memory document holding `facts`; its `etag`, as `If-Match` names it."""
    document = memory_document(user_id=owner.oid, facts=list(facts))
    written = await cosmos.get_container_client(MEMORIES.name).upsert_item(document.to_item())
    return memory_if_match(written)


async def read_memory(cosmos: DatabaseProxy, owner: SharedPrincipal) -> dict[str, Any] | None:
    try:
        stored = await cosmos.get_container_client(MEMORIES.name).read_item(
            str(owner.oid), str(owner.oid)
        )
    except CosmosResourceNotFoundError:
        return None
    return dict(stored)


def audit_actions(admin: Engine, owner: SharedPrincipal) -> list[str]:
    with admin.connect() as connection:
        rows = connection.execute(
            select(audit_events.c.action)
            .where(audit_events.c.subject_object_id == owner.oid)
            .order_by(audit_events.c.id)
        )
        return [str(row.action) for row in rows]


def problem(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def with_etag(principal: SharedPrincipal, etag: str | None) -> dict[str, str]:
    headers = principal.authorization()
    if etag is not None:
        headers["If-Match"] = etag
    return headers


def events(capture: TelemetryCapture, name: str, **match: str) -> list[dict[str, Any]]:
    found = []
    for record in capture.log_records():
        attributes = dict(record.log_record.attributes or {})
        if attributes.get(CUSTOM_EVENT) != name:
            continue
        if all(attributes.get(key) == value for key, value in match.items()):
            found.append({key: value for key, value in attributes.items() if key != CUSTOM_EVENT})
    return found


def metric_points(capture: TelemetryCapture, name: str) -> list[tuple[dict[str, Any], Any]]:
    data = capture.exporters.metrics.get_metrics_data()
    points: list[tuple[dict[str, Any], Any]] = []
    if data is None:
        return points
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    points += [
                        (dict(point.attributes or {}), getattr(point, "value", None))
                        for point in metric.data.data_points
                    ]
    return points


# --- Sessions (T-303, T-304) ---------------------------------------------------------------


async def test_t303_five_sessions_page_newest_first_and_the_filter_applies(
    *,
    api: ApiUnderTest,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    audit: CosmosAudit,
) -> None:
    project_a = member_project(admin, alice)
    project_b = member_project(admin, alice)
    sessions = [
        (await write_session(cosmos_database, alice, project_a, minutes=minutes))[0]
        for minutes in (1, 4, 2, 5, 3)
    ]
    in_b, _ = await write_session(cosmos_database, alice, project_b, minutes=9)

    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2, "projectId": str(project_a)}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get(
            "/v1/sessions", params=params, headers=alice.authorization()
        )
        assert response.status_code == 200, response.text
        pages.append([item["sessionId"] for item in response.json()["items"]])
        cursor = response.json()["nextCursor"]
        if cursor is None:
            break
    unfiltered = await api.client.get("/v1/sessions", headers=alice.authorization())

    newest_first = [sessions[3], sessions[1], sessions[4], sessions[2], sessions[0]]
    assert pages == [[str(s) for s in newest_first[i : i + 2]] for i in (0, 2, 4)]
    assert [item["sessionId"] for item in unfiltered.json()["items"]] == [
        str(in_b),
        *(str(s) for s in newest_first),
    ]
    prefix = [call for call in audit.calls if call.prefix_query]
    assert prefix
    assert all(call.context.operation == "listSessions" for call in prefix)


async def test_t303_list_session_turns_pages_oldest_first_within_a_page(
    *,
    api: ApiUnderTest,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    audit: CosmosAudit,
) -> None:
    project = member_project(admin, alice)
    session, turns = await write_session(cosmos_database, alice, project, turns=5)

    detail = await api.client.get(
        f"/v1/sessions/{session}", params={"turnsLimit": 2}, headers=alice.authorization()
    )
    earlier = await api.client.get(
        f"/v1/sessions/{session}/turns",
        params={"limit": 2, "cursor": detail.json()["earlierTurnsCursor"]},
        headers=alice.authorization(),
    )
    oldest = await api.client.get(
        f"/v1/sessions/{session}/turns",
        params={"limit": 2, "cursor": earlier.json()["nextCursor"]},
        headers=alice.authorization(),
    )

    assert [turn["turnId"] for turn in detail.json()["turns"]] == [str(t) for t in turns[3:]]
    assert [turn["turnId"] for turn in earlier.json()["items"]] == [str(t) for t in turns[1:3]]
    assert [turn["turnId"] for turn in oldest.json()["items"]] == [str(turns[0])]
    assert oldest.json()["nextCursor"] is None
    reads = [call for call in audit.calls if call.context.operation == "getSession"]
    assert reads
    assert all(call.full_key for call in reads)


async def test_t304_two_thousand_reachable_projects_list_only_reachable_sessions(
    *,
    api: ApiUnderTest,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
) -> None:
    rows = [project_row() for _ in range(2_000)]

    def seed() -> UUID:
        # 4,000 inserts take longer than the loop guard allows, so they run in a thread.
        with admin.begin() as connection:
            connection.execute(insert(projects), rows)
            connection.execute(
                insert(project_members),
                [project_member_row(alice.oid, row["id"]) for row in rows],
            )
        return member_project(admin)

    unreachable = await asyncio.to_thread(seed)
    reachable_ids = [UUID(str(row["id"])) for row in rows]
    listed_sessions = [
        (await write_session(cosmos_database, alice, project, turns=1))[0]
        for project in (reachable_ids[0], reachable_ids[999], reachable_ids[-1])
    ]
    await write_session(cosmos_database, alice, unreachable, turns=1)

    response = await api.client.get(
        "/v1/sessions", params={"limit": 50}, headers=alice.authorization()
    )

    assert response.status_code == 200, response.text
    assert {item["sessionId"] for item in response.json()["items"]} == {
        str(session) for session in listed_sessions
    }


async def test_t301_hidden_and_foreign_sessions_read_only_the_callers_partition(
    *,
    api: ApiUnderTest,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
    audit: CosmosAudit,
) -> None:
    project = member_project(admin, alice, bob)
    bobs, _ = await write_session(cosmos_database, bob, project)
    responses = [
        await api.client.get(f"/v1/sessions/{bobs}{suffix}", headers=alice.authorization())
        for suffix in ("", "/turns")
    ]

    assert [problem(response) for response in responses] == [(404, "session_not_found")] * 2
    assert audit.user_ids() <= {str(alice.oid)}


# --- Feedback (T-326) --------------------------------------------------------------------------


async def test_t326_up_down_and_a_change_that_keeps_created_at(
    *,
    api: ApiUnderTest,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    telemetry_capture: TelemetryCapture,
    audit: CosmosAudit,
) -> None:
    project = member_project(admin, alice)
    session, (turn,) = await write_session(cosmos_database, alice, project, turns=1)
    url = f"/v1/sessions/{session}/turns/{turn}/feedback"

    up = await api.client.put(url, json={"rating": "up"}, headers=alice.authorization())
    first = (await read_turn(cosmos_database, alice, session, turn))["feedback"]
    await asyncio.sleep(0.01)
    down = await api.client.put(
        url,
        json={"rating": "down", "category": "incomplete", "comment": "Skips a step"},
        headers=alice.authorization(),
    )
    stored = (await read_turn(cosmos_database, alice, session, turn))["feedback"]

    assert up.status_code == 200, up.text
    assert down.json()["category"] == "incomplete"
    assert stored["createdAt"] == first["createdAt"]
    assert stored["updatedAt"] == down.json()["updatedAt"]
    saved = events(telemetry_capture, "turn_feedback_saved", turnId=str(turn))
    assert len(saved) == 2
    assert set(saved[1]) == {
        "rating",
        "category",
        "projectId",
        "turnId",
        "sessionId",
        "isChange",
        "hasComment",
    }
    assert (saved[1]["isChange"], saved[1]["hasComment"]) == (True, True)
    points = metric_points(telemetry_capture, "docchat.feedback.saved")
    assert points
    assert all(set(attributes) == {"rating", "category"} for attributes, _ in points)
    assert all(call.full_key for call in audit.calls if call.context.operation == "putTurnFeedback")
    assert audit_actions(admin, alice) == []


async def test_t326_the_category_rule_and_the_comment_limit(
    *, api: ApiUnderTest, admin: Engine, cosmos_database: DatabaseProxy, alice: SharedPrincipal
) -> None:
    project = member_project(admin, alice)
    session, (turn,) = await write_session(cosmos_database, alice, project, turns=1)
    url = f"/v1/sessions/{session}/turns/{turn}/feedback"

    category = await api.client.put(
        url, json={"rating": "up", "category": "other"}, headers=alice.authorization()
    )
    long_comment = await api.client.put(
        url, json={"rating": "down", "comment": "x" * 2_001}, headers=alice.authorization()
    )

    assert category.json()["errors"] == [
        {"path": "body.category", "reason": "category_requires_down"}
    ]
    assert long_comment.json()["errors"] == [{"path": "body.comment", "reason": "too_long"}]
    assert (await read_turn(cosmos_database, alice, session, turn))["feedback"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"status": TurnStatus.STREAMING, "answer": "", "finish_reason": None, "ended_at": None},
        {
            "status": TurnStatus.FAILED,
            "answer": "",
            "error": TurnError(code="model_unavailable", retryable=True),
        },
    ],
    ids=["streaming", "failed"],
)
async def test_t326_streaming_and_failed_turns_refuse_feedback(
    *,
    api: ApiUnderTest,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    changes: dict[str, Any],
) -> None:
    project = member_project(admin, alice)
    session, (turn,) = await write_session(cosmos_database, alice, project, turns=1, **changes)

    response = await api.client.put(
        f"/v1/sessions/{session}/turns/{turn}/feedback",
        json={"rating": "up"},
        headers=alice.authorization(),
    )

    assert problem(response) == (409, "feedback_not_allowed")


async def test_t326_feedback_and_followups_patched_together_both_persist(
    *, api: ApiUnderTest, admin: Engine, cosmos_database: DatabaseProxy, alice: SharedPrincipal
) -> None:
    project = member_project(admin, alice)
    session, (turn,) = await write_session(cosmos_database, alice, project, turns=1)
    conversations = cosmos_database.get_container_client(CONVERSATIONS.name)

    async def followups() -> None:
        # As `attach_followups` writes them: a patch with no ETag condition.
        await conversations.patch_item(
            str(turn),
            [str(alice.oid), str(session)],
            [{"op": "set", "path": "/followups", "value": ["What about hotels?"]}],
        )

    feedback, _ = await asyncio.gather(
        api.client.put(
            f"/v1/sessions/{session}/turns/{turn}/feedback",
            json={"rating": "up"},
            headers=alice.authorization(),
        ),
        followups(),
    )

    stored = await read_turn(cosmos_database, alice, session, turn)
    assert feedback.status_code == 200, feedback.text
    assert stored["followups"] == ["What about hotels?"]
    assert stored["feedback"]["rating"] == "up"


@pytest.fixture
def faults() -> EmulatorContainers:
    """The emulator's containers with every `conversations` call failing as a lost
    connection."""
    return EmulatorContainers(audit=CosmosFaults(CONVERSATIONS.name))


@pytest.fixture
def faulty_built(
    api_sql_engine: SqlEngine,
    signing_keys: SigningKeys,
    faults: EmulatorContainers,
    chat_fakes: ChatFakes,
) -> BuiltApp:
    return build(api_sql_engine, signing_keys, faults, chat_fakes)


async def test_t326_an_injected_cosmos_failure_answers_503_with_nothing_stored(
    *,
    faulty_built: BuiltApp,
    faults: EmulatorContainers,
    api_sql_engine: SqlEngine,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
) -> None:
    project = member_project(admin, alice)
    session, (turn,) = await write_session(cosmos_database, alice, project, turns=1)
    faults.database = cosmos_database

    try:
        async with serving(faulty_built) as faulty:
            response = await faulty.client.put(
                f"/v1/sessions/{session}/turns/{turn}/feedback",
                json={"rating": "up"},
                headers=alice.authorization(),
            )
    finally:
        await api_sql_engine.dispose()

    assert problem(response) == (503, "dependency_unavailable")
    assert (await read_turn(cosmos_database, alice, session, turn))["feedback"] is None


# --- Memory (T-318, T-321, T-322, T-323, T-020) ------------------------------------------------


async def test_t318_an_attack_on_edited_text_is_contains_instruction_with_nothing_changed(
    *,
    api: ApiUnderTest,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    chat_fakes: ChatFakes,
) -> None:
    edited = fact()
    etag = await write_memory(cosmos_database, alice, edited)
    before = await read_memory(cosmos_database, alice)
    chat_fakes.shields.queued.append(ATTACK)

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": NEW_TEXT, "category": "topic"},
        headers=with_etag(alice, etag),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.text", "reason": "contains_instruction"}]
    assert await read_memory(cosmos_database, alice) == before
    assert audit_actions(admin, alice) == []


async def test_t318_two_screening_failures_answer_503_and_a_category_change_isnt_screened(
    *,
    api: ApiUnderTest,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    chat_fakes: ChatFakes,
) -> None:
    edited = fact(text=NEW_TEXT)
    etag = await write_memory(cosmos_database, alice, edited)
    chat_fakes.shields.queued.extend([httpx.Response(503), httpx.Response(503)])

    unavailable = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": "Works on supplier contract audits", "category": "topic"},
        headers=with_etag(alice, etag),
    )
    screened = len(chat_fakes.shields.requests)
    recategorised = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": NEW_TEXT, "category": "expertise"},
        headers=with_etag(alice, etag),
    )

    assert problem(unavailable) == (503, "dependency_unavailable")
    assert screened == 2
    assert recategorised.status_code == 200, recategorised.text
    assert len(chat_fakes.shields.requests) == screened


@pytest.fixture
def memory_off(admin: Engine) -> None:
    """`memory.enabled` false in `app.ops_config` before the app loads its snapshot; the SQL
    fixture's clean-up restores the seeded rows after the test."""
    with admin.begin() as connection:
        connection.execute(
            update(ops_config)
            .where(ops_config.c.config_key == "memory.enabled")
            .values(value_json=encode_value(False))
        )


async def test_t321_memory_off_for_everyone_serves_every_operation(
    *,
    memory_off: None,
    built: BuiltApp,
    api_sql_engine: SqlEngine,
    emulator: EmulatorContainers,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    principals: dict[PrincipalName, SharedPrincipal],
) -> None:
    emulator.database = cosmos_database
    await clear_memories(cosmos_database, principals)
    edited = fact()
    etag = await write_memory(cosmos_database, alice, edited)

    try:
        async with serving(built) as off:
            read = await off.client.get("/v1/memory", headers=alice.authorization())
            paused = await off.client.patch(
                "/v1/memory", json={"paused": True}, headers=with_etag(alice, etag)
            )
            updated = await off.client.put(
                f"/v1/memory/facts/{edited.id}",
                json={"text": NEW_TEXT, "category": "topic"},
                headers=with_etag(alice, paused.json()["etag"]),
            )
            deleted = await off.client.delete(
                f"/v1/memory/facts/{edited.id}", headers=with_etag(alice, updated.json()["etag"])
            )
    finally:
        await api_sql_engine.dispose()

    assert read.json()["enabled"] is False
    assert [response.status_code for response in (paused, updated, deleted)] == [200] * 3
    assert deleted.json()["enabled"] is False


async def test_t322_two_edits_with_one_etag_end_one_200_and_one_412(
    *, api: ApiUnderTest, admin: Engine, cosmos_database: DatabaseProxy, alice: SharedPrincipal
) -> None:
    edited = fact()
    etag = await write_memory(cosmos_database, alice, edited)

    first, second = await asyncio.gather(
        *(
            api.client.put(
                f"/v1/memory/facts/{edited.id}",
                json={"text": text, "category": "topic"},
                headers=with_etag(alice, etag),
            )
            for text in (NEW_TEXT, "Works on facilities budgets")
        )
    )

    assert sorted([first.status_code, second.status_code]) == [200, 412]
    assert audit_actions(admin, alice) == ["memory.fact_updated"]


async def test_t322_a_fact_deleted_in_another_tab_is_404_and_no_document_is_412(
    *, api: ApiUnderTest, admin: Engine, cosmos_database: DatabaseProxy, alice: SharedPrincipal
) -> None:
    gone, kept = fact(), fact()
    etag = await write_memory(cosmos_database, alice, gone, kept)
    deleted = await api.client.delete(f"/v1/memory/facts/{gone.id}", headers=with_etag(alice, etag))
    assert deleted.status_code == 200, deleted.text
    after_delete = await read_memory(cosmos_database, alice)

    stale = [
        await api.client.delete(f"/v1/memory/facts/{gone.id}", headers=with_etag(alice, value))
        for value in (etag, None)
    ]
    stale += [
        await api.client.put(
            f"/v1/memory/facts/{gone.id}",
            json={"text": NEW_TEXT, "category": "topic"},
            headers=with_etag(alice, value),
        )
        for value in (etag, None)
    ]
    await cosmos_database.get_container_client(MEMORIES.name).delete_item(
        str(alice.oid), str(alice.oid)
    )
    cleared = await api.client.delete("/v1/memory/facts", headers=with_etag(alice, etag))

    assert [problem(response) for response in stale] == [(404, "memory_fact_not_found")] * 4
    assert problem(cleared) == (412, "precondition_failed")
    assert after_delete is not None
    assert audit_actions(admin, alice) == ["memory.fact_deleted"]
    assert await read_memory(cosmos_database, alice) is None


async def test_t323_an_edit_above_the_token_cap_is_memory_full(
    *, api: ApiUnderTest, cosmos_database: DatabaseProxy, alice: SharedPrincipal
) -> None:
    edited = fact(tokens=1)
    etag = await write_memory(cosmos_database, alice, edited, fact(tokens=990))

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": "Works with the procurement team", "category": "team"},
        headers=with_etag(alice, etag),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.text", "reason": "memory_full"}]


def failing_audit(engine: SqlEngine) -> Callable[..., Awaitable[Any]]:
    """`engine.run` with the audit insert failing as SQL unavailable on every attempt."""
    run = engine.run

    async def patched(work: Callable[[Any], Any], *, name: str, retry: Any) -> Any:
        if name == "write_audit_event":
            raise SqlUnavailable("connection", 40613, "")
        return await run(work, name=name, retry=retry)

    return patched


async def test_t020_sql_unavailable_during_a_memory_edit_still_answers_200(
    *,
    api: ApiUnderTest,
    api_sql_engine: SqlEngine,
    admin: Engine,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    telemetry_capture: TelemetryCapture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    etag = await write_memory(cosmos_database, alice, fact())
    before = sum(
        value or 0
        for attributes, value in metric_points(telemetry_capture, "docchat.audit.write_failures")
        if attributes.get("action") == "memory.paused"
    )
    monkeypatch.setattr(api_sql_engine, "run", failing_audit(api_sql_engine))

    response = await api.client.patch(
        "/v1/memory", json={"paused": True}, headers=with_etag(alice, etag)
    )

    assert response.status_code == 200, response.text
    stored = await read_memory(cosmos_database, alice)
    assert stored is not None
    assert stored["paused"] is True
    failed = events(telemetry_capture, "audit_write_failed", targetId=str(alice.oid))
    assert [event["action"] for event in failed] == ["memory.paused"]
    after = sum(
        value or 0
        for attributes, value in metric_points(telemetry_capture, "docchat.audit.write_failures")
        if attributes.get("action") == "memory.paused"
    )
    assert after - before == 1
    assert audit_actions(admin, alice) == []


async def test_t301_memory_operations_name_only_the_callers_partition(
    *,
    api: ApiUnderTest,
    cosmos_database: DatabaseProxy,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
    audit: CosmosAudit,
) -> None:
    await write_memory(cosmos_database, bob, fact())
    read = await api.client.get("/v1/memory", headers=alice.authorization())
    etag = read.json()["etag"]
    await api.client.patch("/v1/memory", json={"paused": True}, headers=with_etag(alice, etag))

    memory_calls = [call for call in audit.calls if call.container == MEMORIES.name]
    assert memory_calls
    assert all(call.partition_key == (str(alice.oid),) for call in memory_calls)
    stored = await read_memory(cosmos_database, alice)
    assert stored is not None
    assert stored["pausedAt"] == stored["userEditedAt"]
