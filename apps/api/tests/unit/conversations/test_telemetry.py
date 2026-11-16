from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from docchat_api.conversations.telemetry import (
    Charges,
    known_code,
    record_abandoned,
    record_conflict,
    record_duplicate,
    record_finalized,
    record_superseded,
    record_trace_truncated,
    report_followups_not_saved,
)


class Captured(Protocol):
    metrics: InMemoryMetricReader

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


def _histogram_sums(captured: Captured, name: str) -> dict[tuple[tuple[str, object], ...], float]:
    data = captured.metrics.get_metrics_data()
    assert data is not None
    return {
        tuple(sorted((point.attributes or {}).items())): point.sum  # type: ignore[union-attr]
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    }


def test_charges_sum_the_request_units_of_every_call(captured: Captured) -> None:
    charges = Charges()
    charges.hook({"x-ms-request-charge": "2.5"}, None)
    charges.hook({"x-ms-request-charge": "1"}, {"id": "a"})
    charges.hook({}, None)
    charges.hook({"x-ms-request-charge": "not a number"}, None)

    assert charges.record("begin_turn") == 3.5
    assert _histogram_sums(captured, "docchat.conversations.request_charge") == {
        (("operation", "begin_turn"),): 3.5
    }


def test_only_catalogue_codes_reach_telemetry() -> None:
    assert known_code("turn_abandoned") == "turn_abandoned"
    assert known_code("made up") is None
    assert known_code(None) is None


def test_the_counters(captured: Captured) -> None:
    record_finalized("failed", "made up")
    record_finalized("completed", None)
    record_duplicate("conflict")
    record_abandoned("takeover")
    record_conflict("finalize_turn")
    record_superseded()
    record_trace_truncated()

    assert captured.points("docchat.conversations.turns_finalized") == {
        (("error_code", "none"), ("status", "failed")): 1,
        (("error_code", "none"), ("status", "completed")): 1,
    }
    assert captured.points("docchat.conversations.duplicates") == {(("outcome", "conflict"),): 1}
    assert captured.points("docchat.conversations.abandoned") == {(("source", "takeover"),): 1}
    assert captured.points("docchat.conversations.store_conflicts") == {
        (("operation", "finalize_turn"),): 1
    }
    assert captured.points("docchat.conversations.finalize_superseded") == {(): 1}
    assert captured.points("docchat.conversations.trace_truncated") == {(): 1}


def test_unsaved_followups_emit_their_event(captured: Captured) -> None:
    session, turn = uuid4(), uuid4()

    report_followups_not_saved(session, turn)

    assert captured.events("followups_not_saved") == [
        {"sessionId": str(session), "turnId": str(turn)}
    ]
    assert captured.points("docchat.conversations.followups_not_saved") == {(): 1}
