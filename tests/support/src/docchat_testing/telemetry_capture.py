"""Telemetry capture and the session-wide canary audit (`84-testing.md` section 2.10;
`85-observability.md` section 3.7 item 7).

Every content value in tests comes from `builders.canary(kind)`, which embeds
`cnry-<kind>-<8 hex>`. During an integration session the plugin installs in-memory span, log and
metric exporters through `configure_telemetry(exporters=…)`, so everything the code under test
exports passes through the production pipeline (registry checks, scrubbing processors) and lands
here. At session end the audit reads every exported string: a canary, an e-mail-shaped value or a
JWT-shaped value in any of them means content or a credential would have reached Application
Insights, and the session fails naming the item.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import pytest

from docchat_core.settings import load_settings
from docchat_core.telemetry import (
    ServiceName,
    TelemetrySettings,
    TestExporters,
    configure_telemetry,
)
from docchat_core.telemetry.scrubbing import PATTERNS
from docchat_testing.builders.canaries import CANARY_PREFIX

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import ReadableLogRecord
    from opentelemetry.sdk.trace import ReadableSpan

SESSION_SERVICE: Final[ServiceName] = "docchat-api"
"""The session pipeline is the API's, so the FastAPI instrumentation records the server spans of
the in-process API; the ingestion code under test exports through the same providers."""

SESSION_ENVIRONMENT: Final[Mapping[str, str]] = {
    "DOCCHAT_CORE_ENVIRONMENT": "local",
    "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": "00000000-0000-0000-0000-000000000000",
    "DOCCHAT_BUILD_COMMIT": "0" * 40,
}
"""What `configure_telemetry` reads beside its settings (the resource's environment, the build
commit), used only where the process doesn't set them already: a test process is a `local` run
with no release commit."""


def _pattern(replacement: str) -> re.Pattern[str]:
    # The audit looks for exactly what the scrubbing patterns of section 3.6 redact.
    return next(pattern for pattern, replaced in PATTERNS if replaced == replacement)


type LeakKind = Literal["canary", "e-mail address", "JWT"]

EMAIL_PATTERN: Final = _pattern("[redacted:email]")
JWT_PATTERN: Final = _pattern("[redacted:jwt]")
_SHAPES: Final[tuple[tuple[LeakKind, re.Pattern[str]], ...]] = (
    ("e-mail address", EMAIL_PATTERN),
    ("JWT", JWT_PATTERN),
)

CAPTURE_KEY: Final = pytest.StashKey["TelemetryCapture"]()
"""Where the session's capture lives on the pytest `Config`, once installed."""

_EXCERPT_LENGTH: Final = 80


@dataclass(frozen=True, slots=True)
class TelemetryItem:
    """One exported span, log record or metric data point, as the strings it carries."""

    label: str
    strings: tuple[tuple[str, str], ...]
    """(where in the item, the string) pairs: names, keys and values."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One string of one item that holds content or a credential."""

    item: str
    where: str
    kind: LeakKind
    excerpt: str

    def describe(self) -> str:
        return f"{self.item}: {self.where} holds a {self.kind} ({self.excerpt!r})"


def _strings(where: str, value: object) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, bytes):
        yield where, value.decode("utf-8", errors="replace")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            yield f"{where} key", str(key)
            yield from _strings(f"{where}.{key}", nested)
    elif isinstance(value, Sequence):
        for index, nested in enumerate(value):
            yield from _strings(f"{where}[{index}]", nested)


def span_item(span: ReadableSpan) -> TelemetryItem:
    """A span's name, attributes, status description, and each event's and link's attributes
    (an `exception` event's `exception.message` and `exception.stacktrace` included)."""
    context = span.context
    ids = "" if context is None else f" trace {context.trace_id:032x} span {context.span_id:016x}"
    strings: list[tuple[str, str]] = [("name", span.name)]
    strings.extend(_strings("attributes", dict(span.attributes or {})))
    if span.status.description:
        strings.append(("status description", span.status.description))
    for index, event in enumerate(span.events):
        strings.append((f"events[{index}] name", event.name))
        strings.extend(_strings(f"events[{index}] {event.name}", dict(event.attributes or {})))
    for index, link in enumerate(span.links):
        strings.extend(_strings(f"links[{index}]", dict(link.attributes or {})))
    return TelemetryItem(label=f"span {span.name!r}{ids}", strings=tuple(strings))


def log_item(index: int, readable: ReadableLogRecord) -> TelemetryItem:
    """A log record's body, event name and attributes."""
    record = readable.log_record
    scope = readable.instrumentation_scope
    origin = "" if scope is None else f" from {scope.name!r}"
    strings: list[tuple[str, str]] = list(_strings("body", record.body))
    if record.event_name:
        strings.append(("event name", record.event_name))
    strings.extend(_strings("attributes", dict(record.attributes or {})))
    return TelemetryItem(label=f"log record #{index}{origin}", strings=tuple(strings))


