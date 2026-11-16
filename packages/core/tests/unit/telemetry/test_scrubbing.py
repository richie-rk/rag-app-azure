"""The scrubbing safety net: T-486 (patterns, attach points, attribute rules, processor errors)
and the stack-trace half of T-487 (exceptions export frames and type names only)."""

from __future__ import annotations

import logging
import secrets
import traceback
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry import trace
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.trace import SpanKind, Status, StatusCode

from docchat_core.telemetry import scrubbing
from docchat_core.telemetry.scrubbing import (
    CAUSE_SENTENCE,
    CONTEXT_SENTENCE,
    REDACTED_ERROR,
    REDACTED_STACKTRACE,
    TRACEBACK_HEADER,
    ScrubbingLogFilter,
    attribute_store,
    catalogue_message,
    is_removed_attribute,
    qualified_type_name,
    reduce_stacktrace,
    safe_traceback,
    scrub,
    scrub_attributes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Every secret-shaped and content value is built at run time, so no source line a stack trace
# quotes can hold it.
_CANARY = "cnry-question-" + secrets.token_hex(4)
_MULTILINE = _CANARY + "\nsecond line " + _CANARY
_NOTE = "a note " + _CANARY
_JWT = "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 16
_KEY = "A" * 43 + "/" + "B" * 42 + "=="
_EMAIL = "someone" + "@" + "example.com"
_CONNECTION = "AccountKey=" + "s3cr3t+/k3y" + ";EndpointSuffix=core.windows.net"
_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "x" * 40 + "\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"token {_JWT} end", "token [redacted:jwt] end"),
        ("Authorization: Bearer abcdefgh12345678", "Authorization: Bearer [redacted]"),
        ("basic dXNlcjpwYXNzd29yZA==", "basic [redacted]"),
        (_CONNECTION, "AccountKey=[redacted];EndpointSuffix=core.windows.net"),
        ("Server=x;Pwd = hunter22;", "Server=x;Pwd=[redacted];"),
        ("SharedAccessSignature=sv%3D2024", "SharedAccessSignature=[redacted]"),
        (
            "https://a.blob.core.windows.net/c/b?sv=2024&sig=abcDEF%2B123&se=x",
            "https://a.blob.core.windows.net/c/b?sv=2024&sig=[redacted]&se=x",
        ),
        ("/callback?code=0.AAAA-xyz&state=1", "/callback?code=[redacted]&state=1"),
        (f"key {_KEY} end", "key [redacted:key] end"),
        ("api_key: abcd1234", "api_key: [redacted]"),
        ('{"client_secret": "zyxw9876"}', '{"client_secret": "[redacted]"}'),
        ("Ocp-Apim-Subscription-Key=0123456789abcdef", "Ocp-Apim-Subscription-Key=[redacted]"),
        (f"pem {_PEM} end", "pem [redacted:private-key] end"),
        (f"mail {_EMAIL} now", "mail [redacted:email] now"),
    ],
)
def test_t486_scrub_replaces_each_pattern(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "value",
    [
        "0f8fad5b-d9cb-469f-a165-70867728950e",
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "2026-10-30T12:34:56.789Z",
        "2026-10-30T12:34:56+02:00",
        "12345",
        "-3.25",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "docchat.chat.turn",
    ],
)
def test_t486_scrub_leaves_identifiers_timestamps_and_numbers(value: str) -> None:
    assert scrub(value) == value
    assert scrub(f"before {value} after") == f"before {value} after"


def test_scrub_is_stable_on_its_own_output() -> None:
    once = scrub(f"Bearer {_JWT} {_CONNECTION} {_EMAIL} password=abcdefgh")

    assert scrub(once) == once


# Attribute rules.


@pytest.mark.parametrize(
    "key",
    [
        "url.query",
        "db.statement",
        "db.query.text",
        "http.request.header.authorization",
        "http.request.header.x-ms-client-request-id",
        "http.response.header.set_cookie",
    ],
)
def test_t486_removed_attributes(key: str) -> None:
    assert is_removed_attribute(key)


