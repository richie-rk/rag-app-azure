"""The record helpers: T-483 (registered keys, events and properties; raised in test mode, dropped
and counted in production) and the `record_failure` half of T-487."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from typing import Any, override

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanKind, StatusCode

from docchat_core.telemetry.attributes import CUSTOM_EVENT_NAME_ATTRIBUTE
from docchat_core.telemetry.records import (
    LOGGER_NAME,
    emit_event,
    log_event,
    record_failure,
    set_attributes,
    start_span,
)
from docchat_core.telemetry.scrubbing import catalogue_message, qualified_type_name

type MetricPoints = Callable[[str], list[Any]]

_CANARY = "cnry-question-" + secrets.token_hex(4)
_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_OTHER_UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records(logger_state: None) -> list[logging.LogRecord]:
    handler = _Records()
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return handler.records


def _attributes(record: logging.LogRecord) -> dict[str, object]:
    standard = vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message"}
    return {key: value for key, value in vars(record).items() if key not in standard}


def _dropped(metric_points: MetricPoints) -> int:
    return int(sum(point.value for point in metric_points("docchat.telemetry.dropped_attributes")))


_DOCCHAT = logging.getLogger(LOGGER_NAME)


# log_event


def test_log_event_emits_the_record_with_its_keys(records: list[logging.LogRecord]) -> None:
    log_event(
        _DOCCHAT,
        "job_failed",
        logging.ERROR,
        **{"error.type": "builtins.ValueError", "docchat.error_code": "internal_error"},
    )

    (record,) = records
    assert record.getMessage() == "job_failed"
    assert record.levelno == logging.ERROR
    assert _attributes(record) == {
        "error.type": "builtins.ValueError",
        "docchat.error_code": "internal_error",
    }
    assert record.exc_info is None


@pytest.mark.parametrize(
    ("event", "level", "attributes", "problem"),
    [
        ("job_failed", logging.ERROR, {"docchat.unknown": "x"}, "unregistered attribute key"),
        ("job_failed", logging.ERROR, {"note": _CANARY}, "unregistered attribute key"),
        ("job_failed", logging.ERROR, {"docchat.session_id": _UUID}, "not one of its attributes"),
        ("job_failed", logging.ERROR, {"error.type": "ValueError: x"}, "not a valid string"),
        ("job_failed", logging.ERROR, {"docchat.error_code": _CANARY}, "not a valid enum"),
        ("request_failed", logging.ERROR, {"docchat.error_code": "not_found"}, "not a valid"),
        ("scan_time_unusable", logging.WARNING, {"docchat.version_id": "X"}, "not a valid uuid"),
        ("job_failed", logging.WARNING, {}, "registered at level ERROR"),
        ("no_such_record", logging.ERROR, {}, "unregistered log record"),
        (f"failed {_CANARY}", logging.ERROR, {}, "a non-static name"),
    ],
)
def test_t483_log_event_raises_on_a_breach_in_test_mode(
    records: list[logging.LogRecord],
    event: str,
    level: int,
    attributes: dict[str, object],
    problem: str,
) -> None:
    with pytest.raises(ValueError, match=problem) as caught:
        log_event(_DOCCHAT, event, level, **attributes)

    assert "cnry-" not in str(caught.value)
    assert records == []


def test_log_event_refuses_another_logger(records: list[logging.LogRecord]) -> None:
    with pytest.raises(ValueError, match="'docchat' logger only"):
        log_event(logging.getLogger("docchat.other"), "ops_config_unreachable", logging.WARNING)


def test_t483_log_event_drops_and_counts_in_production(
    records: list[logging.LogRecord], production: None, metric_points: MetricPoints
) -> None:
    log_event(
        _DOCCHAT,
        "memory_unavailable",
        logging.WARNING,
        **{
            "docchat.session_id": _UUID,
            "docchat.turn_id": "not-a-uuid",
            "note": _CANARY,
            "error.type": "builtins.TimeoutError",
        },
    )

    (record,) = records
    assert _attributes(record) == {
        "docchat.session_id": _UUID,
        "error.type": "builtins.TimeoutError",
    }
    assert _dropped(metric_points) == 2


def test_t483_log_event_in_production_corrects_the_level_and_logger(
    records: list[logging.LogRecord], production: None, metric_points: MetricPoints
) -> None:
    log_event(logging.getLogger("docchat.other"), "ops_config_unreachable", logging.ERROR)

    (record,) = records
    assert record.name == LOGGER_NAME
    assert record.levelno == logging.WARNING
    assert _dropped(metric_points) == 2


def test_t483_log_event_in_production_drops_an_unregistered_record(
    records: list[logging.LogRecord], production: None, metric_points: MetricPoints
) -> None:
    log_event(_DOCCHAT, f"failed {_CANARY}", logging.ERROR)

    assert records == []
    assert _dropped(metric_points) == 1


# emit_event


def test_emit_event_writes_a_custom_event(records: list[logging.LogRecord]) -> None:
    emit_event("ingestion_queue_send_failed", runId=_UUID)

    (record,) = records
    assert record.getMessage() == "ingestion_queue_send_failed"
    assert record.levelno == logging.INFO
    assert _attributes(record) == {
        CUSTOM_EVENT_NAME_ATTRIBUTE: "ingestion_queue_send_failed",
        "runId": _UUID,
    }


def test_emit_event_ignores_the_logger_level(records: list[logging.LogRecord]) -> None:
    _DOCCHAT.setLevel(logging.ERROR)

    emit_event("ingestion_worker_started")

    (record,) = records
    assert _attributes(record) == {CUSTOM_EVENT_NAME_ATTRIBUTE: "ingestion_worker_started"}


@pytest.mark.parametrize(
    ("name", "properties", "problem"),
    [
        ("no_such_event", {}, "undeclared custom event"),
        ("ingestion_queue_send_failed", {"run_id": _UUID}, "not lowerCamelCase"),
        ("ingestion_queue_send_failed", {"projectId": _UUID}, "undeclared property"),
        ("ingestion_queue_send_failed", {"runId": _CANARY}, "not a valid uuid"),
        ("scanner_canary_result", {"result": "fine"}, "not a valid enum"),
        ("turn_feedback_saved", {"isChange": "true"}, "not a valid boolean"),
    ],
)
def test_t483_emit_event_raises_on_a_breach_in_test_mode(
    records: list[logging.LogRecord], name: str, properties: dict[str, object], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem) as caught:
        emit_event(name, **properties)

    assert "cnry-" not in str(caught.value)
    assert records == []


def test_t483_emit_event_drops_a_property_and_keeps_the_event_in_production(
    records: list[logging.LogRecord], production: None, metric_points: MetricPoints
) -> None:
    emit_event("followups_not_saved", sessionId=_UUID, turnId=_OTHER_UUID, comment=_CANARY)

    (record,) = records
    assert _attributes(record) == {
        CUSTOM_EVENT_NAME_ATTRIBUTE: "followups_not_saved",
        "sessionId": _UUID,
        "turnId": _OTHER_UUID,
    }
    assert _dropped(metric_points) == 1


def test_t483_emit_event_drops_an_undeclared_event_whole_in_production(
    records: list[logging.LogRecord], production: None, metric_points: MetricPoints
) -> None:
    emit_event("no_such_event", runId=_UUID)

    assert records == []
    assert _dropped(metric_points) == 1


# Spans


@pytest.fixture
def exported(span_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    return span_exporter


def test_start_span_opens_a_registered_span(exported: InMemorySpanExporter) -> None:
    with start_span(
        "docchat.ingestion.queue_message",
        kind=SpanKind.CONSUMER,
        **{"docchat.run_id": _UUID, "docchat.ingestion.dequeue_count": 1},
    ) as span:
        set_attributes(span, **{"docchat.ingestion.dispatcher_started": True})

    (finished,) = exported.get_finished_spans()
    assert finished.name == "docchat.ingestion.queue_message"
    assert finished.kind is SpanKind.CONSUMER
    assert dict(finished.attributes or {}) == {
        "docchat.run_id": _UUID,
        "docchat.ingestion.dequeue_count": 1,
        "docchat.ingestion.dispatcher_started": True,
    }


def test_sql_spans_share_one_registration(exported: InMemorySpanExporter) -> None:
    with start_span(
        "docchat.sql.ensure_user",
        kind=SpanKind.CLIENT,
        **{"db.system.name": "microsoft.sql_server", "docchat.outcome": "ok"},
    ):
        pass

    (finished,) = exported.get_finished_spans()
    assert finished.name == "docchat.sql.ensure_user"


def test_a_root_span_starts_its_own_trace_and_links_to_runs(
    exported: InMemorySpanExporter,
) -> None:
    tracer = trace.get_tracer("docchat.tests")
    with tracer.start_as_current_span("POST /v1/x") as parent:
        link = Link(parent.get_span_context())
        with start_span(
            "docchat.ingestion.dispatcher.sweep",
            links=[link],
            **{"docchat.ingestion.generation": 1},
        ):
            pass

    sweep, request = exported.get_finished_spans()
    assert sweep.parent is None
    assert sweep.context is not None
    assert request.context is not None
    assert sweep.context.trace_id != request.context.trace_id
    assert [item.context.trace_id for item in sweep.links] == [request.context.trace_id]


@pytest.mark.parametrize(
    ("name", "kind", "attributes", "problem"),
    [
        ("docchat.chat.unknown", SpanKind.INTERNAL, {}, "unregistered span"),
        ("docchat.chat.turn", SpanKind.CLIENT, {}, "registered as INTERNAL"),
        ("docchat.chat.turn", SpanKind.INTERNAL, {"docchat.run_id": _UUID}, "not one of its"),
        ("docchat.chat.turn", SpanKind.INTERNAL, {"docchat.outcome": "ok"}, "not a valid enum"),
        ("docchat.chat.turn", SpanKind.INTERNAL, {"docchat.unknown": 1}, "unregistered"),
        ("docchat.chat.search", SpanKind.INTERNAL, {"docchat.results": -1}, "not a valid"),
    ],
)
def test_start_span_raises_on_a_breach_in_test_mode(
    exported: InMemorySpanExporter,
    name: str,
    kind: SpanKind,
    attributes: dict[str, object],
    problem: str,
) -> None:
    with pytest.raises(ValueError, match=problem):
        start_span(name, kind=kind, **attributes)

    assert exported.get_finished_spans() == ()


def test_start_span_drops_and_counts_in_production(
    exported: InMemorySpanExporter, production: None, metric_points: MetricPoints
) -> None:
    with start_span(
        "docchat.chat.served_filter",
        kind=SpanKind.CLIENT,
        **{"docchat.candidates": 3, "docchat.served": "two", "note": _CANARY},
    ):
        pass

    (finished,) = exported.get_finished_spans()
    assert finished.kind is SpanKind.INTERNAL
    assert dict(finished.attributes or {}) == {"docchat.candidates": 3}
    assert _dropped(metric_points) == 3


def test_start_span_refuses_a_kind_or_links_of_another_type() -> None:
    with pytest.raises(TypeError, match="SpanKind"):
        start_span("docchat.chat.turn", kind="INTERNAL")
    with pytest.raises(TypeError, match="Link"):
        start_span("docchat.chat.turn", links=["00-ab-cd-01"])


def test_set_attributes_on_the_server_span_allows_any_registered_key(
    exported: InMemorySpanExporter,
) -> None:
    tracer = trace.get_tracer("docchat.tests")
    with tracer.start_as_current_span("GET /v1/me", kind=SpanKind.SERVER) as span:
        set_attributes(span, **{"enduser.id": _UUID, "docchat.auth.client": "web"})
        with pytest.raises(ValueError, match="unregistered attribute key"):
            set_attributes(span, **{"enduser.name": "someone"})

    (finished,) = exported.get_finished_spans()
    assert dict(finished.attributes or {}) == {"enduser.id": _UUID, "docchat.auth.client": "web"}


# Recording a failure (T-487).


def _fail() -> None:
    raise LookupError(_CANARY)


def test_t487_record_failure_exports_the_code_and_frames_only(
    exported: InMemorySpanExporter,
) -> None:
    with start_span("docchat.ingestion.activity") as span:
        try:
            _fail()
        except LookupError as exc:
            record_failure(span, exc, "internal_error")

    (finished,) = exported.get_finished_spans()
    (event,) = finished.events
    attributes = dict(event.attributes or {})
    assert event.name == "exception"
    assert attributes["exception.type"] == qualified_type_name(LookupError)
    assert attributes["exception.message"] == catalogue_message("internal_error")
    assert attributes["docchat.error_code"] == "internal_error"
    assert "in _fail" in str(attributes["exception.stacktrace"])
    assert "cnry-" not in str(attributes)
    assert finished.status.status_code is StatusCode.ERROR
    assert finished.status.description == "internal_error"


def test_record_failure_takes_an_item_code(exported: InMemorySpanExporter) -> None:
    with start_span("docchat.ingestion.activity") as span:
        record_failure(span, ValueError(_CANARY), "parse_failed")

    (finished,) = exported.get_finished_spans()
    attributes = dict(finished.events[0].attributes or {})
    assert attributes["exception.message"] == catalogue_message("parse_failed")


def test_record_failure_refuses_an_unknown_code_in_test_mode(
    exported: InMemorySpanExporter,
) -> None:
    with start_span("docchat.ingestion.activity") as span, pytest.raises(ValueError, match="code"):
        record_failure(span, ValueError(_CANARY), "no_such_code")


def test_record_failure_with_an_unknown_code_in_production(
    exported: InMemorySpanExporter, production: None, metric_points: MetricPoints
) -> None:
    with start_span("docchat.ingestion.activity") as span:
        record_failure(span, ValueError(_CANARY), "no_such_code")

    (finished,) = exported.get_finished_spans()
    attributes = dict(finished.events[0].attributes or {})
    assert attributes["exception.message"] == "builtins.ValueError"
    assert "docchat.error_code" not in attributes
    assert finished.status.description == "builtins.ValueError"
    assert _dropped(metric_points) == 1
