"""The ops-config signals (`85-observability.md` sections 4.3, 4.8, 4.12 and 4.13; `81-config.md`
section 3.1 items 3 and 5): the span `docchat.opsconfig.refresh`, the counters
`docchat.config.invalid` and `docchat.opsconfig.refresh_failures`, the custom event and error
record `ops_config_invalid`, and the warning record `ops_config_unreachable`.

Every signal names an area and a key, never a value. The instruments are created once, at import,
as an area's own instruments are. `OpsConfigStore` reports the API's invalid areas;
`load_dispatch_settings` reports ingestion's once per wake through `report_invalid`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Final, Literal

from opentelemetry.trace import Span

from docchat_core.telemetry import LOGGER_NAME, emit_event, log_event, set_attributes, start_span
from docchat_core.telemetry.instruments import counter

type RefreshOutcome = Literal["ok", "partial", "unreachable"]

REFRESH_SPAN: Final = "docchat.opsconfig.refresh"
INVALID_EVENT: Final = "ops_config_invalid"
UNREACHABLE_RECORD: Final = "ops_config_unreachable"

OUTCOME_ATTRIBUTE: Final = "docchat.opsconfig.outcome"
INVALID_AREAS_ATTRIBUTE: Final = "docchat.opsconfig.invalid_areas"
AREA_ATTRIBUTE: Final = "docchat.opsconfig.area"
KEY_ATTRIBUTE: Final = "docchat.opsconfig.key"

LOGGER: Final = logging.getLogger(LOGGER_NAME)

CONFIG_INVALID: Final = counter("docchat.config.invalid")
REFRESH_FAILURES: Final = counter("docchat.opsconfig.refresh_failures")


def refresh_span() -> AbstractContextManager[Span]:
    """One span per refresh, the start-up read included."""
    return start_span(REFRESH_SPAN)


def record_refresh(span: Span, outcome: RefreshOutcome, invalid_areas: int) -> None:
    set_attributes(span, **{OUTCOME_ATTRIBUTE: outcome, INVALID_AREAS_ATTRIBUTE: invalid_areas})


def log_invalid(area: str, keys: Iterable[str]) -> None:
    """The error record `ops_config_invalid` for each key; at start-up it is the only signal,
    because the process exits before it serves."""
    for key in keys:
        attributes = {AREA_ATTRIBUTE: area, KEY_ATTRIBUTE: key}
        log_event(LOGGER, INVALID_EVENT, logging.ERROR, **attributes)


def report_invalid(area: str, keys: Iterable[str]) -> None:
    """An area that failed validation while the process runs: for each key, one
    `docchat.config.invalid`, the custom event and the error record (alert A39)."""
    for key in keys:
        CONFIG_INVALID.add(1, area=area, key=key)
        emit_event(INVALID_EVENT, area=area, key=key)
        log_invalid(area, (key,))


def report_unreachable() -> None:
    """A refresh that couldn't read SQL: the warning record and one refresh failure."""
    REFRESH_FAILURES.add(1, reason="unreachable")
    log_event(LOGGER, UNREACHABLE_RECORD, logging.WARNING)
