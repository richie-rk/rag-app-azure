from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from docchat_core.cosmos.base import DocumentTooLargeError
from docchat_core.cosmos.conversations.ids import new_session_id, turn_id
from docchat_core.cosmos.conversations.turn import (
    BillableCounts,
    CallOutcome,
    CitationContentType,
    ExtractionState,
    Feedback,
    FeedbackCategory,
    FeedbackRating,
    InternalErrorCode,
    MemoryExtractionState,
    ModelCall,
    ModelCallUsage,
    RejectedCounts,
    StoredCitation,
    TurnDocument,
    TurnError,
    TurnStatus,
    TurnUsage,
    UsageTotals,
)

_OID = UUID("0b8f3c1e-6a2d-4f7b-9c55-1d2e3f405162")
_CLIENT_TURN_ID = UUID("7d9e2b4a-1c3f-4e5d-8a6b-9f0e1d2c3b4a")
_SESSION_ID = new_session_id(_OID, _CLIENT_TURN_ID)
_TURN_ID = turn_id(_SESSION_ID, _CLIENT_TURN_ID)
_NOW = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_IDS = [UUID(int=n) for n in range(1, 6)]


def _turn(**changes: Any) -> TurnDocument:
    values: dict[str, Any] = {
        "id": _TURN_ID,
        "user_id": _OID,
        "session_id": _SESSION_ID,
        "seq": 1,
        "client_turn_id": _CLIENT_TURN_ID,
        "project_id": _IDS[0],
        "status": TurnStatus.STREAMING,
        "question": "Which form starts a supplier change request?",
        "trace_id": _TRACE,
        "deadline_at": _NOW + timedelta(seconds=210),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return TurnDocument(**(values | changes))


def _citation(number: int = 1, *, title: str = "Travel policy", text: str = "x") -> StoredCitation:
    return StoredCitation(
        number=number,
        source_id=f"S{number}",
        chunk_key=f"{_IDS[1]}_{_IDS[2]}_{number}",
        search_index_id=_IDS[3],
        physical_index_id=_IDS[4],
        document_id=_IDS[1],
        document_version_id=_IDS[0],
        chunk_set_id=_IDS[2],
        title=title,
        page_number=3,
        heading_path=[text],
        content_type=CitationContentType.PDF,
        open_as="pdf_page",
    )


def _usage(
    call: ModelCall = ModelCall.ANSWER, *, cost: int | None = 1_750, estimated: bool = False
) -> ModelCallUsage:
    return ModelCallUsage(
        call=call,
        deployment="chat",
        model="gpt-5.6-sol",
        prompt_tokens=1_000,
        completion_tokens=300,
        reasoning_tokens=100,
        cached_tokens=200,
        cache_write_tokens=0,
        total_tokens=1_300,
        estimated=estimated,
        estimated_cost_micros=cost,
        duration_ms=4_200,
        outcome=CallOutcome.OK,
    )


def test_a_turn_is_created_with_the_specified_values() -> None:
    item = _turn().to_item()

    assert item["type"] == "turn"
    assert item["schemaVersion"] == 1
    assert item["status"] == "streaming"
    assert item["answer"] == ""
    assert item["answerTruncated"] is False
    assert item["citations"] == item["allowedLinks"] == item["followups"] == []
    for name in ("finishReason", "error", "followupsAt", "feedback", "historyTokens", "endedAt"):
        assert item[name] is None
    assert item["usage"]["calls"] == []
    assert item["usage"]["totals"]["totalTokens"] == 0
    assert item["usage"]["totals"]["estimatedCostMicros"] == 0
    extraction = item["memoryExtraction"]
    assert (extraction["state"], extraction["reason"], extraction["attempts"]) == (
        "not_applicable",
        None,
        0,
    )
    assert item["traceId"] == _TRACE
    assert item["createdAt"] == "2026-10-14T09:30:00.000Z"
    assert item["deadlineAt"] == "2026-10-14T09:33:30.000Z"


def test_the_stored_field_names() -> None:
    assert set(_turn().to_item()) == {
        "id", "userId", "sessionId", "type", "schemaVersion", "seq", "clientTurnId", "projectId",
        "status", "question", "answer", "answerTruncated", "finishReason", "error", "citations",
        "allowedLinks", "followups", "followupsAt", "feedback", "historyTokens", "usage",
        "memoryExtraction", "traceId", "deadlineAt", "createdAt", "endedAt", "updatedAt",
    }  # fmt: skip


def test_the_id_derives_from_the_session_and_the_client_turn_id() -> None:
    with pytest.raises(ValidationError, match="derives"):
        _turn(id=_IDS[0])


def test_a_stored_turn_reads_back_equal() -> None:
    turn = _turn(
        status=TurnStatus.COMPLETED,
        answer="Use form SC-1 ⟦1⟧.",
        citations=[_citation()],
        usage=TurnUsage.of([_usage()]),
        ended_at=_NOW,
    )

    assert TurnDocument.from_item({**turn.to_item(), "_etag": '"1"'}) == turn


@pytest.mark.parametrize(
    "changes",
    [
        {"question": ""},
        {"question": "q" * 4_001},
        {"answer": "a" * 100_001},
        {"seq": 0},
        {"seq": 5_001},
        {"citations": [_citation(n % 16 + 1) for n in range(17)]},
        {"citations": [_citation(1), _citation(1)]},
        {"allowed_links": ["https://example.org/a"] * 51},
        {"allowed_links": ["http://example.org/a"]},
        {"allowed_links": ["https:///no-host"]},
        {"allowed_links": ["https://example.org/" + "a" * 2_029]},
        {"followups": ["a", "b", "c", "d"]},
        {"followups": [""]},
        {"followups": ["f" * 201]},
        {"history_tokens": -1},
        {"trace_id": "0" * 32},
        {"trace_id": _TRACE.upper()},
        {"error": {"code": "Not A Code", "retryable": True}},
        {"error": {"code": "c" * 65, "retryable": True}},
        {"status": "abandoned"},
        {"finish_reason": "timeout"},
        {"created_at": datetime(2026, 10, 14, 9, 30)},  # noqa: DTZ001 - naive on purpose
    ],
)
def test_limits_are_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _turn(**changes)


def test_limits_at_their_maximum_pass() -> None:
    _turn(
        question="q" * 4_000,
        answer="a" * 100_000,
        seq=5_000,
        citations=[_citation(n) for n in range(1, 17)],
        allowed_links=["https://example.org/" + "a" * 2_028] * 50,
        followups=["f" * 200] * 3,
        error=TurnError(code="turn_abandoned", retryable=True),
    ).to_item()


def test_t093_a_turn_at_its_field_maximums_fits_its_size_limit() -> None:
    usage = TurnUsage.of([_usage(call) for call in ModelCall] + [_usage(), _usage()])
    feedback = Feedback(
        rating=FeedbackRating.DOWN,
        category=FeedbackCategory.INCOMPLETE,
        comment="c" * 2_000,
        created_at=_NOW,
        updated_at=_NOW,
    )
    turn = _turn(
        status=TurnStatus.COMPLETED,
        question="q" * 4_000,
        answer="a" * 100_000,
        citations=[
            _citation(n, title="t" * 300, text="h" * 200).model_copy(
                update={"heading_path": ["h" * 200] * 8}
            )
            for n in range(1, 17)
        ],
        allowed_links=["https://example.org/" + "a" * 2_028] * 50,
        followups=["f" * 200] * 3,
        followups_at=_NOW,
        feedback=feedback,
        history_tokens=2_147_483_647,
        usage=usage,
        memory_extraction=MemoryExtractionState(
            state=ExtractionState.FAILED,
            error_code=InternalErrorCode.STORE_CONFLICT,
            usage=_usage(ModelCall.MEMORY),
            rejected=RejectedCounts(**dict.fromkeys(RejectedCounts.model_fields, 2_147_483_647)),
            updated_at=_NOW,
        ),
        ended_at=_NOW,
    )

    assert turn.json_size() <= TurnDocument.max_bytes == 350_000
    turn.to_item()


def test_t093_a_turn_above_350000_bytes_is_refused() -> None:
    turn = _turn(answer="😀" * 100_000)

    with pytest.raises(DocumentTooLargeError):
        turn.to_item()


def test_feedback_category_only_with_a_down_rating() -> None:
    with pytest.raises(ValidationError, match="only when the rating is down"):
        Feedback(
            rating=FeedbackRating.UP,
            category=FeedbackCategory.OTHER,
            created_at=_NOW,
            updated_at=_NOW,
        )
    with pytest.raises(ValidationError):
        Feedback(rating=FeedbackRating.DOWN, comment="c" * 2_001, created_at=_NOW, updated_at=_NOW)


def test_citation_view_has_exactly_the_public_fields() -> None:
    view = _citation().view()

    assert view.model_dump(mode="json") == {
        "number": 1,
        "title": "Travel policy",
        "pageNumber": 3,
        "headingPath": ["x"],
        "contentType": "application/pdf",
        "openAs": "pdf_page",
    }


@pytest.mark.parametrize("source_id", ["S0", "S17", "s1", "S01"])
def test_source_ids_are_s1_to_s16(source_id: str) -> None:
    with pytest.raises(ValidationError):
        StoredCitation.model_validate(_citation().model_dump() | {"sourceId": source_id})


def test_the_three_content_types_and_one_viewer() -> None:
    assert {member.value for member in CitationContentType} == {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    with pytest.raises(ValidationError):
        StoredCitation.model_validate(_citation().model_dump() | {"openAs": "document"})


def test_a_usage_record_totals_prompt_plus_completion() -> None:
    with pytest.raises(ValidationError, match="totalTokens"):
        ModelCallUsage.model_validate(_usage().model_dump() | {"totalTokens": 1})
    assert _usage(cost=None).estimated_cost_micros is None


def test_usage_totals_over_the_calls() -> None:
    usage = TurnUsage.of(
        [_usage(), _usage(ModelCall.FOLLOWUPS, estimated=True)], BillableCounts(semantic_queries=2)
    )

    assert usage.totals == UsageTotals(
        prompt_tokens=2_000,
        completion_tokens=600,
        reasoning_tokens=200,
        cached_tokens=400,
        cache_write_tokens=0,
        total_tokens=2_600,
        estimated_cost_micros=3_500,
        unpriced_calls=0,
        estimated=True,
    )
    assert usage.billable.semantic_queries == 2


def test_one_unpriced_call_makes_the_cost_unknown() -> None:
    totals = TurnUsage.of([_usage(), _usage(cost=None)]).totals

    assert totals.estimated_cost_micros is None
    assert totals.unpriced_calls == 1


def test_attaching_a_record_recomputes_the_totals() -> None:
    usage = TurnUsage.of([_usage()]).with_call(_usage(ModelCall.FOLLOWUPS))

    assert [call.call for call in usage.calls] == [ModelCall.ANSWER, ModelCall.FOLLOWUPS]
    assert usage.totals.total_tokens == 2_600


def test_totals_that_disagree_with_the_calls_are_refused() -> None:
    stale = TurnUsage.of([_usage()]).model_dump()

    with pytest.raises(ValidationError, match="computed over the calls"):
        TurnUsage.model_validate(stale | {"calls": []})


def test_at_most_ten_usage_records() -> None:
    with pytest.raises(ValidationError):
        TurnUsage.of([_usage()] * 11)


def test_the_model_calls_and_outcomes() -> None:
    assert [call.value for call in ModelCall] == [
        "rewrite",
        "search_passage",
        "answer",
        "followups",
        "query_embedding",
        "passage_embedding",
        "summary",
        "memory",
    ]
    assert [outcome.value for outcome in CallOutcome] == [
        "ok",
        "filtered",
        "failed",
        "cancelled",
        "timed_out",
    ]


def test_rejected_counts_are_stored_under_the_reason_codes() -> None:
    assert list(RejectedCounts().model_dump(mode="json")) == [
        "sensitivity",
        "invalid_target",
        "empty",
        "too_long",
        "contains_link",
        "contains_code",
        "contains_instruction",
        "contains_secret",
        "duplicate_fact",
        "cap_reached",
        "over_limit",
    ]


@pytest.mark.parametrize(
    "changes",
    [{"attempts": 6}, {"facts_added": 11}, {"facts_replaced": 11}, {"shield_calls": 1_001}],
)
def test_extraction_counters_have_ceilings(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MemoryExtractionState(state=ExtractionState.RUNNING, **changes)


def test_the_extraction_states_and_internal_codes() -> None:
    assert [state.value for state in ExtractionState] == [
        "not_applicable",
        "pending",
        "running",
        "done",
        "skipped_paused",
        "skipped_disabled",
        "superseded",
        "expired",
        "failed",
    ]
    assert [code.value for code in InternalErrorCode] == [
        "model_unavailable",
        "content_filtered",
        "invalid_output",
        "store_conflict",
    ]
    with pytest.raises(ValidationError):
        MemoryExtractionState(state=ExtractionState.NOT_APPLICABLE, reason="other")  # type: ignore[arg-type]
