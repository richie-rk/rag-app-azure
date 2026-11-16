"""Fixtures shared by the unit tests of `docchat_core`."""

from __future__ import annotations

import logging
import os
from collections import defaultdict, deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

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

CUSTOM_EVENT_NAME = "microsoft.custom_event.name"

type SpecTable = Callable[[str, str], list[list[str]]]

_PLATFORM_HOST_VARIABLES = ("WEBSITE_SITE_NAME", "WEBSITE_INSTANCE_ID")


def _spec_directory() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("docs/spec not found above the tests")


def _cell(text: str) -> str:
    # A cell that is one code span (a code name) loses its backticks; any other cell is kept.
    text = text.strip()
    if text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        return text[1:-1]
    return text


@pytest.fixture(scope="session")
def spec_table() -> SpecTable:
    """Reads the body rows of the first table under a heading of a design document, so a
    catalogue test compares the code with the specification's own text."""

    def read(file_name: str, heading: str) -> list[list[str]]:
        lines = (_spec_directory() / file_name).read_text(encoding="utf-8").splitlines()
        start = lines.index(heading) + 1
        rows: list[list[str]] = []
        for line in lines[start:]:
            if line.startswith("#"):
                break
            if not line.startswith("|"):
                if rows:
                    break
                continue
            if set(line.strip()) <= set("|-: "):
                continue
            rows.append([_cell(cell) for cell in line.strip().strip("|").split(" | ")])
        # The first row is the header.
        return rows[1:]

    return read


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A process environment with no `DOCCHAT_*` setting and no App Service host variable, so a
    settings test sees only what it sets."""
    for name in list(os.environ):
        if name.startswith("DOCCHAT_") or name in _PLATFORM_HOST_VARIABLES:
            monkeypatch.delenv(name)
    return monkeypatch


# --- Telemetry --------------------------------------------------------------------------------


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass
class Captured:
    """What one test emitted: finished spans, metric points and `docchat` log records."""

    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader
    collector: _Collector = field(default_factory=_Collector)

    def finished_spans(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.spans.get_finished_spans() if span.name == name]

    def span_attributes(self, name: str) -> list[dict[str, Any]]:
        return [dict(span.attributes or {}) for span in self.finished_spans(name)]

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]:
        """Each data point's value (a counter's sum, a gauge's last value) by its sorted
        attributes."""
        data = self.metrics.get_metrics_data()
        found: dict[tuple[tuple[str, object], ...], float] = {}
        if data is None:
            return found
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != metric_name:
                        continue
                    for point in metric.data.data_points:
                        key = tuple(sorted((point.attributes or {}).items()))
                        found[key] = point.value  # type: ignore[union-attr]
        return found

    def records(self, message: str) -> list[logging.LogRecord]:
        """The log records named `message`, custom events excluded."""
        return [
            record
            for record in self.collector.records
            if record.getMessage() == message and CUSTOM_EVENT_NAME not in record.__dict__
        ]

    def events(self, name: str) -> list[dict[str, Any]]:
        """The properties of each custom event named `name`."""
        return [
            {
                key: value
                for key, value in record.__dict__.items()
                if key[:1].islower()
                and "." not in key
                and not key.startswith("otel")
                and key not in _LOG_RECORD_FIELDS
            }
            for record in self.collector.records
            if record.__dict__.get(CUSTOM_EVENT_NAME) == name
        ]


_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Captured]:
    """In-memory span and metric pipelines and the `docchat` logger's records for one test; the
    OpenTelemetry providers are process globals, reset here and restored afterwards."""
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


# --- A data-access layer without SQL -------------------------------------------------------------


@dataclass
class SqlCall:
    """One call a repository function made on the engine."""

    method: str
    name: str
    statement: object
    parameters: Mapping[str, Any] | None
    retry: str


@dataclass
class _Result:
    rowcount: int


class FakeConnection:
    """Answers each `execute` with the next queued row count and records the statement."""

    def __init__(self, rowcounts: tuple[int, ...]) -> None:
        self._rowcounts = deque(rowcounts)
        self.executed: list[tuple[object, Mapping[str, Any] | None]] = []

    def execute(self, statement: object, parameters: Mapping[str, Any] | None = None) -> _Result:
        self.executed.append((statement, parameters))
        return _Result(self._rowcounts.popleft() if self._rowcounts else 1)


class FakeSqlEngine:
    """Stands in for `SqlEngine`: each unit of work is answered, by its statement-set name, with
    the next result queued for that name. An exception is raised; a `FakeConnection` given to
    `run` has the unit of work run on it."""

    def __init__(self) -> None:
        self.calls: list[SqlCall] = []
        self._results: defaultdict[str, deque[object]] = defaultdict(deque)

    def queue(self, name: str, *results: object) -> None:
        self._results[name].extend(results)

    def connection(self, *rowcounts: int) -> FakeConnection:
        return FakeConnection(rowcounts)

    def names(self) -> list[str]:
        return [call.name for call in self.calls]

    def _next(self, name: str) -> object:
        queued = self._results[name]
        if not queued:
            raise AssertionError(f"no result queued for the unit of work {name!r}")
        result = queued.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    async def fetch_one(
        self,
        statement: object,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: str = "transient",
    ) -> Any:
        self.calls.append(SqlCall("fetch_one", name, statement, parameters, retry))
        return self._next(name)

    async def fetch_all(
        self,
        statement: object,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: str = "transient",
    ) -> Any:
        self.calls.append(SqlCall("fetch_all", name, statement, parameters, retry))
        return self._next(name)

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self.calls.append(SqlCall("run", name, work, None, retry))
        result = self._next(name)
        if isinstance(result, FakeConnection):
            return work(result)
        return result


@pytest.fixture
def fake_sql() -> FakeSqlEngine:
    return FakeSqlEngine()
