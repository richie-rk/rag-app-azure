"""In-memory telemetry for the ops-config unit tests.

The OpenTelemetry providers are process globals that can be set once; the `captured` fixture
resets them for one test, installs an in-memory span exporter and metric reader, and collects the
records of the `docchat` logger, so a test reads exactly what the store emitted.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import override

import opentelemetry.metrics._internal as metrics_internal
import opentelemetry.trace as trace_api
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from docchat_core.telemetry import LOGGER_NAME


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass
class Captured:
    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader
    collector: _Collector = field(default_factory=_Collector)

    def finished_spans(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.spans.get_finished_spans() if span.name == name]

    def counts(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], int]:
        """Each data point of a counter, by its sorted attributes."""
        data = self.metrics.get_metrics_data()
        found: dict[tuple[tuple[str, object], ...], int] = {}
        if data is None:
            return found
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != metric_name:
                        continue
                    for point in metric.data.data_points:
                        key = tuple(sorted((point.attributes or {}).items()))
                        found[key] = int(point.value)  # type: ignore[union-attr]
        return found

    def records(self, message: str) -> list[logging.LogRecord]:
        return [record for record in self.collector.records if record.getMessage() == message]


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Captured]:
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", None)
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    trace_api.set_tracer_provider(tracer_provider)
    reader = InMemoryMetricReader()
    metrics_internal.set_meter_provider(MeterProvider(metric_readers=[reader]))
    capture = Captured(spans=spans, metrics=reader)
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(capture.collector)
    try:
        yield capture
    finally:
        logger.removeHandler(capture.collector)
