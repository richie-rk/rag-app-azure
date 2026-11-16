"""Unit tests of the data-access layer's instruments (`85-observability.md` section 4)."""

from __future__ import annotations

import gc
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from docchat_core.sql import telemetry as sql_telemetry
from docchat_core.sql.telemetry import INSTRUMENTS, SqlInstruments
from docchat_core.telemetry import instruments as registered


class Pool:
    def __init__(self, service: str, count: int) -> None:
        self._service = service
        self.count = count

    @property
    def service(self) -> str:
        return self._service

    def checked_out(self) -> int:
        return self.count


def metric_names(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    data = reader.get_metrics_data()
    names: dict[str, list[Any]] = {}
    if data is None:
        return names
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                names[metric.name] = list(metric.data.data_points)
    return names


def test_the_six_instruments() -> None:
    reader = InMemoryMetricReader()
    instruments = SqlInstruments(MeterProvider(metric_readers=[reader]).get_meter("test"))
    instruments.duration.record(1.5, {"operation": "read", "outcome": "ok"})
    instruments.retries.add(1, {"error_number": 1205})
    instruments.failures.add(1, {"error_number": 0, "reason": "token"})
    instruments.pool_wait.record(0.2, {"service": "api"})
    instruments.idle_releases.add(1, {"service": "api"})
    pool = Pool("ingestion", 3)
    instruments.watch_pool(pool)
    names = metric_names(reader)
    assert set(names) == {
        "docchat.sql.duration",
        "docchat.sql.retries",
        "docchat.sql.failures",
        "docchat.sql.pool.wait",
        "docchat.sql.pool.in_use",
        "docchat.sql.idle_releases",
    }
    (gauge,) = names["docchat.sql.pool.in_use"]
    assert gauge.value == 3
    assert dict(gauge.attributes) == {"service": "ingestion"}


def test_a_collected_engine_is_no_longer_observed() -> None:
    reader = InMemoryMetricReader()
    instruments = SqlInstruments(MeterProvider(metric_readers=[reader]).get_meter("test"))
    instruments.watch_pool(Pool("api", 1))
    gc.collect()
    assert metric_names(reader).get("docchat.sql.pool.in_use", []) == []


def test_process_instruments_exist() -> None:
    assert isinstance(INSTRUMENTS, SqlInstruments)


def test_the_process_records_through_the_registered_instruments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    calls: list[tuple[str, float, dict[str, object]]] = []

    class Fake:
        def __init__(self, name: str) -> None:
            self.name = name

        def record(self, value: float, /, **dimensions: object) -> None:
            calls.append((self.name, value, dimensions))

        def add(self, value: int, /, **dimensions: object) -> None:
            calls.append((self.name, value, dimensions))

    for attribute in ("SQL_DURATION", "SQL_RETRIES", "SQL_FAILURES", "SQL_POOL_WAIT"):
        monkeypatch.setattr(registered, attribute, Fake(attribute))
    monkeypatch.setattr(registered, "SQL_IDLE_RELEASES", Fake("SQL_IDLE_RELEASES"))
    process = sql_telemetry.RegisteredSqlInstruments()
    process.duration.record(12.5, {"operation": "ensure_user", "outcome": "ok"})
    process.retries.add(1, {"error_number": 1205})
    process.failures.add(1, {"error_number": 40613, "reason": "exhausted"})
    process.pool_wait.record(3.0, {"service": "api"})
    process.idle_releases.add(1, {"service": "api"})
    assert calls == [
        ("SQL_DURATION", 12.5, {"operation": "ensure_user", "outcome": "ok"}),
        ("SQL_RETRIES", 1, {"error_number": 1205}),
        ("SQL_FAILURES", 1, {"error_number": 40613, "reason": "exhausted"}),
        ("SQL_POOL_WAIT", 3.0, {"service": "api"}),
        ("SQL_IDLE_RELEASES", 1, {"service": "api"}),
    ]


def test_the_process_pool_gauge_forgets_a_collected_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    sources: list[tuple[object, dict[str, object]]] = []
    forgotten: list[bool] = []

    class FakeGauge:
        def observe(self, source: object, /, **dimensions: object) -> object:
            sources.append((source, dimensions))
            return lambda: forgotten.append(True)

    class Engine:
        service = "ingestion"

        def checked_out(self) -> int:
            return 3

    monkeypatch.setattr(registered, "SQL_POOL_IN_USE", FakeGauge())
    engine = Engine()
    sql_telemetry.RegisteredSqlInstruments().watch_pool(engine)
    ((read, dimensions),) = sources
    assert dimensions == {"service": "ingestion"}
    assert read() == 3  # type: ignore[operator]
    del engine
    gc.collect()
    assert read() is None  # type: ignore[operator]
    assert forgotten == [True]
