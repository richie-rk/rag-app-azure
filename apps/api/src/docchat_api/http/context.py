"""What the layers of one request share through its ASGI scope, and its correlation ID
(`11-api.md` sections 1.3 and 1.6; `85-observability.md` section 2.2).

`correlationId` is the 32-character lower-case hexadecimal W3C trace ID of the request's server
span: the instrumentation continues a valid incoming `traceparent`, so the ID a user reports is the
trace the web client started. When no tracer provider records (a local run without a connection
string), `CorrelationMiddleware` gives a request without a valid trace context a non-recording span
with a fresh random trace ID, so every problem body and every audit row of the request still names
one trace. No response header carries the ID.

The route class places the matched route's options, its operationId and the server span in the
scope (`DocChatRoute`), where the request-size guard, the problem handlers and the audit helper
read them.
"""

from __future__ import annotations

import secrets
from collections.abc import MutableMapping
from typing import Any, Final

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, TraceFlags
from starlette.types import ASGIApp, Receive, Scope, Send

ROUTE_OPTIONS_KEY: Final = "docchat.route_options"
OPERATION_KEY: Final = "docchat.operation_id"
SERVER_SPAN_KEY: Final = "docchat.server_span"


def _random_id(bits: int) -> int:
    # An all-zero ID is invalid in W3C Trace Context.
    value = 0
    while value == 0:
        value = secrets.randbits(bits)
    return value


def correlation_id() -> str:
    """The trace ID of the current span; inside a request it is never the invalid all-zero ID,
    because the correlation middleware runs first."""
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


def operation_of(scope: MutableMapping[str, Any]) -> str | None:
    """The operationId of the route serving the request, once the route class has run."""
    value = scope.get(OPERATION_KEY)
    return value if isinstance(value, str) else None


def server_span_of(scope: MutableMapping[str, Any]) -> Span:
    """The request's server span: the one the route class saw, else the current span."""
    span = scope.get(SERVER_SPAN_KEY)
    return span if isinstance(span, Span) else trace.get_current_span()


class CorrelationMiddleware:
    """Gives a request without a valid trace context one of its own (section 1.6)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or trace.get_current_span().get_span_context().is_valid:
            await self.app(scope, receive, send)
            return
        span = NonRecordingSpan(
            SpanContext(
                trace_id=_random_id(128),
                span_id=_random_id(64),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.DEFAULT),
            )
        )
        token = otel_context.attach(trace.set_span_in_context(span))
        try:
            await self.app(scope, receive, send)
        finally:
            otel_context.detach(token)
