"""Unit tests of `listSessions`, `getSession` and `listSessionTurns` in process (T-302, T-305,
the in-process halves of T-303 and T-306; `13-conversations-and-memory.md` sections 4 and 5;
`11-api.md` sections 1.8 and 3.4).

The app runs over `FakeContainer`s and the fake data-access layer of `conftest.py`, whose
`AccessScript` answers the reachability queries; the sessions and turns are seeded with the
Cosmos DB builders, so every stored field passes the document models.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, get_args
from uuid import UUID, uuid4

import httpx
import pytest
from azure.core.exceptions import ServiceRequestError
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.chat.stream import STREAM_ERROR_CODES
from docchat_api.conversations import session_routes
from docchat_api.conversations.session_routes import TurnErrorCode, TurnView, turn_view
from docchat_api.conversations.store import SESSIONS_AFTER_QUERY, SESSIONS_QUERY
from docchat_api.http.paging import encode_cursor
from docchat_api.http.routing import DocChatRoute
from docchat_core.cosmos.conversations import (
    Feedback,
    FeedbackCategory,
    FeedbackRating,
    TurnError,
    TurnStatus,
)
from docchat_core.models import format_utc
from docchat_core.sql.engine import SqlUnavailable
from docchat_testing.builders.cosmos import session_document, stored_citation, turn_document
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

START: Final = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
"""The start of the conftest's clock."""

PROJECT_A: Final = UUID("a0000000-0000-4000-8000-00000000000a")
PROJECT_B: Final = UUID("b0000000-0000-4000-8000-00000000000b")
OPERATIONS: Final = ("listSessions", "getSession", "listSessionTurns")
TURN_VIEW_KEYS: Final = {
    "turnId",
    "seq",
    "status",
    "question",
    "answer",
    "answerTruncated",
    "errorCode",
    "citations",
    "allowedLinks",
    "followups",
    "feedback",
    "createdAt",
    "endedAt",
}


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


class AccessScript(Protocol):
    """The conftest's answers of the reachability queries."""

    archived: set[UUID]
    failures: dict[str, BaseException]

    def grant(self, project: UUID, *users: UUID) -> None: ...

    def revoke(self, project: UUID, user: UUID) -> None: ...


def seed_session(
    containers: dict[str, FakeContainer],
    user: UUID,
    *,
    project: UUID = PROJECT_A,
    turns: int = 2,
    last_turn_minutes: int = 0,
    session_id: UUID | None = None,
    **turn_changes: Any,
) -> tuple[UUID, list[dict[str, Any]]]:
    """A session of `user` with `turns` completed turns, `seq` 1 to `turns`."""
    session = uuid4() if session_id is None else session_id
    last_turn_at = START + timedelta(minutes=last_turn_minutes)
    containers["conversations"].put(
        session_document(
            user_id=user,
            session_id=session,
            project_id=project,
            created_at=START,
            last_turn_at=last_turn_at,
            turn_count=turns,
        ).to_item()
    )
    stored = []
    for seq in range(1, turns + 1):
        changes: dict[str, Any] = {
            "project_id": project,
            "seq": seq,
            "created_at": START,
            "ended_at": START,
        }
        stored.append(
            containers["conversations"].put(
                turn_document(
                    user_id=user, session_id=session, **(changes | turn_changes)
                ).to_item()
            )
        )
    return session, stored


