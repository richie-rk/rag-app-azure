"""Unit tests of the authorisation signals (`85-observability.md` sections 4.3, 4.8 and 4.12)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from docchat_core.access import telemetry
from docchat_core.telemetry.attributes import EVENTS, METRICS, span_spec

PROJECT = uuid4()
INDEX = uuid4()


def test_every_signal_is_registered() -> None:
    for name in (telemetry.AUTHORIZE_SPAN, telemetry.RESOLVE_SPAN, telemetry.REACHABLE_SPAN):
        assert span_spec(name) is not None
    assert telemetry.OVERRIDE_EVENT in EVENTS
    assert {"docchat.access.denials", "docchat.access.index_overrides_used"} <= set(METRICS)


def test_an_allowed_and_a_refused_project(captured: Any) -> None:
    with telemetry.authorize_span(PROJECT) as span:
        telemetry.record_authorize(span, "member")
    with telemetry.authorize_span(PROJECT) as span:
        telemetry.record_authorize(span, None)

    assert captured.span_attributes(telemetry.AUTHORIZE_SPAN) == [
        {
            "docchat.project_id": str(PROJECT),
            "docchat.access.outcome": "allowed",
            "docchat.access.via": "member",
        },
        {"docchat.project_id": str(PROJECT), "docchat.access.outcome": "not_found"},
    ]


def test_a_resolution_names_the_index_its_source_and_the_cache(captured: Any) -> None:
    with telemetry.resolve_span(PROJECT) as span:
        telemetry.record_choice(span, INDEX, "project_default")
        telemetry.record_resolution(span, resolved=True, cache="hit")
    with telemetry.resolve_span(PROJECT) as span:
        telemetry.record_resolution(span, resolved=False, cache=None)

    assert captured.span_attributes(telemetry.RESOLVE_SPAN) == [
        {
            "docchat.project_id": str(PROJECT),
            "docchat.index_id": str(INDEX),
            "docchat.access.index_source": "project_default",
            "docchat.access.outcome": "ok",
            "docchat.access.index_cache": "hit",
        },
        {"docchat.project_id": str(PROJECT), "docchat.access.outcome": "unavailable"},
    ]


def test_an_override_counts_and_emits_its_event(captured: Any) -> None:
    oid = uuid4()
    telemetry.report_override_used(oid, PROJECT, INDEX, datetime(2026, 11, 7, 9, tzinfo=UTC))

    assert captured.points("docchat.access.index_overrides_used") == {(): 1}
    assert captured.events(telemetry.OVERRIDE_EVENT) == [
        {
            "oid": str(oid),
            "projectId": str(PROJECT),
            "indexId": str(INDEX),
            "expiresAt": "2026-11-07T09:00:00.000Z",
        }
    ]


def test_reachable_projects_carry_their_count(captured: Any) -> None:
    with telemetry.reachable_span() as span:
        telemetry.record_reachable(span, 3)
    assert captured.span_attributes(telemetry.REACHABLE_SPAN) == [
        {"docchat.access.project_count": 3}
    ]


def test_a_denial_is_counted_by_kind_and_operation(captured: Any) -> None:
    telemetry.record_denial("admin_required", "adminListProjects")
    telemetry.record_denial("not_found", "createChatTurn")
    telemetry.record_denial("not_found", "createChatTurn")

    assert captured.points("docchat.access.denials") == {
        (("kind", "admin_required"), ("operation", "adminListProjects")): 1,
        (("kind", "not_found"), ("operation", "createChatTurn")): 2,
    }
