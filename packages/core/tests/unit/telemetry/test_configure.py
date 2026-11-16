"""`configure_telemetry`: T-481 (sampler, propagator, distro options, resource), T-482 (no
connection string), T-491 (sampling by trace ID), the full in-memory pipeline of
`85-observability.md` section 3.7 items 5 and 6 at unit level, and no event from
`configure_telemetry` itself (part of T-495). Nothing here exports anywhere: the distro is
replaced by a recorder, and the test pipeline uses in-memory exporters."""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from azure.monitor.opentelemetry.exporter import ApplicationInsightsSampler
from azure.monitor.opentelemetry.exporter.export.trace._utils import _get_DJB2_sample_score
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk.metrics.export import NumberDataPoint
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from docchat_core.settings import SettingsValidationError
from docchat_core.telemetry import configure
from docchat_core.telemetry.configure import (
    LEVELLED_LOGGERS,
    StderrHandler,
    TestExporters,
    build_resource,
    configure_telemetry,
    instrumentation_options,
)
from docchat_core.telemetry.instruments import MILLISECOND_HISTOGRAM_VIEW, counter
from docchat_core.telemetry.records import LOGGER_NAME, log_event
from docchat_core.telemetry.scrubbing import (
    ScrubbingLogFilter,
    ScrubbingLogRecordProcessor,
    ScrubbingSpanProcessor,
)
from docchat_core.telemetry.settings import TelemetrySettings
from docchat_core.telemetry.state import STATE

_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://westeurope-5.in.applicationinsights.azure.com/"
)
_PRINCIPAL_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_CANARY = "cnry-question-" + secrets.token_hex(4)
_JWT = "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 16
_EMAIL = "someone" + "@" + "example.com"
_CONNECTION = "AccountKey=" + "s3cr3t+/k3y" + ";EndpointSuffix=core.windows.net"
_PROCESS_VARIABLES = (
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_PROPAGATORS",
    "AZURE_SDK_TRACING_IMPLEMENTATION",
)


