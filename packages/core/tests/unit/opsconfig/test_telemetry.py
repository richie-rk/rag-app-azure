"""Unit tests of the ops-config signals (`85-observability.md` sections 4.3, 4.8, 4.12 and 4.13)."""

from __future__ import annotations

import logging
from typing import Protocol

from opentelemetry.sdk.trace import ReadableSpan

from docchat_core.opsconfig.telemetry import (
    REFRESH_SPAN,
    log_invalid,
    record_refresh,
    refresh_span,
    report_invalid,
    report_unreachable,
)
from docchat_core.telemetry.attributes import EVENTS, LOG_EVENTS, METRICS, span_spec


class Captured(Protocol):
    """The `captured` fixture of this directory's `conftest.py`."""

    def finished_spans(self, name: str) -> list[ReadableSpan]: ...

    def counts(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], int]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...


def test_every_signal_is_registered() -> None:
    assert span_spec(REFRESH_SPAN) is not None
    assert "ops_config_invalid" in EVENTS
    assert {"ops_config_invalid", "ops_config_unreachable"} <= set(LOG_EVENTS)
    assert {"docchat.config.invalid", "docchat.opsconfig.refresh_failures"} <= set(METRICS)


def test_a_refresh_is_one_span_with_its_outcome(captured: Captured) -> None:
    with refresh_span() as span:
        record_refresh(span, "partial", 2)
    (finished,) = captured.finished_spans(REFRESH_SPAN)
    assert dict(finished.attributes or {}) == {
        "docchat.opsconfig.outcome": "partial",
        "docchat.opsconfig.invalid_areas": 2,
    }


def test_an_invalid_area_counts_emits_and_logs_each_key(captured: Captured) -> None:
    report_invalid("summary", ("summary.lease_seconds", "summary.timeout_seconds"))
    assert captured.counts("docchat.config.invalid") == {
        (("area", "summary"), ("key", "summary.lease_seconds")): 1,
        (("area", "summary"), ("key", "summary.timeout_seconds")): 1,
    }
    records = captured.records("ops_config_invalid")
    events = [record for record in records if "microsoft.custom_event.name" in record.__dict__]
    errors = [record for record in records if record not in events]
    assert [(event.__dict__["area"], event.__dict__["key"]) for event in events] == [
        ("summary", "summary.lease_seconds"),
        ("summary", "summary.timeout_seconds"),
    ]
    assert [(error.levelno, error.__dict__["docchat.opsconfig.key"]) for error in errors] == [
        (logging.ERROR, "summary.lease_seconds"),
        (logging.ERROR, "summary.timeout_seconds"),
    ]


def test_at_start_up_only_the_error_record_is_written(captured: Captured) -> None:
    log_invalid("chat", ("chat.max_turn_seconds",))
    (record,) = captured.records("ops_config_invalid")
    assert record.__dict__["docchat.opsconfig.area"] == "chat"
    assert "microsoft.custom_event.name" not in record.__dict__
    assert captured.counts("docchat.config.invalid") == {}


def test_an_unreachable_refresh_warns_and_counts(captured: Captured) -> None:
    report_unreachable()
    (record,) = captured.records("ops_config_unreachable")
    assert record.levelno == logging.WARNING
    assert captured.counts("docchat.opsconfig.refresh_failures") == {
        (("reason", "unreachable"),): 1
    }
