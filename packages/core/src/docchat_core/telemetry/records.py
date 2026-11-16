"""The only ways application code emits telemetry (`85-observability.md` section 3.2; ADR 0014
items 2 to 4): `log_event` for log records, `emit_event` for custom events, `start_span` and
`set_attributes` for first-party spans, and `record_failure` for a failure on a span.

Each checks what it is given against the attribute registry. In test mode a breach raises
`ValueError`, so a test fails at the call site; in production the offending attribute (or an
undeclared event, whole) is dropped, the rest is kept, and `docchat.telemetry.dropped_attributes`
counts each drop. Nothing here raises into application code in production. A breach is described
by its names only, never by the value, which may be the very content the check keeps out.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Final

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from docchat_core.telemetry.attributes import (
    CUSTOM_EVENT_NAME_ATTRIBUTE,
    EVENTS,
    LOG_EVENTS,
    is_camel_case,
    is_event_name,
    span_spec,
    value_spec,
)
from docchat_core.telemetry.instruments import DROPPED_ATTRIBUTES, SCRUB_ERRORS
from docchat_core.telemetry.scrubbing import (
    ERROR_CODE_KEY,
    EXCEPTION_EVENT,
    EXCEPTION_MESSAGE_KEY,
    EXCEPTION_STACKTRACE_KEY,
    EXCEPTION_TYPE_KEY,
    REDACTED_ERROR,
    catalogue_message,
    qualified_type_name,
    safe_traceback,
)
from docchat_core.telemetry.state import is_strict

LOGGER_NAME: Final = "docchat"
"""The one first-party logger."""

TRACER_NAME: Final = "docchat"

_tracer_lock: Final = threading.Lock()
_tracer_cache: list[tuple[trace.TracerProvider, trace.Tracer]] = []


def _breach(problem: str) -> None:
    if is_strict():
        raise ValueError(problem)
    DROPPED_ATTRIBUTES.add(1)


def _name(name: str) -> str:
    # A name that isn't a static snake_case word may be content; it never enters a message.
    return repr(name) if is_event_name(name.replace(".", "_")) else "a non-static name"


def _checked(
    attributes: Mapping[str, object],
    *,
    where: str,
    allowed: frozenset[str] | None,
    narrowed: Mapping[str, frozenset[str]] | None,
) -> dict[str, AttributeValue]:
    checked: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        spec = value_spec(key, narrowed)
        if spec is None:
            problem = f"{where}: unregistered attribute key {_name(key)}"
        elif allowed is not None and key not in allowed:
            problem = f"{where}: {key!r} is not one of its attributes"
        elif not spec.accepts(value):
            problem = f"{where}: the value of {key!r} is not a valid {spec.kind}"
        else:
            # `accepts` admits only strings, booleans, integers and floats.
            checked[key] = value  # type: ignore[assignment]
            continue
        _breach(problem)
    return checked


def log_event(logger: logging.Logger, event: str, level: int, /, **attributes: object) -> None:
    """Emit the log record `event` (a name of `LOG_EVENTS`, the record's whole message) at its
    registered level, carrying only the record's registered keys."""
    spec = LOG_EVENTS.get(event)
    if spec is None:
        _breach(f"unregistered log record {_name(event)}")
        return
    if logger.name != LOGGER_NAME:
        _breach(f"{event}: records go to the {LOGGER_NAME!r} logger only")
        logger = logging.getLogger(LOGGER_NAME)
    if level != spec.level:
        _breach(f"{event}: registered at level {logging.getLevelName(spec.level)}")
        level = spec.level
    extra = _checked(attributes, where=event, allowed=spec.keys, narrowed=spec.values)
    logger.log(level, event, extra=extra, stacklevel=2)


def emit_event(name: str, /, **properties: object) -> None:
    """Emit the custom event `name` (stored in `AppEvents`) with its declared lowerCamelCase
    properties.

    The record goes to the `docchat` logger's handlers whatever the logger's level, because an
    event is a signal that alerts and the deploy check wait for, not a log line the level trims;
    the stderr handler's own `WARNING` level keeps events off standard error.
    """
    spec = EVENTS.get(name)
    if spec is None:
        _breach(f"undeclared custom event {_name(name)}")
        return
    checked: dict[str, AttributeValue] = {}
    for prop, value in properties.items():
        declared = spec.properties.get(prop)
        if not is_camel_case(prop):
            problem = f"{name}: property {_name(prop)} is not lowerCamelCase"
        elif declared is None:
            problem = f"{name}: undeclared property {prop!r}"
        elif not declared.accepts(value):
            problem = f"{name}: the value of {prop!r} is not a valid {declared.kind}"
        else:
            checked[prop] = value  # type: ignore[assignment]
            continue
        _breach(problem)
    logger = logging.getLogger(LOGGER_NAME)
    path, line, function, _ = logger.findCaller(stacklevel=2)
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        path,
        line,
        name,
        (),
        None,
        func=function,
        extra={CUSTOM_EVENT_NAME_ATTRIBUTE: name, **checked},
    )
    logger.handle(record)


