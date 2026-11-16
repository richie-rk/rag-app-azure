"""Unit tests of the memory operations in process (the in-process halves of T-306, T-318, T-321,
T-322, T-323 and T-020; `13-conversations-and-memory.md` sections 7.1 to 7.9 and 7.16; `11-api.md`
sections 1.9 and 3.6).

The app runs over `FakeContainer`s, the fake data-access layer of `conftest.py` (which records the
audit rows written after a commit) and the chat doubles, whose Prompt Shields endpoint answers
the fact screening and whose tokenizer counts one token per code point.
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
from docchat_api.conversations import memory_routes
from docchat_api.conversations.memory_routes import (
    FACT_EXAMPLE,
    SETTINGS_EXAMPLE,
    MemoryFactRequest,
    MemorySettingsRequest,
    ordered_facts,
)
from docchat_api.http.routing import DocChatRoute
from docchat_core.cosmos.memories import FactSource, FactSourceKind
from docchat_core.memory import FactCategory
from docchat_core.models import format_utc
from docchat_core.opsconfig import encode_value
from docchat_testing.builders.cosmos import fact, memory_document
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.api import ApiUnderTest, api_settings, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

START: Final = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
"""The start of the conftest's clock."""
NEW_TEXT: Final = "Works on supplier contract renewals"
ATTACK: Final = httpx.Response(
    200, json={"userPromptAnalysis": {"attackDetected": True}, "documentsAnalysis": []}
)


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...

    def records(self, message: str) -> list[Any]: ...


class AccessScript(Protocol):
    audits: list[Any]
    audit_attempts: int
    audit_failures: int


class FakeRows(Protocol):
    rows: dict[str, str]


def seed(
    containers: dict[str, FakeContainer], user: UUID, *facts: Any, **changes: Any
) -> dict[str, Any]:
    """The memory document of `user` holding `facts`; the stored item with its `_etag`."""
    document = memory_document(user_id=user, facts=list(facts), **changes)
    return containers["memories"].put(document.to_item())


def stored(containers: dict[str, FakeContainer], user: UUID) -> dict[str, Any] | None:
    return containers["memories"].get(str(user), str(user))


