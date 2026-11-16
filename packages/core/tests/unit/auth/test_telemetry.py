"""Unit tests of the authentication signals (`85-observability.md` sections 4.3, 4.8, 4.12 and
4.13)."""

from __future__ import annotations

import logging
from typing import Any

from docchat_core.auth import telemetry
from docchat_core.telemetry.attributes import EVENTS, LOG_EVENTS, METRICS, span_spec


def test_every_signal_is_registered() -> None:
    assert span_spec(telemetry.VALIDATE_SPAN) is not None
    assert span_spec(telemetry.FETCH_SPAN) is not None
    assert telemetry.REFRESH_FAILED_EVENT in EVENTS
    assert telemetry.FETCH_FAILED_RECORD in LOG_EVENTS
    assert {
        "docchat.auth.validations",
        "docchat.auth.signing_key_fetches",
        "docchat.auth.signing_key_age",
    } <= set(METRICS)


def test_a_success_has_no_reason_on_the_span_and_none_in_the_count(captured: Any) -> None:
    with telemetry.validate_span() as span:
        telemetry.record_validation(span, "ok", None, "web")

    assert captured.span_attributes(telemetry.VALIDATE_SPAN) == [
        {"docchat.auth.outcome": "ok", "docchat.auth.client": "web"}
    ]
    assert captured.points("docchat.auth.validations") == {
        (("client", "web"), ("outcome", "ok"), ("reason", "none")): 1
    }


def test_a_refusal_carries_its_reason_and_client(captured: Any) -> None:
    with telemetry.validate_span() as span:
        telemetry.record_validation(span, "invalid", "bad_client", "unknown")

    assert captured.span_attributes(telemetry.VALIDATE_SPAN) == [
        {
            "docchat.auth.outcome": "invalid",
            "docchat.auth.reason": "bad_client",
            "docchat.auth.client": "unknown",
        }
    ]
    assert captured.points("docchat.auth.validations") == {
        (("client", "unknown"), ("outcome", "invalid"), ("reason", "bad_client")): 1
    }


def test_a_fetch_records_its_trigger_count_and_status(captured: Any) -> None:
    with telemetry.fetch_span("scheduled") as span:
        telemetry.record_fetch(span, "scheduled", "ok", key_count=6, status_code=200)

    assert captured.span_attributes(telemetry.FETCH_SPAN) == [
        {
            "docchat.auth.trigger": "scheduled",
            "docchat.auth.outcome": "ok",
            "docchat.auth.key_count": 6,
            "http.response.status_code": 200,
        }
    ]
    assert captured.points("docchat.auth.signing_key_fetches") == {
        (("outcome", "ok"), ("trigger", "scheduled")): 1
    }


def test_a_fetch_without_a_response_has_no_status(captured: Any) -> None:
    with telemetry.fetch_span("startup") as span:
        telemetry.record_fetch(span, "startup", "error", key_count=None, status_code=None)

    assert captured.span_attributes(telemetry.FETCH_SPAN) == [
        {"docchat.auth.trigger": "startup", "docchat.auth.outcome": "error"}
    ]


def test_a_failed_fetch_warns_and_emits_the_event(captured: Any) -> None:
    telemetry.report_fetch_failure(
        "unknown_kid", 502, "docchat_core.auth.keys.UnexpectedStatusError"
    )

    (record,) = captured.records(telemetry.FETCH_FAILED_RECORD)
    assert record.levelno == logging.WARNING
    assert record.__dict__["http.response.status_code"] == 502
    assert record.__dict__["error.type"] == "docchat_core.auth.keys.UnexpectedStatusError"
    assert record.__dict__["docchat.auth.trigger"] == "unknown_kid"
    assert captured.events(telemetry.REFRESH_FAILED_EVENT) == [
        {
            "trigger": "unknown_kid",
            "statusCode": 502,
            "errorType": "docchat_core.auth.keys.UnexpectedStatusError",
        }
    ]


def test_a_failure_without_a_response_reports_status_zero(captured: Any) -> None:
    telemetry.report_fetch_failure("scheduled", None, "builtins.TimeoutError")

    (record,) = captured.records(telemetry.FETCH_FAILED_RECORD)
    assert "http.response.status_code" not in record.__dict__
    assert captured.events(telemetry.REFRESH_FAILED_EVENT)[0]["statusCode"] == 0


def test_the_key_age_gauge_reads_its_source(captured: Any) -> None:
    ages: list[float | None] = [None]
    forget = telemetry.observe_key_age(lambda: ages[0])
    try:
        assert captured.points("docchat.auth.signing_key_age") == {}
        ages[0] = 42.0
        assert captured.points("docchat.auth.signing_key_age") == {(): 42.0}
    finally:
        forget()