def _tracer() -> trace.Tracer:
    provider = trace.get_tracer_provider()
    cached = _tracer_cache[0] if _tracer_cache else None
    if cached is not None and cached[0] is provider:
        return cached[1]
    tracer = provider.get_tracer(TRACER_NAME)
    with _tracer_lock:
        _tracer_cache[:] = [(provider, tracer)]
    return tracer


def start_span(
    name: str,
    /,
    kind: object = SpanKind.INTERNAL,
    links: object = (),
    **attributes: object,
) -> AbstractContextManager[Span]:
    """Open the first-party span `name` as the current span, with registered attributes only.

    `kind` is a `SpanKind` and `links` a sequence of `Link`. Both are typed `object` so that a
    call unpacking a dictionary of dotted attribute keys type-checks (a type checker matches every
    unpacked value against every keyword parameter); they are checked when called instead.

    A span the registry marks as a root (the dispatcher's sweep and claim) starts a trace of its
    own, whatever context is current; it reaches the runs it serves through `links`.
    """
    if not isinstance(kind, SpanKind):
        raise TypeError("kind must be a SpanKind")
    if not isinstance(links, Sequence) or not all(isinstance(link, Link) for link in links):
        raise TypeError("links must be a sequence of Link")
    spec = span_spec(name)
    if spec is None:
        _breach(f"unregistered span {_name(name)}")
    elif kind != spec.kind:
        _breach(f"{name}: registered as {spec.kind.name}")
        kind = spec.kind
    checked = _checked(
        attributes,
        where=name,
        allowed=None if spec is None else spec.attributes,
        narrowed=None if spec is None else spec.values,
    )
    return _tracer().start_as_current_span(
        name,
        context=Context() if spec is not None and spec.root else None,
        kind=kind,
        attributes=checked,
        links=links,
    )


def set_attributes(span: Span, /, **attributes: object) -> None:
    """Set registered attributes on `span` after it started, checked as `start_span` checks
    them; on the instrumentation's server span any registered key is allowed."""
    name = getattr(span, "name", None)
    spec = span_spec(name) if isinstance(name, str) else None
    where = name if spec is not None and isinstance(name, str) else "span"
    checked = _checked(
        attributes,
        where=where,
        allowed=None if spec is None else spec.attributes,
        narrowed=None if spec is None else spec.values,
    )
    if checked:
        span.set_attributes(checked)


def record_failure(span: Span, exc: BaseException, code: str) -> None:
    """Record a failure on `span`: one `exception` event with the qualified type name, the code's
    catalogue message (never the exception's text), the code and the frames-only stack trace,
    and an error status described by the code."""
    exc_type = qualified_type_name(type(exc))
    try:
        stacktrace = safe_traceback(exc)
    except Exception:  # noqa: BLE001 - a failure is still recorded, redacted
        stacktrace = REDACTED_ERROR
        SCRUB_ERRORS.add(1)
    attributes: dict[str, AttributeValue] = {
        EXCEPTION_TYPE_KEY: exc_type,
        EXCEPTION_STACKTRACE_KEY: stacktrace,
    }
    message = catalogue_message(code)
    if message is None:
        _breach(f"unknown error code {_name(code)}")
        attributes[EXCEPTION_MESSAGE_KEY] = exc_type
        description = exc_type
    else:
        attributes[EXCEPTION_MESSAGE_KEY] = message
        attributes[ERROR_CODE_KEY] = code
        description = code
    span.add_event(EXCEPTION_EVENT, attributes)
    span.set_status(Status(StatusCode.ERROR, description))