def problem(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def body_without_correlation(response: httpx.Response) -> dict[str, Any]:
    body = dict(response.json())
    body.pop("correlationId")
    return body


def cursor_text(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --- Routing -----------------------------------------------------------------------------------


def test_t306_the_three_reads_are_burst_limited_routes_of_the_route_class() -> None:
    routes = {
        route.operation_id: route
        for route in session_routes.router.routes
        if isinstance(route, DocChatRoute)
    }
    assert set(routes) == set(OPERATIONS)
    for route in routes.values():
        assert route.route_options.burst_limited is True
        assert route.route_options.write_limited is False
        assert route.access_spec.level == "user"
        assert route.tags == ["sessions"]
    assert routes["listSessions"].path == "/sessions"
    assert routes["listSessions"].access_spec.scoped_params == ("projectId",)
    assert routes["getSession"].path == "/sessions/{sessionId}"
    assert routes["listSessionTurns"].path == "/sessions/{sessionId}/turns"
    assert routes["getSession"].access_spec.scoped_params == ("sessionId",)


def test_the_error_code_enumeration_is_the_stream_codes_and_turn_abandoned() -> None:
    assert set(get_args(TurnErrorCode)) == STREAM_ERROR_CODES | {"turn_abandoned"}


# --- listSessions ------------------------------------------------------------------------------


async def test_t303_five_sessions_with_limit_two_page_newest_first(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    # Two sessions share a lastTurnAt, so the sessionId breaks the tie.
    minutes = [1, 3, 3, 4, 2]
    sessions = [seed_session(containers, alice.oid, last_turn_minutes=m)[0] for m in minutes]
    expected = sorted(
        zip(minutes, sessions, strict=True), key=lambda pair: (pair[0], str(pair[1])), reverse=True
    )

    seen: list[str] = []
    pages = 0
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get(
            "/v1/sessions", params=params, headers=alice.authorization()
        )
        assert response.status_code == 200, response.text
        body = response.json()
        pages += 1
        seen += [item["sessionId"] for item in body["items"]]
        cursor = body["nextCursor"]
        if cursor is None:
            break

    assert pages == 3
    assert seen == [str(session) for _, session in expected]
    queries = containers["conversations"].calls_of("query_items")
    assert [call.query for call in queries] == [
        SESSIONS_QUERY,
        SESSIONS_AFTER_QUERY,
        SESSIONS_AFTER_QUERY,
    ]
    assert all(call.partition_key == (str(alice.oid),) for call in queries)
    assert queries[0].kwargs["timeout"] == 5


async def test_a_summary_holds_exactly_the_list_fields(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid, turns=3, last_turn_minutes=5)

    response = await api.client.get("/v1/sessions", headers=alice.authorization())

    (item,) = response.json()["items"]
    assert set(item) == {"sessionId", "projectId", "title", "createdAt", "lastTurnAt", "turnCount"}
    assert item["sessionId"] == str(session)
    assert item["projectId"] == str(PROJECT_A)
    assert item["turnCount"] == 3
    assert item["lastTurnAt"] == format_utc(START + timedelta(minutes=5))
    assert response.headers["cache-control"] == "no-store"


async def test_the_list_holds_only_the_callers_sessions_in_reachable_projects(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid, bob.oid)
    access_script.grant(PROJECT_B, bob.oid)
    own, _ = seed_session(containers, alice.oid)
    hidden, _ = seed_session(containers, alice.oid, project=PROJECT_B)
    seed_session(containers, bob.oid)
    seed_session(containers, bob.oid)

    listed = await api.client.get("/v1/sessions", headers=alice.authorization())
    filtered = await api.client.get(
        "/v1/sessions", params={"projectId": str(PROJECT_A)}, headers=alice.authorization()
    )

    for response in (listed, filtered):
        assert [item["sessionId"] for item in response.json()["items"]] == [str(own)]
    query = containers["conversations"].calls_of("query_items")[0]
    parameters = {item["name"]: item["value"] for item in query.kwargs["parameters"]}
    assert parameters == {
        "@take": 21,
        "@userId": str(alice.oid),
        "@type": "session",
        "@projectIds": [str(PROJECT_A)],
    }
    assert hidden not in {item["sessionId"] for item in listed.json()["items"]}


async def test_a_revoked_project_hides_its_sessions_and_a_restore_shows_them(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    access_script.grant(PROJECT_B, alice.oid)
    session, _ = seed_session(containers, alice.oid)

    access_script.revoke(PROJECT_A, alice.oid)
    revoked = await api.client.get("/v1/sessions", headers=alice.authorization())
    access_script.grant(PROJECT_A, alice.oid)
    restored = await api.client.get("/v1/sessions", headers=alice.authorization())

    assert revoked.json() == {"items": [], "nextCursor": None}
    assert [item["sessionId"] for item in restored.json()["items"]] == [str(session)]


async def test_no_reachable_project_answers_an_empty_page_without_cosmos(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    response = await api.client.get("/v1/sessions", headers=alice.authorization())

    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}
    assert containers["conversations"].calls_of("query_items") == []


async def test_an_unreachable_project_filter_is_project_not_found_like_a_random_one(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    access_script.grant(PROJECT_B, bob.oid)

    other = await api.client.get(
        "/v1/sessions", params={"projectId": str(PROJECT_B)}, headers=alice.authorization()
    )
    random = await api.client.get(
        "/v1/sessions", params={"projectId": str(uuid4())}, headers=alice.authorization()
    )

    assert problem(other) == (404, "project_not_found")
    assert body_without_correlation(other) == body_without_correlation(random)
    assert containers["conversations"].calls_of("query_items") == []


@pytest.mark.parametrize(
    ("params", "path", "reason"),
    [
        ({"limit": 0}, "query.limit", "out_of_range"),
        ({"limit": 51}, "query.limit", "out_of_range"),
        ({"projectId": "not-a-uuid"}, "query.projectId", "invalid_uuid"),
        ({"page": 2}, "query.page", "unknown_field"),
    ],
)
async def test_list_parameters_are_validated(
    api: ApiUnderTest, alice: SharedPrincipal, params: dict[str, Any], path: str, reason: str
) -> None:
    response = await api.client.get("/v1/sessions", params=params, headers=alice.authorization())

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": path, "reason": reason}]


def _valid_sessions_cursor(project: UUID | None) -> dict[str, Any]:
    return {
        "v": 1,
        "t": "2026-11-02T09:30:00.000Z",
        "s": str(uuid4()),
        "p": None if project is None else str(project),
    }


SESSION_CURSORS: Final[dict[str, str]] = {
    "another_filter": cursor_text(_valid_sessions_cursor(PROJECT_B)),
    "garbage": "not!base64",
    "tampered": cursor_text(_valid_sessions_cursor(None))[:-3] + "AAA",
    "oversized": "A" * 513,
    "wrong_version": cursor_text(_valid_sessions_cursor(None) | {"v": 2}),
    "extra_member": cursor_text(_valid_sessions_cursor(None) | {"x": 1}),
    "not_json": base64.urlsafe_b64encode(b"{not json").rstrip(b"=").decode(),
}
TURN_CURSORS: Final[dict[str, str]] = {
    "garbage": "not!base64",
    "oversized": "A" * 513,
    "wrong_version": cursor_text({"v": 2, "b": 2}),
    "below_two": cursor_text({"v": 1, "b": 1}),
    "not_an_integer": cursor_text({"v": 1, "b": "2"}),
    "extra_member": cursor_text({"v": 1, "b": 2, "p": None}),
}


@pytest.mark.parametrize("name", SESSION_CURSORS)
async def test_t302_a_bad_session_cursor_is_invalid_cursor(
    api: ApiUnderTest,
    access_script: AccessScript,
    alice: SharedPrincipal,
    containers: dict[str, FakeContainer],
    name: str,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    response = await api.client.get(
        "/v1/sessions",
        params={"cursor": SESSION_CURSORS[name], "projectId": str(PROJECT_A)}
        if name == "another_filter"
        else {"cursor": SESSION_CURSORS[name]},
        headers=alice.authorization(),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]
    assert containers["conversations"].calls_of("query_items") == []


async def test_t302_a_cursor_issued_without_a_filter_is_refused_with_one(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    for minutes in (1, 2):
        seed_session(containers, alice.oid, last_turn_minutes=minutes)
    first = await api.client.get("/v1/sessions", params={"limit": 1}, headers=alice.authorization())
    cursor = first.json()["nextCursor"]

    reused = await api.client.get(
        "/v1/sessions",
        params={"limit": 1, "cursor": cursor, "projectId": str(PROJECT_A)},
        headers=alice.authorization(),
    )

    assert problem(reused) == (422, "validation_failed")


@pytest.mark.parametrize("name", TURN_CURSORS)
async def test_t302_a_bad_turn_cursor_is_invalid_cursor(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    name: str,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid)

    response = await api.client.get(
        f"/v1/sessions/{session}/turns",
        params={"cursor": TURN_CURSORS[name]},
        headers=alice.authorization(),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]


async def test_t302_a_turn_cursor_beyond_the_turn_count_is_invalid_cursor(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid, turns=2)

    at_limit = await api.client.get(
        f"/v1/sessions/{session}/turns",
        params={"cursor": encode_cursor(session_routes.TurnsCursor(b=3))},
        headers=alice.authorization(),
    )
    beyond = await api.client.get(
        f"/v1/sessions/{session}/turns",
        params={"cursor": encode_cursor(session_routes.TurnsCursor(b=4))},
        headers=alice.authorization(),
    )

    assert at_limit.status_code == 200
    assert problem(beyond) == (422, "validation_failed")
    assert beyond.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]


async def test_a_failing_store_answers_dependency_unavailable(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid)
    containers["conversations"].fail("query_items", error=ServiceRequestError("down"), times=2)
    containers["conversations"].fail("read_item", error=ServiceRequestError("down"))

    listed = await api.client.get("/v1/sessions", headers=alice.authorization())
    read = await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())
    access_script.failures["reachable_projects"] = SqlUnavailable("connection", 40613, "")
    sql = await api.client.get("/v1/sessions", headers=alice.authorization())

    for response in (listed, read, sql):
        assert problem(response) == (503, "dependency_unavailable")
        assert response.json()["retryAfterSeconds"] == 5


async def test_the_list_span_records_the_page_and_the_charge(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    access_script.grant(PROJECT_B, alice.oid)
    seed_session(containers, alice.oid)

    await api.client.get("/v1/sessions", params={"limit": 5}, headers=alice.authorization())

    (span,) = captured.finished_spans("docchat.sessions.list")
    assert dict(span.attributes or {}) == {
        "docchat.page.limit": 5,
        "docchat.page.returned": 1,
        "docchat.projects.reachable": 2,
        "docchat.filter.project": False,
        "docchat.cosmos.request_charge": 1.0,
    }


# --- getSession and listSessionTurns -------------------------------------------------------------


async def test_get_session_returns_the_newest_turns_ascending_with_an_earlier_cursor(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, turns = seed_session(containers, alice.oid, turns=5)

    response = await api.client.get(
        f"/v1/sessions/{session}", params={"turnsLimit": 3}, headers=alice.authorization()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "sessionId",
        "projectId",
        "title",
        "createdAt",
        "lastTurnAt",
        "turnCount",
        "turns",
        "earlierTurnsCursor",
    }
    assert [turn["seq"] for turn in body["turns"]] == [3, 4, 5]
    assert [turn["turnId"] for turn in body["turns"]] == [turn["id"] for turn in turns[2:]]
    assert body["turnCount"] == 5
    earlier = await api.client.get(
        f"/v1/sessions/{session}/turns",
        params={"cursor": body["earlierTurnsCursor"], "limit": 3},
        headers=alice.authorization(),
    )
    assert [turn["seq"] for turn in earlier.json()["items"]] == [1, 2]
    assert earlier.json()["nextCursor"] is None
    query = containers["conversations"].calls_of("query_items")[0]
    assert query.partition_key == (str(alice.oid), str(session))
    parameters = {item["name"]: item["value"] for item in query.kwargs["parameters"]}
    assert parameters["@before"] == 6
    assert parameters["@take"] == 4


async def test_t303_list_session_turns_pages_from_the_newest_to_the_oldest(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid, turns=5)

    pages: list[list[int]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get(
            f"/v1/sessions/{session}/turns", params=params, headers=alice.authorization()
        )
        pages.append([turn["seq"] for turn in response.json()["items"]])
        cursor = response.json()["nextCursor"]
        if cursor is None:
            break

    assert pages == [[4, 5], [2, 3], [1]]


async def test_a_session_of_one_turn_has_no_earlier_cursor(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid, turns=1)

    body = (await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())).json()

    assert len(body["turns"]) == 1
    assert body["earlierTurnsCursor"] is None


@pytest.mark.parametrize("suffix", ["", "/turns"])
async def test_a_missing_foreign_or_hidden_session_is_one_not_found(
    *,
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
    carol: SharedPrincipal,
    suffix: str,
) -> None:
    access_script.grant(PROJECT_A, alice.oid, bob.oid)
    access_script.grant(PROJECT_B, bob.oid)
    bobs, _ = seed_session(containers, bob.oid)
    hidden, _ = seed_session(containers, alice.oid, project=PROJECT_B)

    responses = [
        await api.client.get(f"/v1/sessions/{uuid4()}{suffix}", headers=alice.authorization()),
        await api.client.get(f"/v1/sessions/{bobs}{suffix}", headers=alice.authorization()),
        await api.client.get(f"/v1/sessions/{bobs}{suffix}", headers=carol.authorization()),
        await api.client.get(f"/v1/sessions/{hidden}{suffix}", headers=alice.authorization()),
    ]

    bodies = [body_without_correlation(response) for response in responses]
    assert all(problem(response) == (404, "session_not_found") for response in responses)
    assert all(body == bodies[0] for body in bodies)
    keys = {call.partition_key for call in containers["conversations"].calls}
    assert all(key is not None and key[0] in {str(alice.oid), str(carol.oid)} for key in keys)


async def test_a_session_whose_project_returns_shows_again_unchanged(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid)
    before = (await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())).json()

    access_script.archived.add(PROJECT_A)
    archived = await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())
    access_script.archived.clear()
    after = (await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())).json()

    assert problem(archived) == (404, "session_not_found")
    assert after == before


async def test_a_malformed_session_id_is_invalid_uuid(
    api: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await api.client.get("/v1/sessions/{abc}", headers=alice.authorization())

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "path.sessionId", "reason": "invalid_uuid"}]


async def test_the_read_span_records_the_outcome(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(containers, alice.oid, turns=2)

    await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())
    await api.client.get(f"/v1/sessions/{uuid4()}/turns", headers=alice.authorization())

    (found,) = captured.finished_spans("docchat.sessions.get")
    assert dict(found.attributes or {}) == {
        "docchat.session_id": str(session),
        "docchat.page.returned": 2,
        "docchat.outcome": "ok",
        "docchat.cosmos.request_charge": 2.0,
    }
    (missing,) = captured.finished_spans("docchat.sessions.list_turns")
    assert (missing.attributes or {})["docchat.outcome"] == "not_found"


# --- The turn view (T-305) ---------------------------------------------------------------------


async def test_t305_a_turn_view_has_exactly_its_thirteen_keys(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    feedback = Feedback(
        rating=FeedbackRating.DOWN,
        category=FeedbackCategory.INCOMPLETE,
        comment="Missing a step",
        created_at=START,
        updated_at=START + timedelta(minutes=1),
    )
    session, _ = seed_session(
        containers,
        alice.oid,
        turns=1,
        citations=[stored_citation(number=1)],
        allowed_links=["https://example.org/policy"],
        followups=["What about hotels?"],
        feedback=feedback,
    )

    body = (await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())).json()

    (turn,) = body["turns"]
    assert set(turn) == TURN_VIEW_KEYS
    assert set(turn["citations"][0]) == {
        "number",
        "title",
        "pageNumber",
        "headingPath",
        "contentType",
        "openAs",
    }
    assert turn["feedback"] == {
        "rating": "down",
        "category": "incomplete",
        "comment": "Missing a step",
        "updatedAt": format_utc(START + timedelta(minutes=1)),
    }
    assert turn["allowedLinks"] == ["https://example.org/policy"]
    assert turn["followups"] == ["What about hotels?"]
    assert turn["status"] == "completed"
    assert turn["errorCode"] is None


async def test_t305_a_streaming_turn_past_its_deadline_reads_as_abandoned(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    clock: FakeClock,
) -> None:
    access_script.grant(PROJECT_A, alice.oid)
    session, _ = seed_session(
        containers,
        alice.oid,
        turns=1,
        status=TurnStatus.STREAMING,
        answer="",
        finish_reason=None,
        ended_at=None,
        deadline_at=START + timedelta(seconds=210),
    )

    live = (await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())).json()
    clock.advance(211)
    late = (await api.client.get(f"/v1/sessions/{session}", headers=alice.authorization())).json()

    assert (live["turns"][0]["status"], live["turns"][0]["errorCode"]) == ("streaming", None)
    assert (late["turns"][0]["status"], late["turns"][0]["errorCode"]) == (
        "failed",
        "turn_abandoned",
    )
    assert late["turns"][0]["endedAt"] is None


def test_t305_a_failed_turn_shows_its_stored_code() -> None:
    turn = turn_document(
        status=TurnStatus.FAILED, error=TurnError(code="service_restarting", retryable=True)
    )
    row = session_routes.TurnRow.of(turn.to_item())

    view = turn_view(row, START)

    assert (view.status, view.error_code) == (TurnStatus.FAILED, "service_restarting")
    assert set(view.model_dump(mode="json")) == TURN_VIEW_KEYS
    assert isinstance(view, TurnView)
