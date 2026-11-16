"""The provisioning signals (`85-observability.md` sections 4.3, 4.8 and 4.12; ADR 0021 item 9):
the span `docchat.users.ensure`, the counter `docchat.users.provisioned` and the custom event
`user_provisioned`. Failed spans feed the alert `user-provisioning-failures`. No display name or
e-mail address is ever carried; the instruments are created once, at import.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Final, Literal
from uuid import UUID

from opentelemetry.trace import Span

from docchat_core.telemetry import emit_event, set_attributes, start_span
from docchat_core.telemetry.instruments import counter

type EnsureOutcome = Literal["skipped", "touched", "provisioned", "failed"]

ENSURE_SPAN: Final = "docchat.users.ensure"
PROVISIONED_EVENT: Final = "user_provisioned"
OUTCOME_ATTRIBUTE: Final = "docchat.users.outcome"
DEFAULT_PROJECT_COUNT_ATTRIBUTE: Final = "docchat.users.default_project_count"

PROVISIONED: Final = counter("docchat.users.provisioned")


def ensure_span() -> AbstractContextManager[Span]:
    """One span per provisioning step, a skipped one included."""
    return start_span(ENSURE_SPAN)


def record_outcome(span: Span, outcome: EnsureOutcome) -> None:
    set_attributes(span, **{OUTCOME_ATTRIBUTE: outcome})


def report_provisioned(span: Span, oid: UUID, default_project_count: int) -> None:
    """A new user: the span's outcome and count, the counter and the event."""
    set_attributes(
        span,
        **{
            OUTCOME_ATTRIBUTE: "provisioned",
            DEFAULT_PROJECT_COUNT_ATTRIBUTE: default_project_count,
        },
    )
    PROVISIONED.add(1)
    emit_event(PROVISIONED_EVENT, oid=str(oid), defaultProjectCount=default_project_count)