@pytest.fixture(autouse=True)
def _isolated(
    settings_env: pytest.MonkeyPatch, logger_state: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """A process environment with the core settings, and every global `configure_telemetry`
    touches restored afterwards."""
    saved = {name: os.environ.get(name) for name in _PROCESS_VARIABLES}
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    settings_env.setenv("DOCCHAT_CORE_ENVIRONMENT", "dev")
    settings_env.setenv(
        "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID", "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    )
    monkeypatch.setattr(STATE, "strict", STATE.strict)
    monkeypatch.setattr(STATE, "service", STATE.service)
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", propagate.get_global_textmap())
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def distro(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        configure, "_configure_azure_monitor", lambda **options: calls.append(options)
    )
    return calls


@pytest.fixture
def instrumented(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    services: list[str] = []
    monkeypatch.setattr(configure, "instrument_libraries", services.append)
    return services


def _fail() -> None:
    raise LookupError(_CANARY)


def _settings(**values: Any) -> TelemetrySettings:
    return TelemetrySettings(build_commit=_COMMIT, **values)


# T-481


def test_t481_configure_telemetry_configures_the_distro(distro: list[dict[str, Any]]) -> None:
    settings = _settings(
        telemetry_sampling_ratio=0.5,
        APPLICATIONINSIGHTS_CONNECTION_STRING=_CONNECTION_STRING,
    )

    configure_telemetry("docchat-api", settings)

    assert os.environ["OTEL_TRACES_SAMPLER"] == "microsoft.fixed_percentage"
    assert os.environ["OTEL_TRACES_SAMPLER_ARG"] == "0.5"
    assert os.environ["OTEL_PROPAGATORS"] == "tracecontext"
    assert isinstance(propagate.get_global_textmap(), TraceContextTextMapPropagator)
    (options,) = distro
    resource = options.pop("resource")
    span_processors = options.pop("span_processors")
    log_record_processors = options.pop("log_record_processors")
    assert options == {
        "connection_string": _CONNECTION_STRING,
        "logger_name": "docchat",
        "views": [MILLISECOND_HISTOGRAM_VIEW],
        "instrumentation_options": {
            "azure_sdk": {"enabled": True},
            "httpx": {"enabled": True},
            "fastapi": {"enabled": True},
            "django": {"enabled": False},
            "flask": {"enabled": False},
            "psycopg2": {"enabled": False},
            "requests": {"enabled": False},
            "urllib": {"enabled": False},
            "urllib3": {"enabled": False},
        },
        "enable_live_metrics": False,
        "enable_performance_counters": True,
    }
    assert [type(p) for p in span_processors] == [ScrubbingSpanProcessor]
    assert [type(p) for p in log_record_processors] == [ScrubbingLogRecordProcessor]
    assert dict(resource.attributes) == {
        "service.name": "docchat-api",
        "service.version": _COMMIT,
        "deployment.environment.name": "dev",
    }
    assert "service.namespace" not in resource.attributes
    assert STATE.service == "docchat-api"


def test_t481_the_ingestion_app_has_no_fastapi_instrumentation(
    distro: list[dict[str, Any]],
) -> None:
    configure_telemetry(
        "docchat-ingestion", _settings(APPLICATIONINSIGHTS_CONNECTION_STRING=_CONNECTION_STRING)
    )

    (options,) = distro
    assert options["instrumentation_options"]["fastapi"] == {"enabled": False}
    assert options["resource"].attributes["service.name"] == "docchat-ingestion"
    assert os.environ["OTEL_TRACES_SAMPLER_ARG"] == "1.0"


def test_a_distro_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**options: object) -> None:
        raise RuntimeError("distro failed")

    monkeypatch.setattr(configure, "_configure_azure_monitor", fail)

    with pytest.raises(RuntimeError, match="distro failed"):
        configure_telemetry(
            "docchat-api", _settings(APPLICATIONINSIGHTS_CONNECTION_STRING=_CONNECTION_STRING)
        )


def test_an_unknown_service_is_refused(distro: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="docchat-api or docchat-ingestion"):
        configure_telemetry("docchat-web", _settings())  # type: ignore[arg-type]


def test_build_resource_needs_the_core_settings(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.delenv("DOCCHAT_CORE_ENVIRONMENT")

    with pytest.raises(SettingsValidationError):
        build_resource("docchat-api", _settings())


def test_the_loggers_of_section_1_6(distro: list[dict[str, Any]]) -> None:
    configure_telemetry("docchat-api", _settings(telemetry_log_level="WARNING"))
    configure_telemetry("docchat-api", _settings(telemetry_log_level="ERROR"))

    logger = logging.getLogger(LOGGER_NAME)
    assert logger.level == logging.ERROR
    assert not logger.propagate
    assert sum(isinstance(f, ScrubbingLogFilter) for f in logger.filters) == 1
    (stderr,) = [h for h in logger.handlers if isinstance(h, StderrHandler)]
    assert stderr.level == logging.WARNING
    assert any(isinstance(f, ScrubbingLogFilter) for f in stderr.filters)
    for name in LEVELLED_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name
    assert logging.getLogger("uvicorn.access").disabled


# T-482


def test_t482_without_a_connection_string_nothing_is_exported(
    distro: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], otel_globals: None
) -> None:
    configure_telemetry("docchat-api", _settings())

    assert distro == []
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
    logger = logging.getLogger(LOGGER_NAME)
    logger.info("an info record")
    logger.warning("token %s for %s", _JWT, _EMAIL)

    err = capsys.readouterr().err
    assert "an info record" not in err
    assert err == "WARNING docchat token [redacted:jwt] for [redacted:email]\n"


def test_t482_the_stderr_handler_writes_frames_only_for_an_exception(
    distro: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    configure_telemetry("docchat-api", _settings())

    try:
        _fail()
    except LookupError:
        logging.getLogger(LOGGER_NAME).exception("job_failed")

    err = capsys.readouterr().err
    assert "builtins.LookupError" in err
    assert "cnry-" not in err


# The in-memory pipeline.


@pytest.fixture
def pipeline(otel_globals: None, instrumented: list[str]) -> TestExporters:
    exporters = TestExporters()
    configure_telemetry("docchat-api", _settings(), exporters=exporters)
    return exporters


def test_test_exporters_are_fresh_and_not_collected() -> None:
    first, second = TestExporters(), TestExporters()

    assert first.spans is not second.spans
    assert TestExporters.__test__ is False


def test_t481_the_test_pipeline_uses_the_distro_sampler_and_the_resource(
    pipeline: TestExporters, instrumented: list[str]
) -> None:
    provider = trace.get_tracer_provider()

    assert isinstance(getattr(provider, "sampler", None), ApplicationInsightsSampler)
    assert instrumented == ["docchat-api"]
    with trace.get_tracer("docchat.tests").start_as_current_span("work"):
        pass
    (span,) = pipeline.spans.get_finished_spans()
    assert dict(span.resource.attributes) == {
        "service.name": "docchat-api",
        "service.version": _COMMIT,
        "deployment.environment.name": "dev",
    }


def test_configure_telemetry_emits_no_event(otel_globals: None, instrumented: list[str]) -> None:
    for service in ("docchat-api", "docchat-ingestion"):
        exporters = TestExporters()
        configure_telemetry(service, _settings(), exporters=exporters)

        assert exporters.logs.get_finished_logs() == ()
        assert exporters.spans.get_finished_spans() == ()


def test_records_reach_the_in_memory_exporters_scrubbed(
    pipeline: TestExporters, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    with trace.get_tracer("docchat.tests").start_as_current_span("work") as span:
        span.set_attribute("note", f"{_JWT} {_CONNECTION} {_EMAIL}")
        logger.warning("found %s and %s", _JWT, _EMAIL, extra={"docchat.note": _CONNECTION})
        try:
            _fail()
        except LookupError:
            logger.exception("job_failed")
        log_event(
            logger,
            "job_failed",
            logging.ERROR,
            **{"error.type": "builtins.LookupError", "docchat.error_code": "internal_error"},
        )

    exported = [record.log_record for record in pipeline.logs.get_finished_logs()]
    serialised = str([(r.body, dict(r.attributes or {})) for r in exported])
    (span_data,) = pipeline.spans.get_finished_spans()
    assert len(exported) == 3
    assert exported[0].body == "found [redacted:jwt] and [redacted:email]"
    assert dict(exported[0].attributes or {})["docchat.note"] == (
        "AccountKey=[redacted];EndpointSuffix=core.windows.net"
    )
    assert dict(span_data.attributes or {})["note"] == (
        "[redacted:jwt] AccountKey=[redacted];EndpointSuffix=core.windows.net [redacted:email]"
    )
    assert "cnry-" not in serialised
    assert "eyJ" not in serialised
    assert "@" not in serialised
    assert "in _fail" in serialised
    assert dict(exported[2].attributes or {}) == {
        "error.type": "builtins.LookupError",
        "docchat.error_code": "internal_error",
    }
    assert exported[2].trace_id == span_data.context.trace_id
    assert "cnry-" not in capsys.readouterr().err


def test_metrics_reach_the_in_memory_reader(pipeline: TestExporters) -> None:
    counter("docchat.chat.heartbeats").add()

    data = pipeline.metrics.get_metrics_data()
    assert data is not None
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert "docchat.chat.heartbeats" in names


def test_reconfiguring_keeps_one_otel_handler(otel_globals: None, instrumented: list[str]) -> None:
    configure_telemetry("docchat-api", _settings(), exporters=TestExporters())
    configure_telemetry("docchat-api", _settings(), exporters=TestExporters())

    handlers = logging.getLogger(LOGGER_NAME).handlers
    assert sum(isinstance(h, LoggingHandler) for h in handlers) == 1
    assert sum(isinstance(h, StderrHandler) for h in handlers) == 1


# T-491


def _request(trace_id: str, flag: str) -> None:
    carrier = {"traceparent": f"00-{trace_id}-00f067aa0ba902b7-{flag}"}
    context = TraceContextTextMapPropagator().extract(carrier)
    with trace.get_tracer("docchat.tests").start_as_current_span(
        "POST /v1/chat/turns", context=context, kind=SpanKind.SERVER
    ):
        pass


def _trace_ids(count: int) -> list[str]:
    return [secrets.token_hex(16) for _ in range(count)]


@pytest.mark.parametrize("flag", ["01", "00"])
def test_t491_sampling_follows_the_trace_id_not_the_flag(
    otel_globals: None, instrumented: list[str], flag: str
) -> None:
    exporters = TestExporters()
    configure_telemetry("docchat-api", _settings(telemetry_sampling_ratio=0.5), exporters=exporters)
    trace_ids = _trace_ids(200)

    for trace_id in trace_ids:
        _request(trace_id, flag)
        counter("docchat.chat.heartbeats").add()

    exported = {
        format(span.context.trace_id, "032x") for span in exporters.spans.get_finished_spans()
    }
    inside = {trace_id for trace_id in trace_ids if _get_DJB2_sample_score(trace_id) < 0.5}
    assert exported == inside
    assert 0 < len(exported) < len(trace_ids)
    data = exporters.metrics.get_metrics_data()
    assert data is not None
    (point,) = [
        point.value
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "docchat.chat.heartbeats"
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    ]
    assert point == len(trace_ids)


def test_t491_at_ratio_one_everything_is_exported(
    otel_globals: None, instrumented: list[str]
) -> None:
    exporters = TestExporters()
    configure_telemetry("docchat-api", _settings(), exporters=exporters)

    for trace_id in _trace_ids(50):
        _request(trace_id, "00")

    assert len(exporters.spans.get_finished_spans()) == 50


# Instrumentations.


class _Instrumentor:
    instrumented: list[str] = []  # noqa: RUF012 - shared by the fake entry points of one test

    def __init__(self, name: str) -> None:
        self.name = name

    def instrument(self) -> None:
        _Instrumentor.instrumented.append(self.name)


def _entry_point(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, load=lambda: lambda: _Instrumentor(name))


@pytest.mark.parametrize(
    ("service", "expected"),
    [("docchat-api", ["fastapi", "httpx"]), ("docchat-ingestion", ["httpx"])],
)
def test_t481_instrument_libraries_enables_only_the_listed_instrumentations(
    monkeypatch: pytest.MonkeyPatch, service: str, expected: list[str]
) -> None:
    names = ["fastapi", "httpx", "requests", "django", "flask", "urllib3", "logging"]
    monkeypatch.setattr(configure, "entry_points", lambda group: [_entry_point(n) for n in names])
    monkeypatch.setattr(_Instrumentor, "instrumented", [])
    monkeypatch.delenv("AZURE_SDK_TRACING_IMPLEMENTATION", raising=False)

    configure.instrument_libraries(service)  # type: ignore[arg-type]

    assert _Instrumentor.instrumented == expected
    assert os.environ["AZURE_SDK_TRACING_IMPLEMENTATION"] == "opentelemetry"


def test_t481_instrumentation_options_match_the_distro_call() -> None:
    assert set(instrumentation_options("docchat-api")) == {
        "azure_sdk",
        "httpx",
        "fastapi",
        "django",
        "flask",
        "psycopg2",
        "requests",
        "urllib",
        "urllib3",
    }
