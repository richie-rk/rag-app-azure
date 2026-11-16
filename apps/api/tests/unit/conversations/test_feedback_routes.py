"""Unit tests of `putTurnFeedback` in process (T-327, the in-process halves of T-326 and T-306;
`13-conversations-and-memory.md` section 8; `11-api.md` section 3.4.4).

The app runs over `FakeContainer`s and the fake data-access layer of `conftest.py`; turns are
seeded with the Cosmos DB builders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from azure.core.exceptions import ServiceRequestError
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.app import Ports
from docchat_api.conversations import feedback_routes
from docchat_api.conversations.feedback_routes import (
    REQUEST_EXAMPLE,
    FeedbackRequest,
    normalise_comment,
)
from docchat_api.http.routing import DocChatRoute
from docchat_core.cosmos.conversations import (
    Feedback,
    FeedbackCategory,
    FeedbackRating,
    TurnError,
    TurnStatus,
)
from docchat_core.models import format_utc
from docchat_core.opsconfig import encode_value
from docchat_testing.builders.cosmos import turn_document
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.api import ApiUnderTest, api_settings, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

START: Final = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
"""The start of the conftest's clock."""
PROJECT: Final = UUID("a0000000-0000-4000-8000-00000000000a")
DOWN: Final = {"rating": "down", "category": "incorrect", "comment": "Wrong figure"}


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


class AccessScript(Protocol):
    audits: list[Any]
    calls: list[str]

    def grant(self, project: UUID, *users: UUID) -> None: ...

    def revoke(self, project: UUID, user: UUID) -> None: ...


class FakeRows(Protocol):
    rows: dict[str, str]


def seed_turn(
    containers: dict[str, FakeContainer], user: UUID, **changes: Any
) -> tuple[str, dict[str, Any]]:
    """A completed turn of `user` in `PROJECT`; its URL and stored item."""
    turn = turn_document(user_id=user, project_id=PROJECT, **changes)
    stored = containers["conversations"].put(turn.to_item())
    return f"/v1/sessions/{turn.session_id}/turns/{turn.id}/feedback", stored


def stored_feedback(
    containers: dict[str, FakeContainer], item: dict[str, Any]
) -> dict[str, Any] | None:
    stored = containers["conversations"].get([item["userId"], item["sessionId"]], item["id"])
    assert stored is not None
    feedback = stored["feedback"]
    return feedback if feedback is None else dict(feedback)


def problem(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


# --- Routing and models ------------------------------------------------------------------------


def test_t306_put_turn_feedback_is_a_write_limited_route_of_the_route_class() -> None:
    (route,) = [route for route in feedback_routes.router.routes if isinstance(route, DocChatRoute)]
    assert route.operation_id == "putTurnFeedback"
    assert route.path == "/sessions/{sessionId}/turns/{turnId}/feedback"
    assert route.methods == {"PUT"}
    assert route.route_options.write_limited is True
    assert route.route_options.burst_limited is True
    assert route.route_options.body_limit == 65_536
    assert route.access_spec.scoped_params == ("sessionId", "turnId")
    assert route.tags == ["sessions"]


def test_t043_the_request_example_validates_and_is_published() -> None:
    body = FeedbackRequest.model_validate(REQUEST_EXAMPLE)
    (route,) = [route for route in feedback_routes.router.routes if isinstance(route, DocChatRoute)]

    assert body.rating is FeedbackRating.DOWN
    assert body.category is FeedbackCategory.INCOMPLETE
    assert route.openapi_extra is not None
    content = route.openapi_extra["requestBody"]["content"]["application/json"]
    assert content["example"] == REQUEST_EXAMPLE


def test_t327_comment_normalisation() -> None:
    assert normalise_comment(None) is None
    assert normalise_comment("  \r\n\t ") is None
    assert normalise_comment("e\u0301") == "\u00e9"
    assert normalise_comment(" a\r\nb\rc\td ") == "a\nb\nc\td"
    assert normalise_comment("a\u0000b\u200bc\u202ed\ue000e") == "abcde"


# --- Saving ------------------------------------------------------------------------------------


async def test_t326_up_then_down_keeps_created_at_and_replaces_the_value(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    clock: FakeClock,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid)

    up = await api.client.put(url, json={"rating": "up"}, headers=alice.authorization())
    clock.advance(60)
    down = await api.client.put(url, json=DOWN, headers=alice.authorization())

    assert up.status_code == 200, up.text
    assert up.json() == {
        "rating": "up",
        "category": None,
        "comment": None,
        "updatedAt": format_utc(START),
    }
    assert down.json() == {
        "rating": "down",
        "category": "incorrect",
        "comment": "Wrong figure",
        "updatedAt": format_utc(START + timedelta(seconds=60)),
    }
    assert stored_feedback(containers, item) == {
        "rating": "down",
        "category": "incorrect",
        "comment": "Wrong figure",
        "createdAt": format_utc(START),
        "updatedAt": format_utc(START + timedelta(seconds=60)),
    }
    (patch, *_) = containers["conversations"].calls_of("patch_item")
    assert patch.partition_key == (str(alice.oid), item["sessionId"])
    assert patch.kwargs["etag"] == item["_etag"]
    assert patch.kwargs["timeout"] == 5
    assert access_script.audits == []


async def test_a_change_to_up_clears_the_category_and_a_null_comment_removes_it(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid)
    await api.client.put(url, json=DOWN, headers=alice.authorization())

    response = await api.client.put(
        url, json={"rating": "up", "comment": None}, headers=alice.authorization()
    )

    assert response.json()["category"] is None
    stored = stored_feedback(containers, item)
    assert stored is not None
    assert (stored["category"], stored["comment"]) == (None, None)


async def test_a_cancelled_turn_with_text_takes_feedback(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, _ = seed_turn(containers, alice.oid, status=TurnStatus.CANCELLED, answer="Part of it")

    response = await api.client.put(url, json={"rating": "up"}, headers=alice.authorization())

    assert response.status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"status": TurnStatus.STREAMING, "answer": "", "finish_reason": None, "ended_at": None},
        {
            "status": TurnStatus.FAILED,
            "answer": "Partial",
            "error": TurnError(code="model_unavailable", retryable=True),
        },
        {"status": TurnStatus.CANCELLED, "answer": ""},
    ],
    ids=["streaming", "failed", "cancelled_without_text"],
)
async def test_t326_a_turn_without_an_answer_refuses_feedback(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    changes: dict[str, Any],
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid, **changes)

    response = await api.client.put(url, json={"rating": "up"}, headers=alice.authorization())

    assert problem(response) == (409, "feedback_not_allowed")
    assert stored_feedback(containers, item) is None