@pytest.mark.parametrize(
    "key",
    [
        "http.request.header.content-type",
        "http.request.header.content_length",
        "http.request.header.traceparent",
        "http.response.header.content-type",
        "url.full",
        "docchat.outcome",
    ],
)
def test_t486_kept_attributes(key: str) -> None:
    assert not is_removed_attribute(key)


def test_t486_scrub_attributes_applies_the_attribute_rules() -> None:
    store: dict[str, Any] = {
        "url.full": "https://h/v1/x?search=" + _CANARY,
        "http.url": "https://h/a?b=c",
        "http.target": "/v1/x?q=1",
        "url.query": "search=" + _CANARY,
        "db.statement": "SELECT 1",
        "http.request.header.authorization": ("Bearer abcdefgh12345678",),
        "http.request.header.content-type": ("application/json",),
        "docchat.attempts": 2,
        "note": f"mail {_EMAIL}",
        "values": ["x", _EMAIL, 3],
    }

    scrub_attributes(store)

    assert store == {
        "url.full": "https://h/v1/x",
        "http.url": "https://h/a",
        "http.target": "/v1/x",
        "http.request.header.content-type": ("application/json",),
        "docchat.attempts": 2,
        "note": "mail [redacted:email]",
        "values": ("x", "[redacted:email]", 3),
    }


# Stack traces.


class _ContentError(Exception):
    pass


def _fail_inner() -> None:
    raise KeyError(_MULTILINE)


def _fail_with_cause() -> None:
    try:
        _fail_inner()
    except KeyError as cause:
        error = _ContentError(_CANARY)
        error.add_note(_NOTE)
        raise error from cause


def _fail_in_group() -> None:
    try:
        _fail_with_cause()
    except _ContentError as member:
        raise ExceptionGroup(_CANARY, [member, ValueError(_CANARY)]) from None


def _caught(action: Callable[[], None]) -> BaseException:
    try:
        action()
    except BaseException as exc:  # noqa: BLE001 - the test inspects whatever was raised
        return exc
    raise AssertionError("expected an exception")


def test_t487_safe_traceback_keeps_frames_and_type_names_only() -> None:
    text = safe_traceback(_caught(_fail_in_group))

    assert "cnry-" not in text
    assert "a note" not in text
    assert "second line" not in text
    # The group, its first member and that member's cause, and its second member.
    assert text.count(TRACEBACK_HEADER) == 4
    assert CAUSE_SENTENCE in text
    assert "builtins.KeyError" in text
    assert qualified_type_name(_ContentError) in text
    assert "builtins.ExceptionGroup" in text
    assert "builtins.ValueError" in text
    assert "in _fail_inner" in text
    assert "in _fail_with_cause" in text
    assert "in _fail_in_group" in text


def _handled() -> None:
    try:
        _fail_inner()
    except KeyError:
        raise ValueError(_CANARY) from None


def _chained() -> None:
    try:
        _fail_inner()
    except KeyError:
        raise ValueError(_CANARY)  # noqa: B904 - the implicit context is the case tested


def test_safe_traceback_honours_suppressed_context() -> None:
    assert CONTEXT_SENTENCE not in safe_traceback(_caught(_handled))
    assert "builtins.KeyError" not in safe_traceback(_caught(_handled))
    assert CONTEXT_SENTENCE in safe_traceback(_caught(_chained))
    assert "builtins.KeyError" in safe_traceback(_caught(_chained))


def test_safe_traceback_ends_a_cycle() -> None:
    first, second = ValueError(_CANARY), KeyError(_CANARY)
    first.__context__, second.__context__ = second, first

    text = safe_traceback(first)

    assert text.count(TRACEBACK_HEADER) == 2
    assert "cnry-" not in text


def test_safe_traceback_of_an_unraised_exception() -> None:
    assert safe_traceback(ValueError(_CANARY)) == f"{TRACEBACK_HEADER}\nbuiltins.ValueError\n"