def metric_items(exporters: TestExporters) -> Iterator[TelemetryItem]:
    """Each metric data point's attribute set."""
    data = exporters.metrics.get_metrics_data()
    if data is None:
        return
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                for index, point in enumerate(metric.data.data_points):
                    # Every point type carries `attributes`; the SDK's union types don't say so.
                    attributes = getattr(point, "attributes", None) or {}
                    yield TelemetryItem(
                        label=f"metric {metric.name!r} data point #{index}",
                        strings=tuple(_strings("attributes", dict(attributes))),
                    )


def find_leaks(item: TelemetryItem) -> list[Finding]:
    """Every string of `item` holding a canary, an e-mail-shaped or a JWT-shaped value."""
    findings: list[Finding] = []
    for where, text in item.strings:
        canary = text.find(CANARY_PREFIX)
        if canary >= 0:
            findings.append(
                Finding(item.label, where, "canary", text[canary : canary + _EXCERPT_LENGTH])
            )
        for kind, pattern in _SHAPES:
            match = pattern.search(text)
            if match is not None:
                findings.append(Finding(item.label, where, kind, match.group(0)[:_EXCERPT_LENGTH]))
    return findings


def format_findings(findings: Sequence[Finding]) -> str:
    """The audit's failure text: one line per leaking string."""
    lines = [f"Canary audit: {len(findings)} exported telemetry string(s) hold content:"]
    lines.extend(f"  {finding.describe()}" for finding in findings)
    return "\n".join(lines)


@contextmanager
def _session_environment() -> Iterator[None]:
    """Supply the variables `configure_telemetry` reads beside its settings, only while it runs,
    so no test later sees settings it didn't set."""
    added = [name for name in SESSION_ENVIRONMENT if not os.environ.get(name)]
    for name in added:
        os.environ[name] = SESSION_ENVIRONMENT[name]
    try:
        yield
    finally:
        for name in added:
            os.environ.pop(name, None)


class _Serialised[T]:
    """The items of an exporter's finished records, serialised once each.

    The exporters only append, so a session's tests, which read everything exported since the
    session began, serialise just what arrived since the last read; reading thousands of spans
    afresh inside a test's coroutine held its event loop for most of a second late in a run. A
    cleared exporter starts the cache again."""

    def __init__(self, serialise: Callable[[int, T], TelemetryItem]) -> None:
        self._serialise = serialise
        self._records: list[T] = []
        self._items: list[TelemetryItem] = []

    def items(self, records: Sequence[T]) -> list[TelemetryItem]:
        known = len(self._records)
        if len(records) < known or (known and records[known - 1] is not self._records[-1]):
            self._records, self._items, known = [], [], 0
        for index in range(known, len(records)):
            self._records.append(records[index])
            self._items.append(self._serialise(index, records[index]))
        return self._items


class TelemetryCapture:
    """The session's in-memory exporters and the audit over everything they received."""

    def __init__(self, exporters: TestExporters) -> None:
        self.exporters = exporters
        self._spans = _Serialised["ReadableSpan"](lambda _, span: span_item(span))
        self._logs = _Serialised["ReadableLogRecord"](log_item)

    @classmethod
    def install(cls, service: ServiceName = SESSION_SERVICE) -> TelemetryCapture:
        """Configure the process's telemetry with in-memory exporters (once per process: the
        OpenTelemetry providers can be set only once)."""
        exporters = TestExporters()
        with _session_environment():
            # Read inside the context, so the build commit comes from the process or the default.
            settings = load_settings(TelemetrySettings)
            configure_telemetry(service, settings, exporters=exporters)
        return cls(exporters)

    def spans(self) -> tuple[ReadableSpan, ...]:
        return self.exporters.spans.get_finished_spans()

    def log_records(self) -> tuple[ReadableLogRecord, ...]:
        return self.exporters.logs.get_finished_logs()

    def items(self) -> Iterator[TelemetryItem]:
        """Every exported item, serialised: spans, log records, then metric data points."""
        yield from list(self._spans.items(self.spans()))
        yield from list(self._logs.items(self.log_records()))
        yield from metric_items(self.exporters)

    def audit(self) -> list[Finding]:
        """Every leaking string of every exported item; empty when the session is clean."""
        return [finding for item in self.items() for finding in find_leaks(item)]


def session_capture(config: pytest.Config) -> TelemetryCapture:
    """The session's capture, installed on first use."""
    capture = config.stash.get(CAPTURE_KEY, None)
    if capture is None:
        capture = TelemetryCapture.install()
        config.stash[CAPTURE_KEY] = capture
    return capture


@pytest.fixture(scope="session")
def telemetry_capture(pytestconfig: pytest.Config) -> TelemetryCapture:
    """The session's in-memory exporters, for tests that assert on what was exported; the
    session-end audit covers everything they hold."""
    return session_capture(pytestconfig)
