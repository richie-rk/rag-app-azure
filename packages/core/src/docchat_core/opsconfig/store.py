"""`OpsConfigStore`: the per-process loader and cache of `app.ops_config` (`81-config.md` section
3.1 items 3 to 7; ADR 0028 items 5 and 6).

Every row is read with one statement, `SELECT config_key, value_json FROM app.ops_config`, through
the data-access layer. Rows of areas the process didn't register, and keys an area's model doesn't
declare, are dropped, because an expand migration ships a key before the code that reads it and a
rolled-back release runs beside rows it doesn't know.

- **Start-up** (`start`): a missing key or an invalid value in a registered area raises
  `OpsConfigInvalidError` after one `ops_config_invalid` record per area and key, so the process
  exits before it serves; an unreachable database raises the data-access layer's `SqlError`.
- **Snapshots** (`snapshot`): the immutable `OpsConfig` while it is younger than 60 seconds; the
  first access after that refreshes it under a lock, and concurrent callers wait for that one
  refresh. Nothing refreshes on a timer, so the API adds no periodic query and a paused `dev`
  database stays paused.
- **Refresh failures**: an area that fails validation keeps its last valid sub-model and is
  reported (`docchat.config.invalid`, the `ops_config_invalid` event and record); a refresh that
  can't reach SQL keeps the snapshot, logs `ops_config_unreachable` and allows the next attempt no
  sooner than 15 seconds later.

The ingestion app reports its own invalid area once per dispatcher wake in
`load_dispatch_settings` (`14-ingestion.md` section 5.2), so its store is built with
`report_invalid=False` and reads `invalid_keys` instead; and because a dispatcher with no valid
snapshot claims nothing instead of exiting, its first `snapshot` holds only the areas that
validate.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol

from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.sql.base import Executable

from docchat_core.models.clock import Clock, SystemClock
from docchat_core.opsconfig.areas import efforts_context
from docchat_core.opsconfig.root import (
    AREA_MODELS,
    EFFORT_AREAS,
    Area,
    OpsConfig,
    failing_keys,
    validate_area,
)
from docchat_core.opsconfig.telemetry import (
    log_invalid,
    record_refresh,
    refresh_span,
    report_invalid,
    report_unreachable,
)
from docchat_core.opsconfig.values import StoredValueError, area_of, decode_value
from docchat_core.settings import StartupCheckError
from docchat_core.sql.engine import Retry, SqlError
from docchat_core.sql.tables.access import ops_config

SNAPSHOT_MAX_AGE_SECONDS: Final = 60.0
UNREACHABLE_RETRY_SECONDS: Final = 15.0

READ_STATEMENT_NAME: Final = "read_ops_config"
"""The statement-set name of the full read, the one statement the query-plan audit exempts."""

READ_ALL: Final = select(ops_config.c.config_key, ops_config.c.value_json)

type Failures = Mapping[Area, tuple[str, ...]]


class RowReader(Protocol):
    """What the store needs of `docchat_core.sql.engine.SqlEngine`."""

    async def fetch_all(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: Retry = ...,
    ) -> Sequence[Sequence[Any]]: ...


class OpsConfigInvalidError(StartupCheckError):
    """Start-up check 11 of `81-config.md` section 6.1: a registered area has a missing key or an
    invalid value. A settings-validation failure, whose record names the failing keys."""

    def __init__(self, failures: Failures) -> None:
        keys = [key for area_keys in failures.values() for key in area_keys]
        super().__init__(None, "Ops config validation failed.", keys)
        self.failures: Failures = MappingProxyType(dict(failures))


@dataclass(frozen=True)
class _Read:
    """One read of the table: the areas that validated and the keys of those that didn't, or
    the data-access layer's failure."""

    models: Mapping[Area, BaseModel] = field(default_factory=dict)
    failures: Failures = field(default_factory=dict)
    failure: SqlError | None = None