def test_t487_reduce_stacktrace_drops_every_message_line_and_note() -> None:
    exc = _caught(_fail_in_group)
    rendered = "".join(traceback.format_exception(exc))
    assert _CANARY in rendered

    reduced = reduce_stacktrace(rendered)

    assert "cnry-" not in reduced
    assert "a note" not in reduced
    assert "second line" not in reduced
    assert "(2 sub-exceptions)" not in reduced
    assert "| ExceptionGroup" in reduced
    assert "| KeyError" in reduced
    assert f"| {qualified_type_name(_ContentError)}" in reduced
    assert "| ValueError" in reduced
    assert "+-+---------------- 1 ----------------" in reduced
    assert "+------------------------------------" in reduced
    assert CAUSE_SENTENCE in reduced
    assert reduced.count("in _fail_inner") == rendered.count("in _fail_inner")


def test_reduce_stacktrace_keeps_a_safe_traceback_as_it_is() -> None:
    text = safe_traceback(_caught(_fail_in_group))

    assert reduce_stacktrace(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "ValueError: " + _CANARY,
        _CANARY,
        "",
        '  File "x.py", line 1, in f\nValueError: ' + _CANARY,
    ],
)
def test_reduce_stacktrace_without_a_header_redacts_everything(text: str) -> None:
    assert reduce_stacktrace(text) == REDACTED_STACKTRACE


def test_catalogue_message() -> None:
    assert catalogue_message("internal_error") is not None
    assert catalogue_message("parse_failed") is not None
    assert catalogue_message("no_such_code") is None
    assert catalogue_message(None) is None


# The span processor.


@pytest.fixture
def tracer(span_exporter: InMemorySpanExporter) -> trace.Tracer:
    return trace.get_tracer("docchat.tests")


def test_t486_span_attributes_and_event_attributes_are_scrubbed(
    tracer: trace.Tracer, span_exporter: InMemorySpanExporter
) -> None:
    with tracer.start_as_current_span("GET /v1/x", kind=SpanKind.CLIENT) as span:
        span.set_attribute("url.full", "https://h/v1/x?token=" + _JWT)
        span.set_attribute("db.statement", "SELECT " + _CANARY)
        span.set_attribute("note", f"{_CONNECTION} {_EMAIL} {_JWT}")
        span.add_event("retry", {"note": "Bearer abcdefgh12345678"})

    (exported,) = span_exporter.get_finished_spans()
    assert dict(exported.attributes or {}) == {
        "url.full": "https://h/v1/x",
        "note": (
            "AccountKey=[redacted];EndpointSuffix=core.windows.net [redacted:email] [redacted:jwt]"
        ),
    }
    assert dict(exported.events[0].attributes or {}) == {"note": "Bearer [redacted]"}


def test_t487_an_exception_recorded_by_instrumentation_exports_no_message(
    tracer: trace.Tracer, span_exporter: InMemorySpanExporter
) -> None:
    with tracer.start_as_current_span("POST /v1/chat/turns", kind=SpanKind.SERVER) as span:
        span.record_exception(_caught(_fail_with_cause))

    (exported,) = span_exporter.get_finished_spans()
    attributes = dict(exported.events[0].attributes or {})
    assert attributes["exception.message"] == attributes["exception.type"]
    assert "cnry-" not in str(attributes)
    assert "in _fail_inner" in str(attributes["exception.stacktrace"])
    assert "a note" not in str(attributes["exception.stacktrace"])


def test_an_exception_event_takes_the_catalogue_message_of_the_spans_code(
    tracer: trace.Tracer, span_exporter: InMemorySpanExporter
) -> None:
    with tracer.start_as_current_span("docchat.ingestion.activity") as span:
        span.set_attribute("docchat.error_code", "internal_error")
        span.record_exception(ValueError(_CANARY))

    (exported,) = span_exporter.get_finished_spans()
    attributes = dict(exported.events[0].attributes or {})
    assert attributes["exception.message"] == catalogue_message("internal_error")
    # A value without a header (an exception that was never raised has no frames) is redacted.
    assert "cnry-" not in str(attributes)


