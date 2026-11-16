from __future__ import annotations

import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.http import telemetry
from docchat_core.auth import Principal


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


OID = UUID("0a11ce00-0000-4000-8000-000000000001")


def _finished(captured: Captured, name: str = "GET /v1/me") -> dict[str, Any]:
    (span,) = captured.finished_spans(name)
    return dict(span.attributes or {})


def test_the_caller_on_the_server_span(captured: Captured) -> None:
    principal = Principal(
        oid=OID, tid=uuid4(), azp=uuid4(), client="test", roles=frozenset({"User"})
    )
    with trace.get_tracer("t").start_as_current_span("GET /v1/me"):
        telemetry.record_caller(telemetry.current_server_span(), principal)

    assert _finished(captured) == {"enduser.id": str(OID), "docchat.auth.client": "test"}


def test_a_problem_counts_and_names_its_code(captured: Captured) -> None:
    with trace.get_tracer("t").start_as_current_span("GET /v1/me") as span:
        telemetry.record_problem(span, "too_many_requests", 429, "getCurrentUser")

    assert captured.points("docchat.api.problems") == {
        (("code", "too_many_requests"), ("operation", "getCurrentUser"), ("status", 429)): 1
    }
    assert _finished(captured)["docchat.error_code"] == "too_many_requests"


def test_request_failed_carries_the_type_and_code_only(captured: Captured) -> None:
    with trace.get_tracer("t").start_as_current_span("GET /v1/me") as span:
        telemetry.report_request_failed(span, KeyError("cnry-question-00000000"))

    (record,) = captured.records("request_failed")
    assert record.levelno == logging.ERROR
    assert record.__dict__["error.type"] == "builtins.KeyError"
    assert record.__dict__["docchat.error_code"] == "internal_error"
    (span_record,) = captured.finished_spans("GET /v1/me")
    (event,) = span_record.events
    assert event.name == "exception"
    assert "cnry-" not in str(dict(event.attributes or {}))


def test_the_write_check_span(captured: Captured) -> None:
    with telemetry.write_check_span() as span:
        telemetry.record_write_check(span, "allowed", 2)

    assert _finished(captured, "docchat.ratelimit.write_check") == {
        "docchat.ratelimit.outcome": "allowed",
        "docchat.ratelimit.attempts": 2,
    }


@pytest.mark.parametrize("reason", ["api", "write"])
def test_a_rejection_counts_its_reason(captured: Captured, reason: str) -> None:
    telemetry.record_rejection(reason)  # type: ignore[arg-type]

    assert captured.points("docchat.ratelimit.rejections") == {(("reason", reason),): 1}


def test_admin_change_names_the_project_only_when_there_is_one(captured: Captured) -> None:
    project = uuid4()

    telemetry.report_admin_change("index.created", "search_index", None)
    telemetry.report_admin_change("project.created", "project", project)

    assert captured.events("admin_change") == [
        {"action": "index.created", "targetType": "search_index"},
        {"action": "project.created", "targetType": "project", "projectId": str(project)},
    ]


def test_the_audit_action_on_the_server_span(captured: Captured) -> None:
    with trace.get_tracer("t").start_as_current_span("GET /v1/me") as span:
        telemetry.record_audit_action(span, "user.provisioned")

    assert _finished(captured) == {"docchat.audit.action": "user.provisioned"}
