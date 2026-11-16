"""Unit tests of the audit failure report (`85-observability.md`: `audit_write_failed`,
`docchat.audit.write_failures`)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

import pytest

from docchat_core.audit import telemetry
from docchat_core.audit.models import (
    AuditEvent,
    MemoryFactsExtractedMetadata,
    ProjectArchivedMetadata,
)
from docchat_core.telemetry.attributes import EVENTS, LOG_EVENTS, METRICS

U = "3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998"
S = "11111111-2222-4333-8444-555555555555"
T = "66666666-7777-4888-9999-aaaaaaaaaaaa"


@dataclass
class Recorded:
    counted: list[dict[str, object]] = field(default_factory=list)
    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    records: list[tuple[str, int, dict[str, object]]] = field(default_factory=list)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Recorded:
    seen = Recorded()

    class FakeCounter:
        def add(self, amount: int, /, **dimensions: object) -> None:
            assert amount == 1
            seen.counted.append(dimensions)

    def emit(name: str, /, **properties: object) -> None:
        seen.events.append((name, properties))

    def log(logger: logging.Logger, name: str, level: int, /, **attributes: object) -> None:
        assert logger is telemetry.LOGGER
        seen.records.append((name, level, attributes))

    monkeypatch.setattr(telemetry, "WRITE_FAILURES", FakeCounter())
    monkeypatch.setattr(telemetry, "emit_event", emit)
    monkeypatch.setattr(telemetry, "log_event", log)
    return seen


def test_memory_failure_names_session_and_turn(recorded: Recorded) -> None:
    event = AuditEvent(
        action="memory.facts_extracted",
        actor_type="system",
        actor_object_id=UUID(U),
        subject_object_id=UUID(U),
        target_type="memory",
        target_id=U,
        metadata=MemoryFactsExtractedMetadata(
            added_fact_ids=[UUID(U)],
            replaced_fact_ids=[],
            categories=["topic"],
            session_id=UUID(S),
            turn_id=UUID(T),
        ),
    )
    telemetry.report_write_failure(event)
    assert recorded.counted == [{"action": "memory.facts_extracted"}]
    assert recorded.events == [
        (
            "audit_write_failed",
            {
                "action": "memory.facts_extracted",
                "actorType": "system",
                "targetType": "memory",
                "targetId": U,
                "sessionId": S,
                "turnId": T,
            },
        )
    ]
    assert recorded.records == [
        (
            "audit_write_failed",
            logging.ERROR,
            {
                "docchat.audit.action": "memory.facts_extracted",
                "docchat.session_id": S,
                "docchat.turn_id": T,
            },
        )
    ]


def test_other_failures_carry_no_session(recorded: Recorded) -> None:
    event = AuditEvent(
        action="project.archived",
        actor_type="user",
        actor_object_id=UUID(U),
        target_type="project",
        target_id=U,
        project_id=UUID(U),
        metadata=ProjectArchivedMetadata(was_default=False),
    )
    telemetry.report_write_failure(event)
    ((_, properties),) = recorded.events
    ((_, _, record),) = recorded.records
    assert "sessionId" not in properties
    assert record == {"docchat.audit.action": "project.archived"}


def test_the_registered_signals_accept_the_report() -> None:
    """The real counter, event and record take these names, so production drops nothing."""
    assert "audit_write_failed" in EVENTS
    assert "audit_write_failed" in LOG_EVENTS
    assert METRICS["docchat.audit.write_failures"].dimensions == frozenset({"action"})
