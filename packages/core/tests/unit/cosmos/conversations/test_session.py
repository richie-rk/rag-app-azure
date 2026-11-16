from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from docchat_core.cosmos.conversations.session import (
    ActiveTurn,
    SessionDocument,
    SessionHistory,
    SessionSummary,
    SummaryJob,
)
from docchat_core.cosmos.conversations.turn import (
    CallOutcome,
    InternalErrorCode,
    ModelCall,
    ModelCallUsage,
)

_OID = UUID("0b8f3c1e-6a2d-4f7b-9c55-1d2e3f405162")
_SESSION_ID = UUID("b6061e08-59bc-576e-a78d-b2d375c588e7")
_TURN_ID = UUID("445c1c2a-be4c-5d17-a92d-c4efcb094740")
_PROJECT_ID = UUID("ef87ff47-57f2-42a1-8c93-4ea2bca4792c")
_NOW = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)


def _session(**changes: Any) -> SessionDocument:
    values: dict[str, Any] = {
        "id": _SESSION_ID,
        "user_id": _OID,
        "session_id": _SESSION_ID,
        "project_id": _PROJECT_ID,
        "title": "Which form starts a supplier change request?",
        "created_at": _NOW,
        "updated_at": _NOW,
        "last_turn_at": _NOW,
        "turn_count": 1,
        "active_turn": ActiveTurn(turn_id=_TURN_ID, deadline_at=_NOW + timedelta(seconds=210)),
    }
    return SessionDocument(**(values | changes))


def _summary_usage() -> ModelCallUsage:
    return ModelCallUsage(
        call=ModelCall.SUMMARY,
        deployment="chat-small",
        model="gpt-5.6-terra",
        prompt_tokens=4_000,
        completion_tokens=900,
        reasoning_tokens=100,
        cached_tokens=0,
        cache_write_tokens=0,
        total_tokens=4_900,
        estimated=False,
        estimated_cost_micros=None,
        duration_ms=8_000,
        outcome=CallOutcome.OK,
    )


def test_a_new_session_has_the_empty_summary_and_job() -> None:
    item = _session().to_item()

    assert item["type"] == "session"
    assert item["schemaVersion"] == 1
    assert item["activeTurn"] == {"turnId": str(_TURN_ID), "deadlineAt": "2026-10-14T09:33:30.000Z"}
    assert item["history"] == {"unsummarisedTokens": 0, "unsummarisedTurns": 0}
    assert item["summary"] == {
        "text": None,
        "tokens": 0,
        "coversThroughSeq": 0,
        "version": 0,
        "updatedAt": None,
        "deployment": None,
        "lastUsage": None,
    }
    assert item["summaryJob"] == {
        "leaseExpiresAt": None,
        "consecutiveFailures": 0,
        "notBefore": None,
        "lastErrorCode": None,
        "lastAttemptAt": None,
        "lastSucceededAt": None,
    }


def test_the_id_is_the_session_id() -> None:
    with pytest.raises(ValidationError, match="sessionId"):
        _session(id=_TURN_ID)


def test_a_stored_session_reads_back_equal() -> None:
    session = _session(active_turn=None, history=SessionHistory(unsummarised_tokens=3_100))

    assert SessionDocument.from_item({**session.to_item(), "_ts": 1}) == session


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "t" * 81},
        {"turn_count": 0},
        {"turn_count": 5_001},
        {"summary": SessionSummary.model_construct(text="")},
        {"summary": {"text": "s" * 6_001}},
        {"summary": {"deployment": "d" * 65}},
        {"summary": {"coversThroughSeq": 2}},
        {"summary_job": {"consecutiveFailures": 1_001}},
        {"summary_job": {"lastErrorCode": "throttled"}},
        {"history": {"unsummarisedTokens": -1}},
    ],
)
def test_limits_are_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _session(**changes).to_item()


def test_a_title_counts_code_points() -> None:
    assert _session(title="😀" * 80).title == "😀" * 80


def test_t093_a_session_at_its_field_maximums_fits_its_size_limit() -> None:
    session = _session(
        title="😀" * 80,
        turn_count=5_000,
        history=SessionHistory(unsummarised_tokens=2_147_483_647, unsummarised_turns=5_000),
        summary=SessionSummary(
            text="😀" * 6_000,
            tokens=2_000,
            covers_through_seq=5_000,
            version=2_147_483_647,
            updated_at=_NOW,
            deployment="d" * 64,
            last_usage=_summary_usage(),
        ),
        summary_job=SummaryJob(
            lease_expires_at=_NOW,
            consecutive_failures=1_000,
            not_before=_NOW,
            last_error_code=InternalErrorCode.MODEL_UNAVAILABLE,
            last_attempt_at=_NOW,
            last_succeeded_at=_NOW,
        ),
    )

    assert session.json_size() <= SessionDocument.max_bytes == 64_000
    session.to_item()