class OpsConfigStore:
    """One per process: created with the process's data-access layer and the areas it reads."""

    def __init__(
        self,
        reader: RowReader,
        areas: Iterable[Area],
        *,
        chat_small_efforts: Collection[str] | None = None,
        report_invalid: bool = True,
        clock: Clock | None = None,
    ) -> None:
        """`chat_small_efforts` is the chat-small model's row of the capability table, required
        when `summary` or `memory` is registered. `report_invalid=False` leaves the reporting of
        an invalid area to the caller (ingestion's `load_dispatch_settings`)."""
        registered = tuple(dict.fromkeys(areas))
        if not registered:
            raise ValueError("a store registers at least one area")
        if any(area not in AREA_MODELS for area in registered):
            raise ValueError("an unknown ops-config area")
        if chat_small_efforts is None and EFFORT_AREAS.intersection(registered):
            raise ValueError("the summary and memory areas need the chat-small model's efforts")
        self._reader = reader
        self._registered: tuple[Area, ...] = registered
        self._efforts = None if chat_small_efforts is None else frozenset(chat_small_efforts)
        self._report_invalid = report_invalid
        self._clock: Clock = SystemClock() if clock is None else clock
        self._lock = asyncio.Lock()
        self._areas: dict[Area, BaseModel] = {}
        self._invalid: Failures = MappingProxyType({})
        self._snapshot: OpsConfig | None = None
        self._loaded_at = -math.inf
        self._next_attempt_at = -math.inf

    @property
    def areas(self) -> tuple[Area, ...]:
        """The registered areas."""
        return self._registered

    def invalid_keys(self, area: Area) -> tuple[str, ...]:
        """The keys of `area` that failed at the latest read; empty when the area validated."""
        return self._invalid.get(area, ())

    async def start(self) -> OpsConfig:
        """The start-up read: every registered area must validate."""
        async with self._lock:
            started = self._clock.monotonic()
            read = await self._attempt()
            if read.failure is not None:
                raise read.failure
            if read.failures:
                for area, keys in read.failures.items():
                    log_invalid(area, keys)
                raise OpsConfigInvalidError(read.failures)
            return self._install(read, started)

    async def snapshot(self) -> OpsConfig:
        """The current snapshot, refreshed first when it is 60 seconds old or older.

        Before any read has succeeded there is no snapshot to keep, so an unreachable database
        raises the data-access layer's `SqlError` instead of waiting for a later attempt.
        """
        current = self._snapshot
        if current is not None and not self._refresh_due():
            return current
        async with self._lock:
            # The callers that waited for the lock find the refresh done.
            current = self._snapshot
            if current is None or self._refresh_due():
                current = await self._refresh()
            return current

    def _refresh_due(self) -> bool:
        now = self._clock.monotonic()
        return now - self._loaded_at >= SNAPSHOT_MAX_AGE_SECONDS and now >= self._next_attempt_at

    async def _refresh(self) -> OpsConfig:
        started = self._clock.monotonic()
        read = await self._attempt()
        if read.failure is not None:
            self._next_attempt_at = self._clock.monotonic() + UNREACHABLE_RETRY_SECONDS
            report_unreachable()
            if self._snapshot is None:
                raise read.failure
            return self._snapshot
        snapshot = self._install(read, started)
        if self._report_invalid:
            for area, keys in read.failures.items():
                report_invalid(area, keys)
        return snapshot

    async def _attempt(self) -> _Read:
        with refresh_span() as span:
            try:
                rows = await self._reader.fetch_all(READ_ALL, name=READ_STATEMENT_NAME)
            except SqlError as error:
                # Handled inside the span, so the span records no exception text.
                record_refresh(span, "unreachable", 0)
                return _Read(failure=error)
            read = self._validate(rows)
            record_refresh(span, "partial" if read.failures else "ok", len(read.failures))
            return read

    def _validate(self, rows: Sequence[Sequence[Any]]) -> _Read:
        values: dict[str, dict[str, JsonValue]] = {area: {} for area in self._registered}
        for config_key, value_json in rows:
            area_values = values.get(area_of(config_key))
            if area_values is None:
                continue
            try:
                area_values[config_key] = decode_value(value_json)
            except StoredValueError:
                # Left out, so the area's validation names the key as missing.
                continue
        models: dict[Area, BaseModel] = {}
        failures: dict[Area, tuple[str, ...]] = {}
        for area in self._registered:
            try:
                models[area] = validate_area(area, values[area], chat_small_efforts=self._efforts)
            except ValidationError as error:
                failures[area] = failing_keys(area, error)
        return _Read(models=models, failures=failures)

    def _install(self, read: _Read, started: float) -> OpsConfig:
        # An area that failed keeps its last valid sub-model, if it ever had one.
        self._areas.update(read.models)
        self._invalid = MappingProxyType(dict(read.failures))
        # Pydantic runs a sub-model's after-validators again when the root takes the instance,
        # so the root needs the same context as the areas.
        context = None if self._efforts is None else efforts_context(self._efforts)
        snapshot = OpsConfig.model_validate(dict(self._areas), context=context)
        self._snapshot = snapshot
        # Measured from the start of the read, so a value committed before it is served for at
        # most 60 seconds after the read began.
        self._loaded_at = started
        return snapshot