def test_an_escaping_exception_leaves_only_its_type_in_the_status(
    tracer: trace.Tracer, span_exporter: InMemorySpanExporter
) -> None:
    with pytest.raises(ValueError, match="cnry-"), tracer.start_as_current_span("work"):
        raise ValueError(_CANARY)

    (exported,) = span_exporter.get_finished_spans()
    assert exported.status.status_code is StatusCode.ERROR
    assert exported.status.description == "ValueError"
    assert "cnry-" not in str(dict(exported.events[0].attributes or {}))


def test_a_status_description_that_is_not_a_type_or_code_is_redacted(
    tracer: trace.Tracer, span_exporter: InMemorySpanExporter
) -> None:
    with tracer.start_as_current_span("work") as span:
        span.set_status(Status(StatusCode.ERROR, f"lookup of {_CANARY} failed"))

    (exported,) = span_exporter.get_finished_spans()
    assert exported.status.description == "[redacted]"


def test_the_span_attribute_store_is_the_sdk_internal(
    tracer: trace.Tracer, span_exporter: InMemorySpanExporter
) -> None:
    # Fails when the SDK changes the internal the span processor writes through (section 3.6).
    with tracer.start_as_current_span("work") as span:
        span.set_attribute("a", "b")
        span.add_event("e", {"c": "d"})

    (exported,) = span_exporter.get_finished_spans()
    assert isinstance(exported._attributes, BoundedAttributes)
    assert attribute_store(exported) == {"a": "b"}
    assert attribute_store(exported.events[0]) == {"c": "d"}
    with pytest.raises(TypeError):
        attribute_store(object())


def _raise(text: str) -> str:
    raise RuntimeError(text)


def test_t486_a_failure_inside_the_span_processor_redacts_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    tracer: trace.Tracer,
    span_exporter: InMemorySpanExporter,
    metric_points: Callable[[str], list[Any]],
) -> None:
    monkeypatch.setattr(scrubbing, "scrub", _raise)

    with tracer.start_as_current_span("work") as span:
        span.set_attribute("note", _CANARY)

    (exported,) = span_exporter.get_finished_spans()
    assert dict(exported.attributes or {}) == {"note": REDACTED_ERROR}
    assert sum(point.value for point in metric_points("docchat.telemetry.scrub_errors")) == 1


def test_t486_a_stacktrace_without_a_header_counts_a_scrub_error(
    tracer: trace.Tracer,
    span_exporter: InMemorySpanExporter,
    metric_points: Callable[[str], list[Any]],
) -> None:
    with tracer.start_as_current_span("work") as span:
        span.add_event("exception", {"exception.stacktrace": _CANARY})

    (exported,) = span_exporter.get_finished_spans()
    assert dict(exported.events[0].attributes or {}) == {
        "exception.stacktrace": REDACTED_STACKTRACE
    }
    assert sum(point.value for point in metric_points("docchat.telemetry.scrub_errors")) == 1


# The log record processor and the logging filter.


@pytest.fixture
def otel_logger(log_capture: Any) -> Iterator[logging.Logger]:
    logger = logging.getLogger("docchat.tests.scrubbing")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = LoggingHandler(logger_provider=log_capture.provider)
    logger.addHandler(handler)
    yield logger
    logger.removeHandler(handler)


def test_t486_the_log_record_processor_scrubs_body_attributes_and_exceptions(
    otel_logger: logging.Logger, log_capture: Any
) -> None:
    try:
        _fail_with_cause()
    except _ContentError:
        otel_logger.warning(
            "body %s", _JWT, exc_info=True, extra={"docchat.note": _EMAIL, "url.query": "q=1"}
        )

    (exported,) = log_capture.exporter.get_finished_logs()
    record = exported.log_record
    attributes = dict(record.attributes or {})
    assert record.body == "body [redacted:jwt]"
    assert attributes["docchat.note"] == "[redacted:email]"
    assert "url.query" not in attributes
    assert attributes["exception.message"] == attributes["exception.type"]
    assert "cnry-" not in str(attributes)
    assert "in _fail_inner" in attributes["exception.stacktrace"]


