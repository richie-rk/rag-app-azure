"""Unit tests of the provisioning signals (`85-observability.md` sections 4.3, 4.8 and 4.12)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from docchat_core.telemetry.attributes import EVENTS, METRICS, span_spec
from docchat_core.users import telemetry


def test_every_signal_is_registered() -> None:
    assert span_spec(telemetry.ENSURE_SPAN) is not None
    assert telemetry.PROVISIONED_EVENT in EVENTS
    assert "docchat.users.provisioned" in METRICS


def test_each_outcome_is_one_span_attribute(captured: Any) -> None:
    for outcome in ("skipped", "touched", "failed"):
        with telemetry.ensure_span() as span:
            telemetry.record_outcome(span, outcome)

    assert captured.span_attributes(telemetry.ENSURE_SPAN) == [
        {"docchat.users.outcome": "skipped"},
        {"docchat.users.outcome": "touched"},
        {"docchat.users.outcome": "failed"},
    ]
    assert captured.points("docchat.users.provisioned") == {}


def test_a_provisioning_counts_and_emits_the_event(captured: Any) -> None:
    oid = uuid4()
    with telemetry.ensure_span() as span:
        telemetry.report_provisioned(span, oid, 0)

    assert captured.span_attributes(telemetry.ENSURE_SPAN) == [
        {"docchat.users.outcome": "provisioned", "docchat.users.default_project_count": 0}
    ]
    assert captured.points("docchat.users.provisioned") == {(): 1}
    assert captured.events(telemetry.PROVISIONED_EVENT) == [
        {"oid": str(oid), "defaultProjectCount": 0}
    ]
