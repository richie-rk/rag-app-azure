"""The conversation store's signals (`13-conversations-and-memory.md` sections 2, 3, 7.15 and 11.4;
`85-observability.md` sections 4.5 and 4.10).

The store's spans, its counters (finalised, duplicate, abandoned and superseded turns, truncated
traces, unsaved follow-ups, store conflicts, failed memory reads), the request-charge histogram per
operation, the record `memory_unavailable` and the event `followups_not_saved`. Attributes hold IDs,
counts, codes and request charges only, never a question, an answer, a title or fact text. The
instruments are created once, at import.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final, Literal
from uuid import UUID

from docchat_core.errors import CODES
from docchat_core.telemetry import LOGGER_NAME, emit_event, log_event, qualified_type_name
from docchat_core.telemetry.instruments import counter, histogram

type ChargeOperation = Literal[
    "begin_turn",
    "read_recent_turns",
    "finalize_turn",
    "attach_followups",
    "memory_read",
    "list_sessions",
    "get_session",
    "list_turns",
    "feedback",
    "memory_write",
]
type DuplicateOutcome = Literal["replay", "in_progress", "already_ended", "conflict"]
type AbandonSource = Literal["takeover", "duplicate"]

REQUEST_CHARGE_HEADER: Final = "x-ms-request-charge"
NO_ERROR_CODE: Final = "none"
MEMORY_UNAVAILABLE: Final = "memory_unavailable"
FOLLOWUPS_NOT_SAVED: Final = "followups_not_saved"

TURNS_FINALIZED: Final = counter("docchat.conversations.turns_finalized")
DUPLICATES: Final = counter("docchat.conversations.duplicates")
ABANDONED: Final = counter("docchat.conversations.abandoned")
FINALIZE_SUPERSEDED: Final = counter("docchat.conversations.finalize_superseded")
TRACE_TRUNCATED: Final = counter("docchat.conversations.trace_truncated")
FOLLOWUPS_NOT_SAVED_COUNT: Final = counter("docchat.conversations.followups_not_saved")
STORE_CONFLICTS: Final = counter("docchat.conversations.store_conflicts")
REQUEST_CHARGE: Final = histogram("docchat.conversations.request_charge")
MEMORY_READ_FAILURES: Final = counter("docchat.memory.read_failures")

_logger: Final = logging.getLogger(LOGGER_NAME)


class Charges:
    """Sums the request units of an operation's Cosmos DB calls; passed to each call as its
    `response_hook`."""

    def __init__(self) -> None:
        self.total = 0.0

    def hook(self, headers: Mapping[str, object], _body: object) -> None:
        value = headers.get(REQUEST_CHARGE_HEADER)
        try:
            self.total += float(str(value)) if value is not None else 0.0
        except ValueError:
            return

    def record(self, operation: ChargeOperation) -> float:
        """Record the operation's total in the histogram and return it for the span."""
        REQUEST_CHARGE.record(self.total, operation=operation)
        return self.total


def known_code(code: str | None) -> str | None:
    """`code` when the catalogue registers it, so a span attribute never carries free text."""
    return code if code is not None and code in CODES else None


def record_finalized(status: str, error_code: str | None) -> None:
    TURNS_FINALIZED.add(1, status=status, error_code=known_code(error_code) or NO_ERROR_CODE)


def record_duplicate(outcome: DuplicateOutcome) -> None:
    DUPLICATES.add(1, outcome=outcome)


def record_abandoned(source: AbandonSource) -> None:
    ABANDONED.add(1, source=source)


def record_conflict(operation: str) -> None:
    STORE_CONFLICTS.add(1, operation=operation)


def record_superseded() -> None:
    FINALIZE_SUPERSEDED.add(1)


def record_trace_truncated() -> None:
    TRACE_TRUNCATED.add(1)


def report_followups_not_saved(session_id: UUID, turn_id: UUID) -> None:
    emit_event(FOLLOWUPS_NOT_SAVED, sessionId=str(session_id), turnId=str(turn_id))
    FOLLOWUPS_NOT_SAVED_COUNT.add(1)


def report_memory_unavailable(exc: BaseException) -> None:
    """A failed prompt-memory read: one warning with the failure's type name, never its text."""
    log_event(
        _logger,
        MEMORY_UNAVAILABLE,
        logging.WARNING,
        **{"error.type": qualified_type_name(type(exc))},
    )
    MEMORY_READ_FAILURES.add(1)
