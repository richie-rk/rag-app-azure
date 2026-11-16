from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Protocol
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.http import audit
from docchat_api.http.audit import report_audit, write_audit, write_audit_after_commit
from docchat_api.http.context import SERVER_SPAN_KEY
from docchat_core.audit import AuditAction, AuditEvent

ADMIN = UUID("0ca20100-0000-4000-8000-000000000003")
PROJECT = UUID("1e6f7a8b-9c0d-4e2f-9a4b-5c6d7e8f9a65")


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


def _event(action: AuditAction = "project.restored") -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_type="user",
        actor_object_id=ADMIN,
        target_type="project",
        target_id=str(PROJECT),
        project_id=PROJECT,
        metadata=None,
    )


class _Connection:
    def __init__(self) -> None:
        self.executed: list[tuple[Any, Any]] = []

    def execute(self, statement: Any, parameters: Any = None) -> None:
        self.executed.append((statement, parameters))


def test_write_audit_inserts_in_the_callers_transaction_and_returns_the_event() -> None:
    connection = _Connection()
    event = _event()

    assert write_audit(connection, event) is event  # type: ignore[arg-type]
    ((statement, row),) = connection.executed
    assert "audit_events" in str(statement)
    assert row["action"] == "project.restored"
    assert row["project_id"] == PROJECT


def _request() -> Any:
    span = trace.get_tracer("test").start_span("GET /v1/admin/projects")
    return SimpleNamespace(scope={SERVER_SPAN_KEY: span}, state=SimpleNamespace()), span


def test_report_audit_marks_the_span_once_and_emits_admin_change(captured: Captured) -> None:
    request, span = _request()

    report_audit(request, [_event(), _event("memory.paused")], admin=True)
    span.end()

    (finished,) = captured.finished_spans("GET /v1/admin/projects")
    assert finished.attributes is not None
    assert finished.attributes["docchat.audit.action"] == "project.restored"
    assert captured.events("admin_change") == [
        {"action": "project.restored", "targetType": "project", "projectId": str(PROJECT)},
        {"action": "memory.paused", "targetType": "project", "projectId": str(PROJECT)},
    ]


def test_a_user_write_emits_no_admin_change(captured: Captured) -> None:
    request, span = _request()

    report_audit(request, [_event()], admin=False)
    span.end()

    assert captured.events("admin_change") == []


async def test_a_row_after_commit_marks_the_span_only_when_written(
    captured: Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = iter([True, False])

    async def write(engine: object, event: AuditEvent) -> bool:
        return next(outcomes)

    monkeypatch.setattr(audit, "write_audit_event_after_commit", write)
    written, written_span = _request()
    failed, failed_span = _request()

    assert await write_audit_after_commit(written, object(), _event("memory.resumed")) is True  # type: ignore[arg-type]
    assert await write_audit_after_commit(failed, object(), _event("memory.resumed")) is False  # type: ignore[arg-type]
    written_span.end()
    failed_span.end()

    first, second = captured.finished_spans("GET /v1/admin/projects")
    assert (first.attributes or {}).get("docchat.audit.action") == "memory.resumed"
    assert "docchat.audit.action" not in (second.attributes or {})
