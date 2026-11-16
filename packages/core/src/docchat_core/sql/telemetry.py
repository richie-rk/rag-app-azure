"""The data-access layer's instruments: the `docchat.sql.*` metrics of `85-observability.md`
section 4.7.

Those metrics are created once, in `docchat_core.telemetry.instruments`, which checks each
measurement's dimensions against the attribute registry. The process records through them
(`INSTRUMENTS`); `SqlInstruments(meter)` builds the same six instruments on a meter a unit test
passes in, so the test can read the measurements without the process-wide provider.
"""

from __future__ import annotations

import weakref
from collections.abc import Iterable, Mapping
from typing import Final, Protocol, override

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from docchat_core.telemetry import instruments as registered


class PoolSource(Protocol):
    """What the in-use gauge reads from an engine."""

    @property
    def service(self) -> str: ...

    def checked_out(self) -> int: ...


class HistogramSink(Protocol):
    def record(self, amount: float, attributes: Mapping[str, str | int], /) -> None: ...


class CounterSink(Protocol):
    def add(self, amount: int, attributes: Mapping[str, str | int], /) -> None: ...


class _RegisteredHistogram:
    def __init__(self, histogram: registered.Histogram) -> None:
        self._histogram = histogram

    def record(self, amount: float, attributes: Mapping[str, str | int], /) -> None:
        self._histogram.record(amount, **attributes)


class _RegisteredCounter:
    def __init__(self, counter: registered.Counter) -> None:
        self._counter = counter

    def add(self, amount: int, attributes: Mapping[str, str | int], /) -> None:
        self._counter.add(amount, **attributes)


class SqlInstruments:
    """`docchat.sql.duration`, `.retries`, `.failures`, `.pool.wait`, `.pool.in_use` and
    `.idle_releases`, on the given meter."""

    duration: HistogramSink
    retries: CounterSink
    failures: CounterSink
    pool_wait: HistogramSink
    idle_releases: CounterSink

    def __init__(self, meter: Meter) -> None:
        self.duration = meter.create_histogram(
            "docchat.sql.duration", unit="ms", description="Duration of one SQL unit of work."
        )
        self.retries = meter.create_counter(
            "docchat.sql.retries", unit="1", description="Retried attempts of SQL units of work."
        )
        self.failures = meter.create_counter(
            "docchat.sql.failures", unit="1", description="SQL units of work that raised."
        )
        self.pool_wait = meter.create_histogram(
            "docchat.sql.pool.wait", unit="ms", description="Wait for a pooled SQL connection."
        )
        self.idle_releases = meter.create_counter(
            "docchat.sql.idle_releases",
            unit="1",
            description="Disposals of an idle SQL connection pool.",
        )
        self._in_use_readers: weakref.WeakSet[PoolSource] = weakref.WeakSet()
        meter.create_observable_gauge(
            "docchat.sql.pool.in_use",
            callbacks=[self._observe_in_use],
            unit="{connection}",
            description="SQL connections checked out of the pool.",
        )

    def watch_pool(self, source: PoolSource) -> None:
        """Report a live engine's checked-out connections until it is garbage-collected."""
        self._in_use_readers.add(source)

    def _observe_in_use(self, options: CallbackOptions) -> Iterable[Observation]:
        del options
        return [
            Observation(source.checked_out(), {"service": source.service})
            for source in list(self._in_use_readers)
        ]


class RegisteredSqlInstruments(SqlInstruments):
    """The process's instruments: each records through the registered metric of the same name."""

    def __init__(self) -> None:  # the same six instruments, recording through the registry
        self.duration = _RegisteredHistogram(registered.SQL_DURATION)
        self.retries = _RegisteredCounter(registered.SQL_RETRIES)
        self.failures = _RegisteredCounter(registered.SQL_FAILURES)
        self.pool_wait = _RegisteredHistogram(registered.SQL_POOL_WAIT)
        self.idle_releases = _RegisteredCounter(registered.SQL_IDLE_RELEASES)

    @override
    def watch_pool(self, source: PoolSource) -> None:
        reference = weakref.ref(source)

        def checked_out() -> int | None:
            live = reference()
            return None if live is None else live.checked_out()

        forget = registered.SQL_POOL_IN_USE.observe(checked_out, service=source.service)
        weakref.finalize(source, forget)


INSTRUMENTS: Final[SqlInstruments] = RegisteredSqlInstruments()
"""The process's instruments, on the global meter provider that `configure_telemetry` sets."""
