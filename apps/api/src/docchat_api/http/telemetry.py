"""The API layer's own signals (`85-observability.md` sections 4.2, 4.3, 4.8 and 4.13; `11-api.md`
sections 1.3, 1.6 and 1.16).

The first-party attributes of the instrumentation's server span (`enduser.id`,
`docchat.auth.client`, `docchat.audit.action`, `docchat.error_code`), the counters
`docchat.api.problems` and `docchat.ratelimit.rejections`, the span `docchat.ratelimit.write_check`,
the record `request_failed` and the custom event `admin_change`. The route class keeps the server
span on the request, because a first-party span may be current when an attribute is set. Nothing
here carries a token, a claim other than `oid`, a name, an address or a value from a request body.
The instruments are created once, at import.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from typing import Final, Literal
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Span

from docchat_core.auth import Principal
from docchat_core.telemetry import (
    LOGGER_NAME,
    emit_event,
    log_event,
    qualified_type_name,
    record_failure,
    set_attributes,
    start_span,
)
from docchat_core.telemetry.instruments import counter

type RejectionReason = Literal["api", "write"]
"""The `reason` values of `docchat.ratelimit.rejections` this layer records; the chat pipeline
records its own reasons on the same counter."""

type WriteCheckOutcome = Literal["allowed", "limited", "unavailable"]

ENDUSER_ATTRIBUTE: Final = "enduser.id"
CLIENT_ATTRIBUTE: Final = "docchat.auth.client"
AUDIT_ACTION_ATTRIBUTE: Final = "docchat.audit.action"
ERROR_CODE_ATTRIBUTE: Final = "docchat.error_code"
ERROR_TYPE_ATTRIBUTE: Final = "error.type"

WRITE_CHECK_SPAN: Final = "docchat.ratelimit.write_check"
WRITE_OUTCOME_ATTRIBUTE: Final = "docchat.ratelimit.outcome"
WRITE_ATTEMPTS_ATTRIBUTE: Final = "docchat.ratelimit.attempts"

REQUEST_FAILED_RECORD: Final = "request_failed"
ADMIN_CHANGE_EVENT: Final = "admin_change"
INTERNAL_ERROR: Final = "internal_error"
NO_OPERATION: Final = "none"
"""The `operation` dimension of a problem no operation answered (an unknown path or method)."""

PROBLEMS: Final = counter("docchat.api.problems")
RATELIMIT_REJECTIONS: Final = counter("docchat.ratelimit.rejections")

_logger: Final = logging.getLogger(LOGGER_NAME)


def current_server_span() -> Span:
    """The span current when a request reaches the route class: the instrumentation's server
    span (or the correlation middleware's non-recording span when nothing records)."""
    return trace.get_current_span()


def record_caller(span: Span, principal: Principal) -> None:
    """`enduser.id` (the caller's `oid`) and `docchat.auth.client`, set once per request on the
    server span; child spans never repeat them."""
    set_attributes(
        span, **{ENDUSER_ATTRIBUTE: str(principal.oid), CLIENT_ATTRIBUTE: principal.client}
    )


def record_audit_action(span: Span, action: str) -> None:
    """`docchat.audit.action` on the server span of a request that wrote an audit row."""
    set_attributes(span, **{AUDIT_ACTION_ATTRIBUTE: action})


def record_problem(span: Span, code: str, status: int, operation: str) -> None:
    """One problem answered: the counter and the server span's `docchat.error_code`."""
    PROBLEMS.add(1, code=code, status=status, operation=operation)
    set_attributes(span, **{ERROR_CODE_ATTRIBUTE: code})


def report_request_failed(span: Span, exc: BaseException) -> None:
    """An unmapped exception: one `request_failed` record and the failure on the server span,
    with the exception's qualified type name and never its message, which can carry SQL
    parameters, document text or identifiers."""
    log_event(
        _logger,
        REQUEST_FAILED_RECORD,
        logging.ERROR,
        **{
            ERROR_TYPE_ATTRIBUTE: qualified_type_name(type(exc)),
            ERROR_CODE_ATTRIBUTE: INTERNAL_ERROR,
        },
    )
    record_failure(span, exc, INTERNAL_ERROR)


def record_rejection(reason: RejectionReason) -> None:
    RATELIMIT_REJECTIONS.add(1, reason=reason)


def write_check_span() -> AbstractContextManager[Span]:
    return start_span(WRITE_CHECK_SPAN)


def record_write_check(span: Span, outcome: WriteCheckOutcome, attempts: int) -> None:
    set_attributes(span, **{WRITE_OUTCOME_ATTRIBUTE: outcome, WRITE_ATTEMPTS_ATTRIBUTE: attempts})


def report_admin_change(action: str, target_type: str, project_id: UUID | None) -> None:
    """The custom event of one administrator write; `projectId` only when the row names one."""
    properties: dict[str, object] = {"action": action, "targetType": target_type}
    if project_id is not None:
        properties["projectId"] = str(project_id)
    emit_event(ADMIN_CHANGE_EVENT, **properties)
