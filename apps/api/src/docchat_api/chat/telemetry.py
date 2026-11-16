"""The chat pipeline's signals (`12-chat-pipeline.md` section 19; `85-observability.md` sections
4.4 and 4.9).

The metrics of section 19.2 that the pipeline emits, its custom events (`source_screening_flagged`,
`content_filter_triggered`, `content_filter_degraded`) and its log records (`chat_turn_finished`,
`chat_drained`, and `request_failed` for an unexpected fault after `200`). Everything is metadata:
identifiers, counts, codes, durations and token figures, never question, answer, source, summary,
memory or prompt text, a title, a heading, a file name or a URL. The instruments are created once,
at import.
"""

from __future__ import annotations

import logging
from typing import Final, Literal
from uuid import UUID

from docchat_core.errors import CODES
from docchat_core.telemetry import LOGGER_NAME, emit_event, log_event, qualified_type_name
from docchat_core.telemetry.instruments import counter, histogram

type DisconnectPhase = Literal["before_finalisation", "after_finalisation"]
type ScreeningFailure = Literal["timeout", "throttled", "error"]
type FilterWhere = Literal["prompt", "completion"]

NO_ERROR_CODE: Final = "none"
CHAT_TURN_FINISHED: Final = "chat_turn_finished"
CHAT_DRAINED: Final = "chat_drained"
REQUEST_FAILED: Final = "request_failed"
INTERNAL_ERROR: Final = "internal_error"

TIME_TO_FIRST_TOKEN: Final = histogram("docchat.chat.time_to_first_token")
TURN_DURATION: Final = histogram("docchat.chat.turn.duration")
TURNS: Final = counter("docchat.chat.turns")
BROADENED_SEARCHES: Final = counter("docchat.chat.broadened_searches")
FLAGGED_SOURCES: Final = counter("docchat.chat.screening.flagged_sources")
SCREENING_FAILURES: Final = counter("docchat.chat.screening.failures")
PROMPT_TOKENS: Final = histogram("docchat.chat.prompt.tokens")
PROMPT_DROPPED: Final = counter("docchat.chat.prompt.dropped")
DISCONNECTS: Final = counter("docchat.chat.disconnects")
HEARTBEATS: Final = counter("docchat.chat.heartbeats")
DRAINED_TURNS: Final = counter("docchat.chat.drained_turns")
FIRST_CHUNK_LATENCY: Final = histogram("docchat.openai.first_chunk_latency")
OPENAI_THROTTLED: Final = counter("docchat.openai.throttled")
SEARCH_DURATION: Final = histogram("docchat.search.duration")
RERANK_PARTIAL: Final = counter("docchat.search.rerank_partial")

_logger: Final = logging.getLogger(LOGGER_NAME)


def known_code(code: str | None) -> str | None:
    """`code` when the catalogue registers it, so an attribute never carries free text."""
    return code if code is not None and code in CODES else None


def record_heartbeat() -> None:
    HEARTBEATS.add(1)


def record_turn(outcome: str, error_code: str | None, duration_ms: int) -> None:
    TURNS.add(1, outcome=outcome, error_code=known_code(error_code) or NO_ERROR_CODE)
    TURN_DURATION.record(duration_ms, outcome=outcome)


def record_time_to_first_token(milliseconds: int, *, broadened: bool, deployment: str) -> None:
    TIME_TO_FIRST_TOKEN.record(milliseconds, broadened=broadened, deployment=deployment)


def record_broadened_search(outcome: Literal["replaced", "failed"]) -> None:
    BROADENED_SEARCHES.add(1, outcome=outcome)


def record_flagged_source() -> None:
    FLAGGED_SOURCES.add(1)


def record_screening_failure(reason: ScreeningFailure) -> None:
    SCREENING_FAILURES.add(1, reason=reason)


def record_prompt(tokens: int, *, dropped_turns: int, dropped_sources: int) -> None:
    PROMPT_TOKENS.record(tokens)
    if dropped_turns:
        PROMPT_DROPPED.add(dropped_turns, kind="turn")
    if dropped_sources:
        PROMPT_DROPPED.add(dropped_sources, kind="source")


def record_disconnect(phase: DisconnectPhase) -> None:
    DISCONNECTS.add(1, phase=phase)


def record_drained_turn() -> None:
    DRAINED_TURNS.add(1)


def record_first_chunk(milliseconds: int, deployment: str) -> None:
    FIRST_CHUNK_LATENCY.record(milliseconds, deployment=deployment)


def record_throttled(deployment: str, call: str) -> None:
    OPENAI_THROTTLED.add(1, deployment=deployment, call=call)


def record_search(milliseconds: int, search_pass: Literal["first", "broadened"]) -> None:
    SEARCH_DURATION.record(milliseconds, **{"pass": search_pass})


def record_rerank_partial() -> None:
    RERANK_PARTIAL.add(1)


def report_flagged_source(
    *,
    document_id: UUID,
    chunk_set_id: UUID,
    index_id: UUID,
    project_id: UUID,
    session_id: UUID,
    turn_id: UUID,
) -> None:
    """One flagged source, by its identifiers only, so an administrator can review it."""
    emit_event(
        "source_screening_flagged",
        documentId=str(document_id),
        chunkSetId=str(chunk_set_id),
        indexId=str(index_id),
        projectId=str(project_id),
        sessionId=str(session_id),
        turnId=str(turn_id),
    )
    record_flagged_source()


def report_content_filter(
    *,
    where: FilterWhere,
    call: str,
    deployment: str,
    project_id: UUID,
    session_id: UUID,
    turn_id: UUID,
) -> None:
    emit_event(
        "content_filter_triggered",
        where=where,
        call=call,
        deployment=deployment,
        projectId=str(project_id),
        sessionId=str(session_id),
        turnId=str(turn_id),
    )


def report_content_filter_degraded(
    *, deployment: str, project_id: UUID, session_id: UUID, turn_id: UUID
) -> None:
    emit_event(
        "content_filter_degraded",
        deployment=deployment,
        projectId=str(project_id),
        sessionId=str(session_id),
        turnId=str(turn_id),
    )


def report_unexpected(exc: BaseException) -> None:
    """An unexpected fault after `200` (an out-of-order write included): one `request_failed`
    record with the exception's qualified type name, never its text."""
    log_event(
        _logger,
        REQUEST_FAILED,
        logging.ERROR,
        **{"error.type": qualified_type_name(type(exc)), "docchat.error_code": INTERNAL_ERROR},
    )


def report_drained(*, turn_tasks: int, background_tasks: int, downloads: int) -> None:
    log_event(
        _logger,
        CHAT_DRAINED,
        logging.WARNING,
        **{
            "docchat.chat.turn_tasks": turn_tasks,
            "docchat.chat.background_tasks": background_tasks,
            "docchat.chat.downloads": downloads,
        },
    )


def report_turn_finished(attributes: dict[str, object]) -> None:
    """`chat_turn_finished`, one per turn: the IDs, outcome, error code, finish reason, counts,
    durations, token totals and estimated cost."""
    log_event(_logger, CHAT_TURN_FINISHED, logging.INFO, **attributes)
