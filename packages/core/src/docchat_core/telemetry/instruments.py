"""Instruments and the shared package's own metrics (`85-observability.md` sections 3.3, 3.5 and
4.7).

Every instrument is created once, at import, from its `METRICS` declaration, here for the
runtime, SQL and telemetry metrics of section 4.7 and in each area's own `telemetry.py` for the
rest (`01-repository.md` section 2.2). The factories refuse a name the registry doesn't declare or
a kind it doesn't give, and each measurement's dimensions are checked against the instrument's
declared set: a key outside it, a value of another type or an identifier-shaped value raises in
test mode and is dropped and counted in production, so the number of series stays bounded.

An instrument binds to the meter provider that is current when it records, so the instruments
created at import serve the provider `configure_telemetry` installs later.
"""

from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from typing import Final, override

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation, _Gauge
from opentelemetry.sdk.metrics import Histogram as SdkHistogram
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

from docchat_core.models import UUID_PATTERN
from docchat_core.telemetry.attributes import DIMENSIONS, METRICS, InstrumentKind, MetricSpec
from docchat_core.telemetry.state import is_strict, service_dimension

METER_NAME: Final = "docchat"

MILLISECOND_HISTOGRAM_BOUNDARIES: Final = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    200.0,
    400.0,
    800.0,
    1600.0,
    3200.0,
    6400.0,
    12800.0,
    25600.0,
    51200.0,
    102400.0,
)

MILLISECOND_HISTOGRAM_VIEW: Final = View(
    instrument_type=SdkHistogram,
    instrument_unit="ms",
    aggregation=ExplicitBucketHistogramAggregation(boundaries=MILLISECOND_HISTOGRAM_BOUNDARIES),
)
"""The one view every millisecond histogram uses, passed to the distro (section 3.3)."""

type DimensionValue = str | bool | int

_UUID_RE: Final = re.compile(UUID_PATTERN)
_DROPPED_ATTRIBUTES_NAME: Final = "docchat.telemetry.dropped_attributes"


class Instrument[B](ABC):
    """One declared instrument, bound to the current meter provider when it records."""

    def __init__(self, name: str, spec: MetricSpec) -> None:
        if not spec.dimensions <= DIMENSIONS:
            raise ValueError(f"{name}: a dimension outside the closed set")
        self.name = name
        self.kind = spec.kind
        self.unit = spec.unit
        self.dimensions = spec.dimensions
        self._lock = threading.Lock()
        self._provider: metrics.MeterProvider | None = None
        self._bound: B | None = None

    def _instrument(self) -> B:
        provider = metrics.get_meter_provider()
        bound = self._bound
        if bound is None or provider is not self._provider:
            with self._lock:
                bound = self._create(provider.get_meter(METER_NAME))
                self._bound, self._provider = bound, provider
        return bound

    @abstractmethod
    def _create(self, meter: metrics.Meter) -> B: ...

    def attributes(self, dimensions: Mapping[str, object]) -> dict[str, DimensionValue]:
        """The measurement's attributes: the given dimensions, checked, with `service` filled in
        when the instrument declares it."""
        checked: dict[str, DimensionValue] = {}
        dropped = 0
        for key, value in dimensions.items():
            if key not in self.dimensions:
                problem = f"{self.name}: undeclared dimension {key!r}"
            elif not isinstance(value, str | bool | int):
                problem = f"{self.name}: dimension {key!r} must be a string, boolean or integer"
            elif isinstance(value, str) and _UUID_RE.fullmatch(value):
                problem = f"{self.name}: dimension {key!r} holds an identifier"
            else:
                checked[key] = value
                continue
            if is_strict():
                raise ValueError(problem)
            dropped += 1
        if "service" in self.dimensions and "service" not in checked:
            service = service_dimension()
            if service is not None:
                checked["service"] = service
        if dropped and self.name != _DROPPED_ATTRIBUTES_NAME:
            DROPPED_ATTRIBUTES.add(dropped)
        return checked


class Counter(Instrument[metrics.Counter]):
    @override
    def _create(self, meter: metrics.Meter) -> metrics.Counter:
        return meter.create_counter(self.name, unit=self.unit)

    def add(self, amount: int | float = 1, /, **dimensions: object) -> None:
        self._instrument().add(amount, self.attributes(dimensions))


