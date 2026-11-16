"""The authentication signals (`85-observability.md` sections 4.3, 4.8, 4.12 and 4.13; ADR 0017
item 10): the spans `docchat.auth.validate_token` and `docchat.auth.fetch_signing_keys`, the
counters `docchat.auth.validations` and `docchat.auth.signing_key_fetches`, the gauge
`docchat.auth.signing_key_age`, the custom event `signing_keys_refresh_failed` and the warning
record `signing_keys_fetch_failed`.

A refused token is described by its reason, its outcome and the client (`web`, `test`, or
`unknown` before the client check passed), never by its claims, header or text. The instruments
are created once, at import, as an area's own instruments are.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Final, Literal

from opentelemetry.trace import Span

from docchat_core.auth.exceptions import AuthOutcome, AuthReason
from docchat_core.auth.settings import Client
from docchat_core.telemetry import LOGGER_NAME, emit_event, log_event, set_attributes, start_span
from docchat_core.telemetry.instruments import GaugeSource, counter, observable_gauge

type ClientLabel = Client | Literal["unknown"]
type FetchTrigger = Literal["startup", "scheduled", "unknown_kid"]
type FetchOutcome = Literal["ok", "error"]

VALIDATE_SPAN: Final = "docchat.auth.validate_token"
FETCH_SPAN: Final = "docchat.auth.fetch_signing_keys"
FETCH_FAILED_RECORD: Final = "signing_keys_fetch_failed"
REFRESH_FAILED_EVENT: Final = "signing_keys_refresh_failed"

OUTCOME_ATTRIBUTE: Final = "docchat.auth.outcome"
REASON_ATTRIBUTE: Final = "docchat.auth.reason"
CLIENT_ATTRIBUTE: Final = "docchat.auth.client"
TRIGGER_ATTRIBUTE: Final = "docchat.auth.trigger"
KEY_COUNT_ATTRIBUTE: Final = "docchat.auth.key_count"
STATUS_CODE_ATTRIBUTE: Final = "http.response.status_code"
ERROR_TYPE_ATTRIBUTE: Final = "error.type"

NO_REASON: Final = "none"
"""The `reason` dimension of a successful validation."""

NO_STATUS: Final = 0
"""The event's `statusCode` when no response arrived."""

LOGGER: Final = logging.getLogger(LOGGER_NAME)

VALIDATIONS: Final = counter("docchat.auth.validations")
KEY_FETCHES: Final = counter("docchat.auth.signing_key_fetches")
KEY_AGE: Final = observable_gauge("docchat.auth.signing_key_age")


def validate_span() -> AbstractContextManager[Span]:
    """One span per validation, every request except `GET /healthz`."""
    return start_span(VALIDATE_SPAN)


def record_validation(
    span: Span, outcome: AuthOutcome, reason: AuthReason | None, client: ClientLabel
) -> None:
    """The validation's span attributes and its count; `reason` is `None` on success."""
    attributes: dict[str, object] = {OUTCOME_ATTRIBUTE: outcome, CLIENT_ATTRIBUTE: client}
    if reason is not None:
        attributes[REASON_ATTRIBUTE] = reason
    set_attributes(span, **attributes)
    VALIDATIONS.add(1, outcome=outcome, reason=reason or NO_REASON, client=client)


def fetch_span(trigger: FetchTrigger) -> AbstractContextManager[Span]:
    """One span per key-set fetch, with the trigger that started it."""
    return start_span(FETCH_SPAN, **{TRIGGER_ATTRIBUTE: trigger})


def record_fetch(
    span: Span,
    trigger: FetchTrigger,
    outcome: FetchOutcome,
    *,
    key_count: int | None,
    status_code: int | None,
) -> None:
    """The fetch's span attributes and its count; the key count only for a usable set and the
    status only when a response arrived."""
    attributes: dict[str, object] = {OUTCOME_ATTRIBUTE: outcome}
    if key_count is not None:
        attributes[KEY_COUNT_ATTRIBUTE] = key_count
    if status_code is not None:
        attributes[STATUS_CODE_ATTRIBUTE] = status_code
    set_attributes(span, **attributes)
    KEY_FETCHES.add(1, trigger=trigger, outcome=outcome)


def report_fetch_failure(trigger: FetchTrigger, status_code: int | None, error_type: str) -> None:
    """A failed fetch that keeps the last key set: the warning record and the custom event
    behind alert A35, each with the status code and the error's qualified type name."""
    record: dict[str, object] = {ERROR_TYPE_ATTRIBUTE: error_type, TRIGGER_ATTRIBUTE: trigger}
    if status_code is not None:
        record[STATUS_CODE_ATTRIBUTE] = status_code
    log_event(LOGGER, FETCH_FAILED_RECORD, logging.WARNING, **record)
    emit_event(
        REFRESH_FAILED_EVENT,
        trigger=trigger,
        statusCode=NO_STATUS if status_code is None else status_code,
        errorType=error_type,
    )


def observe_key_age(source: GaugeSource) -> Callable[[], None]:
    """Report the worker's key-set age through `source` (seconds, or `None` before the first
    usable set); the returned function stops it."""
    return KEY_AGE.observe(source)