# --- Validation (T-327) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "errors"),
    [
        (
            {"rating": "up", "category": "other"},
            [{"path": "body.category", "reason": "category_requires_down"}],
        ),
        (
            {"rating": "down", "comment": "x" * 2_001},
            [{"path": "body.comment", "reason": "too_long"}],
        ),
        (
            {"rating": "up", "category": "other", "comment": "y" * 2_001},
            [
                {"path": "body.category", "reason": "category_requires_down"},
                {"path": "body.comment", "reason": "too_long"},
            ],
        ),
        (
            {"rating": "down", "comment": "z" * 8_001},
            [{"path": "body.comment", "reason": "too_long"}],
        ),
        ({"rating": "sideways"}, [{"path": "body.rating", "reason": "not_allowed"}]),
        ({"rating": "up", "extra": 1}, [{"path": "body.extra", "reason": "unknown_field"}]),
    ],
    ids=["category_with_up", "comment_2001", "both", "comment_8001", "rating", "unknown"],
)
async def test_t327_body_rules_answer_validation_failed_before_any_lookup(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    alice: SharedPrincipal,
    body: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    url = f"/v1/sessions/{uuid4()}/turns/{uuid4()}/feedback"

    response = await api.client.put(url, json=body, headers=alice.authorization())

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == errors
    assert containers["conversations"].calls == []


async def test_t327_a_comment_of_2000_code_points_after_normalisation_is_kept(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid)
    comment = "  " + "é" * 1_999 + "\u200b" * 100 + "x\r\n  "

    response = await api.client.put(
        url, json={"rating": "down", "comment": comment}, headers=alice.authorization()
    )

    assert response.status_code == 200, response.text
    stored = stored_feedback(containers, item)
    assert stored is not None
    assert stored["comment"] == "é" * 1_999 + "x"


async def test_t327_comments_disabled_refuses_a_comment_but_not_a_rating(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    fake_sql: FakeRows,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    fake_sql.rows["feedback.comments_enabled"] = encode_value(False)
    access_script.grant(PROJECT, alice.oid)
    url, _ = seed_turn(containers, alice.oid)

    async with running_api(settings=api_settings(), ports=ports, signing_keys=signing_keys) as api:
        refused = await api.client.put(url, json=DOWN, headers=alice.authorization())
        blank = await api.client.put(
            url, json={"rating": "up", "comment": "  "}, headers=alice.authorization()
        )

    assert problem(refused) == (422, "validation_failed")
    assert refused.json()["errors"] == [{"path": "body.comment", "reason": "comments_disabled"}]
    assert blank.status_code == 200


# --- Lookup and concurrency --------------------------------------------------------------------


async def test_a_missing_foreign_or_hidden_turn_is_one_turn_not_found(
    *,
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
    carol: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT, alice.oid, bob.oid)
    bobs_url, bobs = seed_turn(containers, bob.oid)
    own_url, own = seed_turn(containers, alice.oid)

    responses = [
        await api.client.put(
            f"/v1/sessions/{uuid4()}/turns/{uuid4()}/feedback",
            json={"rating": "up"},
            headers=alice.authorization(),
        ),
        await api.client.put(bobs_url, json={"rating": "up"}, headers=alice.authorization()),
        await api.client.put(bobs_url, json={"rating": "up"}, headers=carol.authorization()),
    ]
    access_script.revoke(PROJECT, alice.oid)
    responses.append(
        await api.client.put(own_url, json={"rating": "up"}, headers=alice.authorization())
    )
    access_script.grant(PROJECT, alice.oid)
    restored = await api.client.put(own_url, json={"rating": "up"}, headers=alice.authorization())

    bodies = [{**response.json(), "correlationId": None} for response in responses]
    assert all(problem(response) == (404, "turn_not_found") for response in responses)
    assert all(body == bodies[0] for body in bodies)
    assert stored_feedback(containers, bobs) is None
    assert restored.status_code == 200
    assert stored_feedback(containers, own) is not None


async def test_a_turn_changed_between_read_and_patch_is_read_again(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid)
    key = [item["userId"], item["sessionId"]]
    conversations = containers["conversations"]
    conversations.before(
        "patch_item", lambda: conversations.touch(key, item["id"], followups=["Next?"])
    )

    response = await api.client.put(url, json={"rating": "up"}, headers=alice.authorization())

    assert response.status_code == 200
    stored = conversations.get(key, item["id"])
    assert stored is not None
    assert stored["followups"] == ["Next?"]
    assert stored["feedback"]["rating"] == "up"
    assert len(conversations.calls_of("read_item")) == 2
    assert access_script.calls.count("authorize_project") == 2


async def test_three_conflicts_answer_dependency_unavailable(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid)
    containers["conversations"].fail("patch_item", status=412, times=3)

    response = await api.client.put(url, json={"rating": "up"}, headers=alice.authorization())

    assert problem(response) == (503, "dependency_unavailable")
    assert len(containers["conversations"].calls_of("patch_item")) == 3
    assert stored_feedback(containers, item) is None


async def test_t326_a_cosmos_failure_answers_503_with_nothing_stored(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid)
    containers["conversations"].fail("patch_item", error=ServiceRequestError("down"))

    response = await api.client.put(url, json=DOWN, headers=alice.authorization())

    assert problem(response) == (503, "dependency_unavailable")
    assert stored_feedback(containers, item) is None
    assert captured.events("turn_feedback_saved") == []


# --- Telemetry ---------------------------------------------------------------------------------


async def test_t326_the_saved_event_and_metric_never_carry_the_comment(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    access_script.grant(PROJECT, alice.oid)
    url, item = seed_turn(containers, alice.oid)

    await api.client.put(url, json={"rating": "up"}, headers=alice.authorization())
    await api.client.put(url, json=DOWN, headers=alice.authorization())

    first, second = captured.events("turn_feedback_saved")
    assert first == {
        "rating": "up",
        "category": "none",
        "projectId": str(PROJECT),
        "turnId": item["id"],
        "sessionId": item["sessionId"],
        "isChange": False,
        "hasComment": False,
    }
    assert (second["isChange"], second["hasComment"], second["category"]) == (
        True,
        True,
        "incorrect",
    )
    assert captured.points("docchat.feedback.saved") == {
        (("category", "none"), ("rating", "up")): 1,
        (("category", "incorrect"), ("rating", "down")): 1,
    }
    spans = captured.finished_spans("docchat.feedback.put")
    assert dict(spans[1].attributes or {}) == {
        "docchat.session_id": item["sessionId"],
        "docchat.turn_id": item["id"],
        "docchat.project_id": str(PROJECT),
        "docchat.feedback.rating": "down",
        "docchat.feedback.category": "incorrect",
        "docchat.feedback.is_change": True,
        "docchat.outcome": "ok",
    }


async def test_a_refusal_records_its_code_as_the_outcome(
    api: ApiUnderTest, alice: SharedPrincipal, captured: Captured
) -> None:
    await api.client.put(
        f"/v1/sessions/{uuid4()}/turns/{uuid4()}/feedback",
        json={"rating": "up"},
        headers=alice.authorization(),
    )

    (span,) = captured.finished_spans("docchat.feedback.put")
    assert (span.attributes or {})["docchat.outcome"] == "turn_not_found"


def test_saved_feedback_renders_without_created_at() -> None:
    feedback = Feedback(rating=FeedbackRating.UP, created_at=START, updated_at=START)

    assert feedback_routes.FeedbackView.of(feedback).model_dump(mode="json", by_alias=True) == {
        "rating": "up",
        "category": None,
        "comment": None,
        "updatedAt": format_utc(START),
    }