class Histogram(Instrument[metrics.Histogram]):
    @override
    def _create(self, meter: metrics.Meter) -> metrics.Histogram:
        return meter.create_histogram(self.name, unit=self.unit)

    def record(self, value: int | float, /, **dimensions: object) -> None:
        self._instrument().record(value, self.attributes(dimensions))


class Gauge(Instrument[_Gauge]):
    """A synchronous gauge: the value is set when it is measured (the keep-alive's gauges)."""

    @override
    def _create(self, meter: metrics.Meter) -> _Gauge:
        return meter.create_gauge(self.name, unit=self.unit)

    def set(self, value: int | float, /, **dimensions: object) -> None:
        self._instrument().set(value, self.attributes(dimensions))


type GaugeSource = Callable[[], int | float | None]


class ObservableGauge(Instrument[metrics.ObservableGauge]):
    """An observable gauge, read at each collection from the sources observing it; a source
    returning `None` has no value this time."""

    def __init__(self, name: str, spec: MetricSpec) -> None:
        super().__init__(name, spec)
        self._sources: list[tuple[GaugeSource, dict[str, object]]] = []
        self._sources_lock = threading.Lock()

    @override
    def _create(self, meter: metrics.Meter) -> metrics.ObservableGauge:
        return meter.create_observable_gauge(self.name, callbacks=[self._observe], unit=self.unit)

    def observe(self, source: GaugeSource, /, **dimensions: object) -> Callable[[], None]:
        """Adds a source; the returned function removes it."""
        entry = (source, dimensions)
        with self._sources_lock:
            self._sources.append(entry)
        self._instrument()

        def forget() -> None:
            with self._sources_lock:
                if entry in self._sources:
                    self._sources.remove(entry)

        return forget

    def _observe(self, options: CallbackOptions) -> Iterable[Observation]:
        with self._sources_lock:
            sources = list(self._sources)
        observations: list[Observation] = []
        for source, dimensions in sources:
            value = source()
            if value is not None:
                observations.append(Observation(value, self.attributes(dimensions)))
        return observations


type AnyInstrument = Counter | Histogram | Gauge | ObservableGauge

INSTRUMENTS: Final[dict[str, AnyInstrument]] = {}
"""Every instrument created in this process, by name; the dimension test reads it."""

_CLASSES: Final[Mapping[InstrumentKind, type[AnyInstrument]]] = {
    InstrumentKind.COUNTER: Counter,
    InstrumentKind.HISTOGRAM: Histogram,
    InstrumentKind.GAUGE: Gauge,
    InstrumentKind.OBSERVABLE_GAUGE: ObservableGauge,
}
_REGISTRY_LOCK: Final = threading.Lock()


def _create[I: AnyInstrument](name: str, cls: type[I]) -> I:
    spec = METRICS.get(name)
    if spec is None:
        raise ValueError(f"{name}: not a registered metric")
    if _CLASSES[spec.kind] is not cls:
        raise ValueError(f"{name}: the registry declares a {spec.kind}")
    with _REGISTRY_LOCK:
        existing = INSTRUMENTS.get(name)
        if isinstance(existing, cls):
            return existing
        created = cls(name, spec)
        INSTRUMENTS[name] = created
        return created


def counter(name: str) -> Counter:
    return _create(name, Counter)


def histogram(name: str) -> Histogram:
    return _create(name, Histogram)


def gauge(name: str) -> Gauge:
    return _create(name, Gauge)


def observable_gauge(name: str) -> ObservableGauge:
    return _create(name, ObservableGauge)


# Section 4.7: emitted by the shared package in both apps.
EVENT_LOOP_LAG: Final = histogram("docchat.runtime.event_loop_lag")
SQL_DURATION: Final = histogram("docchat.sql.duration")
SQL_RETRIES: Final = counter("docchat.sql.retries")
SQL_FAILURES: Final = counter("docchat.sql.failures")
SQL_POOL_WAIT: Final = histogram("docchat.sql.pool.wait")
SQL_POOL_IN_USE: Final = observable_gauge("docchat.sql.pool.in_use")
SQL_IDLE_RELEASES: Final = counter("docchat.sql.idle_releases")
SCRUB_ERRORS: Final = counter("docchat.telemetry.scrub_errors")
DROPPED_ATTRIBUTES: Final = counter(_DROPPED_ATTRIBUTES_NAME)
