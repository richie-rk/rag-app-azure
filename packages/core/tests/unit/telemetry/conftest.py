"""In-memory capture for the telemetry unit tests.

The OpenTelemetry providers are process globals that can be set once; these fixtures reset them
for one test and restore them afterwards, so each test installs its own in-memory pipeline and
nothing is exported anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import opentelemetry._logs._internal as logs_internal
import opentelemetry.metrics._internal as metrics_internal
import opentelemetry.trace as trace_api
import pytest
from opentelemetry import propagate
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricsData
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from docchat_core.telemetry.instruments import MILLISECOND_HISTOGRAM_VIEW
from docchat_core.telemetry.scrubbing import ScrubbingLogRecordProcessor, ScrubbingSpanProcessor
from docchat_core.telemetry.state import STATE

_DOCCHAT_LOGGERS = ("docchat", "uvicorn.access")
_LEVELLED_LOGGERS = (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "httpx",
    "httpcore",
    "httpx2",
    "openai",
    "opentelemetry",
    "uvicorn.error",
)


@pytest.fixture
def otel_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the global tracer, meter and logger providers and the propagator for this test."""
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", None)
    monkeypatch.setattr(logs_internal, "_LOGGER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(logs_internal, "_LOGGER_PROVIDER", None)
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", propagate.get_global_textmap())


@pytest.fixture
def logger_state() -> Iterator[None]:
    """Restore the loggers `configure_telemetry` changes."""
    names = (*_DOCCHAT_LOGGERS, *_LEVELLED_LOGGERS)
    saved = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
            list(logging.getLogger(name).handlers),
            list(logging.getLogger(name).filters),
        )
        for name in names
    }
    yield
    for name, (level, propagates, disabled, handlers, filters) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagates
        logger.disabled = disabled
        logger.handlers[:] = handlers
        logger.filters[:] = filters


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production mode: registry breaches are dropped and counted instead of raised."""
    monkeypatch.setattr(STATE, "strict", False)


@pytest.fixture
def api_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(STATE, "service", "docchat-api")


@pytest.fixture
def metric_reader(otel_globals: None) -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    metrics_internal.set_meter_provider(
        MeterProvider(metric_readers=[reader], views=[MILLISECOND_HISTOGRAM_VIEW])
    )
    return reader


@pytest.fixture
def span_exporter(otel_globals: None) -> InMemorySpanExporter:
    """A tracer provider with the scrubbing processor before an in-memory exporter, as
    `configure_telemetry` orders them."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(ScrubbingSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace_api.set_tracer_provider(provider)
    return exporter


@dataclass
class LogCapture:
    provider: LoggerProvider
    exporter: InMemoryLogRecordExporter


@pytest.fixture
def log_capture(otel_globals: None) -> LogCapture:
    exporter = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    provider = LoggerProvider()
    provider.add_log_record_processor(ScrubbingLogRecordProcessor())
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return LogCapture(provider=provider, exporter=exporter)


type MetricPoints = Callable[[str], list[Any]]


@pytest.fixture
def metric_points(metric_reader: InMemoryMetricReader) -> MetricPoints:
    """Every data point the in-memory reader holds for one metric, collected now."""

    def read(name: str) -> list[Any]:
        data: MetricsData | None = metric_reader.get_metrics_data()
        points: list[Any] = []
        if data is None:
            return points
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    if metric.name == name:
                        points.extend(metric.data.data_points)
        return points

    return read
