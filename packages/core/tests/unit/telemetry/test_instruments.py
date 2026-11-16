"""The instruments: T-484 (declared dimensions) and the measurement checks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import opentelemetry.metrics._internal as metrics_internal
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util._once import Once

from docchat_core.telemetry import instruments
from docchat_core.telemetry.attributes import DIMENSIONS, METRICS
from docchat_core.telemetry.instruments import (
    INSTRUMENTS,
    MILLISECOND_HISTOGRAM_BOUNDARIES,
    MILLISECOND_HISTOGRAM_VIEW,
    Counter,
    Gauge,
    Histogram,
    ObservableGauge,
    counter,
    gauge,
    histogram,
    observable_gauge,
)

type MetricPoints = Callable[[str], list[Any]]

_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_IDENTIFIER_DIMENSIONS = {
    "user_id",
    "oid",
    "project_id",
    "session_id",
    "turn_id",
    "document_id",
    "version_id",
    "run_id",
    "item_id",
    "index_id",
    "physical_index_id",
    "chunk_set_id",
}


def test_t484_every_instrument_declares_closed_set_dimensions_and_no_identifier() -> None:
    assert INSTRUMENTS
    for name, instrument in INSTRUMENTS.items():
        assert instrument.dimensions <= DIMENSIONS, name
        assert not instrument.dimensions & _IDENTIFIER_DIMENSIONS, name
        assert instrument.dimensions == METRICS[name].dimensions, name
        assert instrument.unit == METRICS[name].unit, name
        assert instrument.kind == METRICS[name].kind, name


def test_the_shared_package_creates_the_section_4_7_instruments() -> None:
    created = {
        instruments.EVENT_LOOP_LAG.name,
        instruments.SQL_DURATION.name,
        instruments.SQL_RETRIES.name,
        instruments.SQL_FAILURES.name,
        instruments.SQL_POOL_WAIT.name,
        instruments.SQL_POOL_IN_USE.name,
        instruments.SQL_IDLE_RELEASES.name,
        instruments.SCRUB_ERRORS.name,
        instruments.DROPPED_ATTRIBUTES.name,
    }

    assert created == {
        "docchat.runtime.event_loop_lag",
        "docchat.sql.duration",
        "docchat.sql.retries",
        "docchat.sql.failures",
        "docchat.sql.pool.wait",
        "docchat.sql.pool.in_use",
        "docchat.sql.idle_releases",
        "docchat.telemetry.scrub_errors",
        "docchat.telemetry.dropped_attributes",
    }


def test_the_factories_create_each_instrument_once() -> None:
    assert counter("docchat.chat.heartbeats") is counter("docchat.chat.heartbeats")
    assert isinstance(histogram("docchat.search.duration"), Histogram)
    assert isinstance(gauge("docchat.ingestion.items.running"), Gauge)
    assert isinstance(observable_gauge("docchat.auth.signing_key_age"), ObservableGauge)


def test_the_factories_refuse_undeclared_names_and_kinds() -> None:
    with pytest.raises(ValueError, match="not a registered metric"):
        counter("docchat.chat.unknown")
    with pytest.raises(ValueError, match="declares a histogram"):
        counter("docchat.search.duration")


def test_a_counter_records_its_dimensions(metric_points: MetricPoints) -> None:
    counter("docchat.chat.turns").add(1, outcome="completed", error_code="none")
    counter("docchat.chat.turns").add(2, outcome="completed", error_code="none")

    (point,) = metric_points("docchat.chat.turns")
    assert point.value == 3
    assert dict(point.attributes) == {"outcome": "completed", "error_code": "none"}


def test_the_service_dimension_comes_from_the_state(
    metric_points: MetricPoints, api_service: None
) -> None:
    instruments.SQL_IDLE_RELEASES.add()

    (point,) = metric_points("docchat.sql.idle_releases")
    assert dict(point.attributes) == {"service": "api"}


def test_millisecond_histograms_use_the_one_view(metric_points: MetricPoints) -> None:
    instruments.SQL_DURATION.record(12.5, operation="ensure_user", outcome="ok")

    (point,) = metric_points("docchat.sql.duration")
    assert tuple(point.explicit_bounds) == MILLISECOND_HISTOGRAM_BOUNDARIES
    assert point.count == 1
    assert MILLISECOND_HISTOGRAM_VIEW._instrument_unit == "ms"


@pytest.mark.parametrize(
    "dimensions",
    [
        {"outcome": "completed", "project_id": "x"},
        {"outcome": 1.5},
        {"outcome": _UUID},
    ],
)
def test_a_bad_dimension_raises_in_test_mode(
    metric_points: MetricPoints, dimensions: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match=r"docchat\.chat\.turns"):
        counter("docchat.chat.turns").add(1, **dimensions)


def test_a_bad_dimension_is_dropped_and_counted_in_production(
    metric_points: MetricPoints, production: None
) -> None:
    counter("docchat.chat.turns").add(1, outcome="completed", project_id=_UUID, call=_UUID)

    (point,) = metric_points("docchat.chat.turns")
    assert dict(point.attributes) == {"outcome": "completed"}
    (dropped,) = metric_points("docchat.telemetry.dropped_attributes")
    assert dropped.value == 2


def test_a_gauge_keeps_the_last_value(metric_points: MetricPoints) -> None:
    gauge("docchat.ingestion.items.queued").set(4, priority="bulk")
    gauge("docchat.ingestion.items.queued").set(7, priority="bulk")

    (point,) = metric_points("docchat.ingestion.items.queued")
    assert point.value == 7


def test_an_observable_gauge_reads_its_sources(
    metric_points: MetricPoints, api_service: None
) -> None:
    gauge_ = ObservableGauge("docchat.sql.pool.in_use", METRICS["docchat.sql.pool.in_use"])
    in_use = [3]
    forget = gauge_.observe(lambda: in_use[0])
    gauge_.observe(lambda: None)

    (point,) = metric_points("docchat.sql.pool.in_use")
    assert point.value == 3
    assert dict(point.attributes) == {"service": "api"}

    forget()
    assert metric_points("docchat.sql.pool.in_use") == []


def test_an_instrument_follows_a_new_meter_provider(
    monkeypatch: pytest.MonkeyPatch, metric_points: MetricPoints
) -> None:
    turns = Counter("docchat.chat.turns", METRICS["docchat.chat.turns"])
    turns.add(1, outcome="failed", error_code="internal_error")
    assert len(metric_points("docchat.chat.turns")) == 1

    second = InMemoryMetricReader()
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", None)
    metrics_internal.set_meter_provider(MeterProvider(metric_readers=[second]))
    turns.add(1, outcome="failed", error_code="internal_error")

    data = second.get_metrics_data()
    assert data is not None
    names = [
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    assert names == ["docchat.chat.turns"]


def test_t484_an_instrument_refuses_dimensions_outside_the_closed_set() -> None:
    spec = METRICS["docchat.chat.turns"]
    with pytest.raises(ValueError, match="closed set"):
        Counter(
            "docchat.chat.turns",
            type(spec)(kind=spec.kind, unit=spec.unit, dimensions=frozenset({"project_id"})),
        )