def problem(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def headers(principal: SharedPrincipal, etag: str | None = None) -> dict[str, str]:
    values = principal.authorization()
    if etag is not None:
        values["If-Match"] = etag
    return values


def writes(containers: dict[str, FakeContainer]) -> list[Any]:
    return [
        call
        for call in containers["memories"].calls
        if call.method in {"create_item", "replace_item"}
    ]


# --- Routing and models ------------------------------------------------------------------------


def test_t306_the_memory_routes_and_their_options() -> None:
    routes = {
        route.operation_id: route
        for route in memory_routes.router.routes
        if isinstance(route, DocChatRoute)
    }
    assert {name: (route.path, route.methods) for name, route in routes.items()} == {
        "getMemory": ("/memory", {"GET"}),
        "updateMemorySettings": ("/memory", {"PATCH"}),
        "updateMemoryFact": ("/memory/facts/{factId}", {"PUT"}),
        "deleteMemoryFact": ("/memory/facts/{factId}", {"DELETE"}),
        "clearMemory": ("/memory/facts", {"DELETE"}),
    }
    for name, route in routes.items():
        assert route.route_options.burst_limited is True
        assert route.route_options.write_limited is (name != "getMemory")
        assert route.access_spec.if_match is (name != "getMemory")
        assert route.tags == ["memory"]
    assert routes["updateMemoryFact"].access_spec.scoped_params == ("factId",)
    assert routes["deleteMemoryFact"].access_spec.scoped_params == ("factId",)
    assert routes["clearMemory"].access_spec.scoped_params == ()


def test_t043_the_request_examples_validate_and_are_published() -> None:
    routes = {
        route.operation_id: route
        for route in memory_routes.router.routes
        if isinstance(route, DocChatRoute)
    }

    assert MemorySettingsRequest.model_validate(SETTINGS_EXAMPLE).paused is True
    assert MemoryFactRequest.model_validate(FACT_EXAMPLE).category is FactCategory.TOPIC
    for operation, example in (
        ("updateMemorySettings", SETTINGS_EXAMPLE),
        ("updateMemoryFact", FACT_EXAMPLE),
    ):
        extra = routes[operation].openapi_extra
        assert extra is not None
        assert extra["requestBody"]["content"]["application/json"]["example"] == example


def test_facts_are_ordered_by_category_then_newest_first() -> None:
    older = fact(category=FactCategory.TEAM, updated_at=START)
    newer = fact(category=FactCategory.TEAM, updated_at=START + timedelta(minutes=1))
    role = fact(category=FactCategory.ROLE, updated_at=START - timedelta(days=1))
    style = fact(category=FactCategory.ANSWER_STYLE, updated_at=START + timedelta(days=1))

    assert ordered_facts([style, older, newer, role]) == [role, newer, older, style]


# --- getMemory ---------------------------------------------------------------------------------


async def test_get_memory_creates_the_empty_document_once(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    first = await api.client.get("/v1/memory", headers=alice.authorization())
    second = await api.client.get("/v1/memory", headers=alice.authorization())

    assert first.status_code == 200, first.text
    document = stored(containers, alice.oid)
    assert document is not None
    assert first.json() == {
        "enabled": True,
        "paused": False,
        "facts": [],
        "limits": {
            "maxFacts": 50,
            "maxFactChars": 200,
            "maxTokens": 1000,
            "usedFacts": 0,
            "usedTokens": 0,
        },
        "etag": document["_etag"],
        "updatedAt": format_utc(START),
    }
    assert second.json() == first.json()
    assert len(containers["memories"].calls_of("create_item")) == 1
    assert document["pausedAt"] is None
    assert document["userEditedAt"] is None


async def test_a_concurrent_creation_is_read(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    memories = containers["memories"]
    memories.before(
        "create_item", lambda: memories.put(memory_document(user_id=alice.oid).to_item())
    )

    response = await api.client.get("/v1/memory", headers=alice.authorization())

    assert response.status_code == 200
    assert len(response.json()["facts"]) == 1
    assert len(memories.calls_of("read_item")) == 2


async def test_get_memory_shows_the_facts_and_their_use(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    session, turn = uuid4(), uuid4()
    extracted = fact(
        category=FactCategory.ROLE,
        tokens=7,
        source=FactSource(kind=FactSourceKind.EXTRACTED, session_id=session, turn_id=turn),
    )
    own = fact(category=FactCategory.TOPIC, tokens=5)
    seed(containers, alice.oid, own, extracted)

    body = (await api.client.get("/v1/memory", headers=alice.authorization())).json()

    assert [item["factId"] for item in body["facts"]] == [str(extracted.id), str(own.id)]
    assert body["facts"][0]["source"] == {"kind": "extracted", "sessionId": str(session)}
    assert body["facts"][1]["source"] == {"kind": "user", "sessionId": None}
    assert set(body["facts"][0]) == {
        "factId",
        "text",
        "category",
        "source",
        "createdAt",
        "updatedAt",
    }
    assert (body["limits"]["usedFacts"], body["limits"]["usedTokens"]) == (2, 12)


async def test_t321_memory_off_for_everyone_still_serves_every_operation(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    fake_sql: FakeRows,
    containers: dict[str, FakeContainer],
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    fake_sql.rows["memory.enabled"] = encode_value(False)
    edited = fact()
    item = seed(containers, alice.oid, edited)

    async with running_api(settings=api_settings(), ports=ports, signing_keys=signing_keys) as api:
        read = await api.client.get("/v1/memory", headers=alice.authorization())
        paused = await api.client.patch(
            "/v1/memory", json={"paused": True}, headers=headers(alice, item["_etag"])
        )
        updated = await api.client.put(
            f"/v1/memory/facts/{edited.id}",
            json={"text": NEW_TEXT, "category": "topic"},
            headers=headers(alice, paused.json()["etag"]),
        )
        cleared = await api.client.delete(
            "/v1/memory/facts", headers=headers(alice, updated.json()["etag"])
        )

    assert read.json()["enabled"] is False
    assert [response.status_code for response in (paused, updated, cleared)] == [200] * 3
    assert cleared.json()["enabled"] is False


# --- If-Match ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, "*", 'W/"abc"', '"a", "b"', "unquoted", '""'],
    ids=["missing", "star", "weak", "list", "unquoted", "empty"],
)
async def test_a_missing_or_malformed_if_match_is_precondition_required(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    alice: SharedPrincipal,
    value: str | None,
) -> None:
    edited = fact()
    seed(containers, alice.oid, edited)

    responses = [
        await api.client.patch("/v1/memory", json={"paused": True}, headers=headers(alice, value)),
        await api.client.put(
            f"/v1/memory/facts/{edited.id}",
            json={"text": NEW_TEXT, "category": "topic"},
            headers=headers(alice, value),
        ),
        await api.client.delete(f"/v1/memory/facts/{edited.id}", headers=headers(alice, value)),
        await api.client.delete("/v1/memory/facts", headers=headers(alice, value)),
    ]

    assert [problem(response) for response in responses] == [(428, "precondition_required")] * 4
    assert writes(containers) == []


async def test_a_stale_etag_is_precondition_failed_and_changes_nothing(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    edited = fact()
    item = seed(containers, alice.oid, edited)
    stale = '"00000000-0000-0000-0000-000000000000"'

    responses = [
        await api.client.patch("/v1/memory", json={"paused": True}, headers=headers(alice, stale)),
        await api.client.put(
            f"/v1/memory/facts/{edited.id}",
            json={"text": NEW_TEXT, "category": "topic"},
            headers=headers(alice, stale),
        ),
        await api.client.delete(f"/v1/memory/facts/{edited.id}", headers=headers(alice, stale)),
        await api.client.delete("/v1/memory/facts", headers=headers(alice, stale)),
    ]

    assert [problem(response) for response in responses] == [(412, "precondition_failed")] * 4
    assert stored(containers, alice.oid) == item


async def test_t322_without_a_document_settings_and_clear_answer_412_and_the_facts_404(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    tag = '"00000000-0000-0000-0000-000000000000"'
    fact_id = uuid4()

    settings = await api.client.patch(
        "/v1/memory", json={"paused": True}, headers=headers(alice, tag)
    )
    clear = await api.client.delete("/v1/memory/facts", headers=headers(alice, tag))
    update = await api.client.put(
        f"/v1/memory/facts/{fact_id}",
        json={"text": NEW_TEXT, "category": "topic"},
        headers=headers(alice),
    )
    delete = await api.client.delete(f"/v1/memory/facts/{fact_id}", headers=headers(alice))

    assert problem(settings) == (412, "precondition_failed")
    assert problem(clear) == (412, "precondition_failed")
    assert problem(update) == (404, "memory_fact_not_found")
    assert problem(delete) == (404, "memory_fact_not_found")
    assert stored(containers, alice.oid) is None


async def test_t322_a_deleted_fact_is_404_whatever_if_match_holds(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    gone, kept = fact(), fact()
    item = seed(containers, alice.oid, gone, kept)
    deleted = await api.client.delete(
        f"/v1/memory/facts/{gone.id}", headers=headers(alice, item["_etag"])
    )
    assert deleted.status_code == 200

    responses = [
        await api.client.delete(f"/v1/memory/facts/{gone.id}", headers=headers(alice, value))
        for value in (item["_etag"], None)
    ]
    responses += [
        await api.client.put(
            f"/v1/memory/facts/{gone.id}",
            json={"text": NEW_TEXT, "category": "topic"},
            headers=headers(alice, value),
        )
        for value in (item["_etag"], None)
    ]

    assert [problem(response) for response in responses] == [(404, "memory_fact_not_found")] * 4
    assert len(access_script.audits) == 1


# --- updateMemorySettings ----------------------------------------------------------------------


async def test_pausing_and_resuming_write_and_audit_each_change(
    *,
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    clock: FakeClock,
    captured: Captured,
) -> None:
    item = seed(containers, alice.oid, fact())

    paused = await api.client.patch(
        "/v1/memory", json={"paused": True}, headers=headers(alice, item["_etag"])
    )
    clock.advance(60)
    resumed = await api.client.patch(
        "/v1/memory", json={"paused": False}, headers=headers(alice, paused.json()["etag"])
    )

    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True
    assert resumed.json()["paused"] is False
    document = stored(containers, alice.oid)
    assert document is not None
    assert document["pausedAt"] == format_utc(START)
    assert document["userEditedAt"] == format_utc(START + timedelta(seconds=60))
    assert resumed.json()["etag"] == document["_etag"] != item["_etag"]
    assert [
        (event.action, event.target_type, event.target_id) for event in access_script.audits
    ] == [
        ("memory.paused", "memory", str(alice.oid)),
        ("memory.resumed", "memory", str(alice.oid)),
    ]
    first = access_script.audits[0]
    assert (first.actor_type, first.actor_object_id, first.subject_object_id) == (
        "user",
        alice.oid,
        alice.oid,
    )
    assert first.project_id is None
    assert first.metadata is None
    assert captured.points("docchat.memory.user_changes") == {
        (("action", "paused"),): 1,
        (("action", "resumed"),): 1,
    }


async def test_an_unchanged_pause_flag_writes_nothing(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    item = seed(containers, alice.oid, fact())

    response = await api.client.patch(
        "/v1/memory", json={"paused": False}, headers=headers(alice, item["_etag"])
    )

    assert response.status_code == 200
    assert response.json()["etag"] == item["_etag"]
    assert writes(containers) == []
    assert access_script.audits == []
    (span,) = captured.finished_spans("docchat.memory.update_settings")
    assert dict(span.attributes or {}) == {
        "docchat.outcome": "unchanged",
        "docchat.memory.facts_count": 1,
        "docchat.cosmos.request_charge": 1.0,
    }


async def test_a_pause_flag_must_be_a_boolean(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    item = seed(containers, alice.oid)

    response = await api.client.patch(
        "/v1/memory", json={"paused": "yes"}, headers=headers(alice, item["_etag"])
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.paused", "reason": "invalid_type"}]


# --- updateMemoryFact ----------------------------------------------------------------------------


async def test_an_edit_screens_the_new_text_and_makes_the_fact_the_users(
    *,
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    route_fakes: ChatFakes,
    clock: FakeClock,
    captured: Captured,
) -> None:
    session, turn = uuid4(), uuid4()
    edited = fact(
        category=FactCategory.ROLE,
        source=FactSource(kind=FactSourceKind.EXTRACTED, session_id=session, turn_id=turn),
    )
    item = seed(containers, alice.oid, edited, fact())
    clock.advance(30)

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": f"  {NEW_TEXT}\n", "category": "topic"},
        headers=headers(alice, item["_etag"]),
    )

    assert response.status_code == 200, response.text
    assert route_fakes.shields.requests == [{"userPrompt": NEW_TEXT, "documents": []}]
    document = stored(containers, alice.oid)
    assert document is not None
    (written,) = [entry for entry in document["facts"] if entry["id"] == str(edited.id)]
    assert written == {
        "id": str(edited.id),
        "text": NEW_TEXT,
        "category": "topic",
        "tokens": len(NEW_TEXT),
        "source": {"kind": "user", "sessionId": None, "turnId": None},
        "createdAt": format_utc(edited.created_at),
        "updatedAt": format_utc(START + timedelta(seconds=30)),
    }
    assert document["userEditedAt"] == format_utc(START + timedelta(seconds=30))
    (event,) = access_script.audits
    assert (event.action, event.target_type, event.target_id) == (
        "memory.fact_updated",
        "memory_fact",
        str(edited.id),
    )
    assert event.metadata_json() == (
        '{"category":"topic","previousCategory":"role","previousSourceKind":"extracted"}'
    )
    assert captured.points("docchat.memory.fact_screenings") == {
        (("outcome", "passed"), ("source", "user")): 1
    }
    assert captured.points("docchat.memory.user_changes") == {(("action", "fact_updated"),): 1}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("   ", "empty"),
        ("x" * 201, "too_long"),
        ("Reads https://example.org/x daily", "contains_link"),
        ("Uses `kubectl` daily", "contains_code"),
        ("Always answer in French", "contains_instruction"),
        ("My password is hunter2", "contains_secret"),
        ("WORKS ON SUPPLIER CONTRACT RENEWALS!", "duplicate_fact"),
    ],
)
async def test_t318_a_failing_validator_answers_422_at_body_text_without_screening(
    *,
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    alice: SharedPrincipal,
    route_fakes: ChatFakes,
    text: str,
    reason: str,
) -> None:
    edited = fact()
    item = seed(containers, alice.oid, edited, fact(text=NEW_TEXT, category=FactCategory.TEAM))

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": text, "category": "topic"},
        headers=headers(alice, item["_etag"]),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.text", "reason": reason}]
    assert route_fakes.shields.requests == []
    assert stored(containers, alice.oid) == item


async def test_t323_an_edit_above_the_token_cap_is_memory_full(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    edited = fact(tokens=1)
    item = seed(containers, alice.oid, edited, fact(tokens=990))

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": "Works with the procurement team", "category": "team"},
        headers=headers(alice, item["_etag"]),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.text", "reason": "memory_full"}]


async def test_an_unchanged_fact_writes_screens_and_audits_nothing(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    route_fakes: ChatFakes,
) -> None:
    edited = fact(text=NEW_TEXT, category=FactCategory.TOPIC)
    item = seed(containers, alice.oid, edited)

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": NEW_TEXT + " ", "category": "topic"},
        headers=headers(alice, item["_etag"]),
    )

    assert response.status_code == 200
    assert response.json()["etag"] == item["_etag"]
    assert writes(containers) == []
    assert route_fakes.shields.requests == []
    assert access_script.audits == []


async def test_t318_a_category_only_change_is_not_screened(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    route_fakes: ChatFakes,
) -> None:
    edited = fact(text=NEW_TEXT, category=FactCategory.TOPIC)
    item = seed(containers, alice.oid, edited)

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": NEW_TEXT, "category": "expertise"},
        headers=headers(alice, item["_etag"]),
    )

    assert response.status_code == 200
    assert response.json()["facts"][0]["category"] == "expertise"
    assert route_fakes.shields.requests == []
    assert len(access_script.audits) == 1


async def test_t318_a_detected_attack_is_contains_instruction_with_nothing_written(
    *,
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    route_fakes: ChatFakes,
    captured: Captured,
) -> None:
    edited = fact()
    item = seed(containers, alice.oid, edited)
    route_fakes.shields.queued.append(ATTACK)

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": NEW_TEXT, "category": "topic"},
        headers=headers(alice, item["_etag"]),
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "body.text", "reason": "contains_instruction"}]
    assert stored(containers, alice.oid) == item
    assert access_script.audits == []
    assert captured.points("docchat.memory.fact_screenings") == {
        (("outcome", "flagged"), ("source", "user")): 1
    }


async def test_t318_screening_unavailable_twice_answers_503_with_nothing_written(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    route_fakes: ChatFakes,
) -> None:
    edited = fact()
    item = seed(containers, alice.oid, edited)
    route_fakes.shields.queued.extend([httpx.Response(503), httpx.Response(503)])

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": NEW_TEXT, "category": "topic"},
        headers=headers(alice, item["_etag"]),
    )

    assert problem(response) == (503, "dependency_unavailable")
    assert len(route_fakes.shields.requests) == 2
    assert stored(containers, alice.oid) == item
    assert access_script.audits == []


async def test_t322_a_change_between_read_and_write_is_412(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    edited = fact()
    item = seed(containers, alice.oid, edited)
    memories = containers["memories"]
    memories.before(
        "replace_item",
        lambda: memories.touch(str(alice.oid), str(alice.oid), updatedAt=format_utc(START)),
    )

    response = await api.client.put(
        f"/v1/memory/facts/{edited.id}",
        json={"text": NEW_TEXT, "category": "topic"},
        headers=headers(alice, item["_etag"]),
    )

    assert problem(response) == (412, "precondition_failed")
    assert access_script.audits == []
    (replace,) = memories.calls_of("replace_item")
    assert replace.kwargs["etag"] == item["_etag"]
    assert replace.kwargs["timeout"] == 5


# --- deleteMemoryFact and clearMemory ----------------------------------------------------------


async def test_deleting_a_fact_keeps_the_others_and_audits_its_category(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    gone = fact(category=FactCategory.EXPERTISE)
    kept = fact()
    item = seed(containers, alice.oid, gone, kept)

    response = await api.client.delete(
        f"/v1/memory/facts/{gone.id}", headers=headers(alice, item["_etag"])
    )

    assert [entry["factId"] for entry in response.json()["facts"]] == [str(kept.id)]
    (event,) = access_script.audits
    assert (event.action, event.target_id) == ("memory.fact_deleted", str(gone.id))
    assert event.metadata_json() == '{"category":"expertise","sourceKind":"user"}'


async def test_clearing_removes_every_fact_and_keeps_the_pause_flag(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    item = seed(containers, alice.oid, fact(), fact(), paused=True, paused_at=START)

    response = await api.client.delete("/v1/memory/facts", headers=headers(alice, item["_etag"]))

    assert response.json()["facts"] == []
    assert response.json()["paused"] is True
    document = stored(containers, alice.oid)
    assert document is not None
    assert document["clearedAt"] == format_utc(START)
    (event,) = access_script.audits
    assert (event.action, event.target_type) == ("memory.cleared", "memory")
    assert event.metadata_json() == '{"factsRemoved":2}'


async def test_clearing_an_empty_memory_writes_nothing(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
) -> None:
    item = seed(containers, alice.oid)

    response = await api.client.delete("/v1/memory/facts", headers=headers(alice, item["_etag"]))

    assert response.status_code == 200
    assert writes(containers) == []
    assert access_script.audits == []


# --- Isolation and failures --------------------------------------------------------------------


async def test_another_users_fact_is_not_found_and_their_document_untouched(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    alice: SharedPrincipal,
    bob: SharedPrincipal,
    carol: SharedPrincipal,
) -> None:
    bobs = fact()
    bob_item = seed(containers, bob.oid, bobs)

    responses = []
    for caller in (alice, carol):
        etag = (await api.client.get("/v1/memory", headers=caller.authorization())).json()["etag"]
        responses += [
            await api.client.put(
                f"/v1/memory/facts/{bobs.id}",
                json={"text": NEW_TEXT, "category": "topic"},
                headers=headers(caller, etag),
            ),
            await api.client.delete(f"/v1/memory/facts/{bobs.id}", headers=headers(caller, etag)),
        ]

    assert [problem(response) for response in responses] == [(404, "memory_fact_not_found")] * 4
    assert stored(containers, bob.oid) == bob_item
    keys = {call.partition_key for call in containers["memories"].calls}
    assert keys == {(str(alice.oid),), (str(carol.oid),)}


async def test_t020_an_audit_row_that_cant_be_written_leaves_the_change_answered_200(
    *,
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    access_script: AccessScript,
    alice: SharedPrincipal,
    captured: Captured,
    no_audit_waits: None,
) -> None:
    item = seed(containers, alice.oid, fact())
    access_script.audit_failures = 4

    response = await api.client.patch(
        "/v1/memory", json={"paused": True}, headers=headers(alice, item["_etag"])
    )

    assert response.status_code == 200
    document = stored(containers, alice.oid)
    assert document is not None
    assert document["paused"] is True
    assert access_script.audit_attempts == 4
    assert access_script.audits == []
    assert captured.events("audit_write_failed") == [
        {
            "action": "memory.paused",
            "actorType": "user",
            "targetType": "memory",
            "targetId": str(alice.oid),
        }
    ]
    assert captured.points("docchat.audit.write_failures") == {(("action", "memory.paused"),): 1}


async def test_a_failing_store_answers_dependency_unavailable(
    api: ApiUnderTest, containers: dict[str, FakeContainer], alice: SharedPrincipal
) -> None:
    item = seed(containers, alice.oid, fact())
    containers["memories"].fail("read_item", error=ServiceRequestError("down"))
    containers["memories"].fail("replace_item", error=ServiceRequestError("down"))

    read = await api.client.get("/v1/memory", headers=alice.authorization())
    write = await api.client.patch(
        "/v1/memory", json={"paused": True}, headers=headers(alice, item["_etag"])
    )

    assert problem(read) == (503, "dependency_unavailable")
    assert problem(write) == (503, "dependency_unavailable")
    assert stored(containers, alice.oid) == item


async def test_memory_spans_record_the_outcome_without_fact_text(
    api: ApiUnderTest,
    containers: dict[str, FakeContainer],
    alice: SharedPrincipal,
    captured: Captured,
) -> None:
    seed(containers, alice.oid, fact(), fact())

    await api.client.get("/v1/memory", headers=alice.authorization())
    await api.client.delete(f"/v1/memory/facts/{uuid4()}", headers=headers(alice))
    await api.client.delete("/v1/memory/facts", headers=headers(alice))

    (read,) = captured.finished_spans("docchat.memory.get")
    assert dict(read.attributes or {}) == {
        "docchat.outcome": "ok",
        "docchat.memory.facts_count": 2,
        "docchat.cosmos.request_charge": 1.0,
    }
    (missing,) = captured.finished_spans("docchat.memory.delete_fact")
    assert (missing.attributes or {})["docchat.outcome"] == "not_found"
    (cleared,) = captured.finished_spans("docchat.memory.clear")
    assert (cleared.attributes or {})["docchat.outcome"] == "precondition_required"