def test_t487_an_exception_logged_with_exc_info_exports_frames_and_type_names_only(
    otel_logger: logging.Logger, log_capture: Any
) -> None:
    try:
        _fail_in_group()
    except ExceptionGroup:
        otel_logger.exception("job_failed")

    (exported,) = log_capture.exporter.get_finished_logs()
    attributes = dict(exported.log_record.attributes or {})
    stacktrace = str(attributes["exception.stacktrace"])
    assert "cnry-" not in str(attributes)
    assert "cnry-" not in str(exported.log_record.body)
    assert "a note" not in stacktrace
    assert "second line" not in stacktrace
    assert "ExceptionGroup" in stacktrace
    assert "KeyError" in stacktrace
    assert qualified_type_name(_ContentError).rsplit(".", 1)[-1] in stacktrace
    assert "in _fail_inner" in stacktrace
    assert "in _fail_with_cause" in stacktrace
    assert "in _fail_in_group" in stacktrace


def test_the_log_record_processor_uses_the_records_error_code(
    otel_logger: logging.Logger, log_capture: Any
) -> None:
    try:
        _fail_inner()
    except KeyError:
        otel_logger.exception("job_failed", extra={"docchat.error_code": "internal_error"})

    (exported,) = log_capture.exporter.get_finished_logs()
    attributes = dict(exported.log_record.attributes or {})
    assert attributes["exception.message"] == catalogue_message("internal_error")


def test_t486_the_log_filter_scrubs_message_args_extras_and_exception(
    otel_logger: logging.Logger,
) -> None:
    exc = _caught(_fail_with_cause)
    record = otel_logger.makeRecord(
        otel_logger.name,
        logging.WARNING,
        __file__,
        1,
        "mail %s and %s",
        (_EMAIL, 3),
        (type(exc), exc, exc.__traceback__),
        extra={"docchat.note": _JWT, "docchat.count": 4},
    )

    assert ScrubbingLogFilter().filter(record)

    assert record.msg == "mail [redacted:email] and 3"
    assert record.args == ()
    assert record.__dict__["docchat.note"] == "[redacted:jwt]"
    assert record.__dict__["docchat.count"] == 4
    assert record.exc_text == safe_traceback(exc)
    assert "cnry-" not in logging.Formatter().format(record)


def test_t486_a_failure_inside_the_log_filter_redacts_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    metric_points: Callable[[str], list[Any]],
) -> None:
    monkeypatch.setattr(scrubbing, "scrub", _raise)
    record = logging.LogRecord("docchat", logging.WARNING, __file__, 1, _CANARY, (), None)
    record.__dict__["docchat.note"] = _CANARY

    ScrubbingLogFilter().filter(record)

    assert record.msg == REDACTED_ERROR
    assert record.__dict__["docchat.note"] == REDACTED_ERROR
    assert sum(point.value for point in metric_points("docchat.telemetry.scrub_errors")) == 2


def test_t486_a_failure_inside_the_log_record_processor_redacts_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    otel_logger: logging.Logger,
    log_capture: Any,
    metric_points: Callable[[str], list[Any]],
) -> None:
    monkeypatch.setattr(scrubbing, "scrub", _raise)

    otel_logger.warning("plain", extra={"docchat.note": _CANARY})

    (exported,) = log_capture.exporter.get_finished_logs()
    assert exported.log_record.body == REDACTED_ERROR
    assert dict(exported.log_record.attributes or {})["docchat.note"] == REDACTED_ERROR
    assert sum(point.value for point in metric_points("docchat.telemetry.scrub_errors")) == 2
