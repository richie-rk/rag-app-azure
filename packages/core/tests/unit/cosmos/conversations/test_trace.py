from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from docchat_core.cosmos.base import DocumentTooLargeError
from docchat_core.cosmos.conversations.ids import turn_trace_id
from docchat_core.cosmos.conversations.trace import (
    BroadenOutcome,
    ChatDeployment,
    EmbeddingDeployment,
    IndexSource,
    KeptOutcome,
    KeptSource,
    ModelInput,
    OmittedModelInput,
    PromptBudget,
    PromptMessage,
    ReasoningEffort,
    RetrievalRecord,
    RewriteOutcome,
    ScreeningOutcome,
    ScreeningRecord,
    SearchResultRecord,
    TraceDeployments,
    TraceTimings,
    TurnTraceDocument,
)

_OID = UUID("0b8f3c1e-6a2d-4f7b-9c55-1d2e3f405162")
_SESSION_ID = UUID("b6061e08-59bc-576e-a78d-b2d375c588e7")
_TURN_ID = UUID("445c1c2a-be4c-5d17-a92d-c4efcb094740")
_NOW = datetime(2026, 10, 14, 9, 31, tzinfo=UTC)
_IDS = [UUID(int=n) for n in range(1, 6)]
_MARKER = "9f86d081884c7d65"


def _trace(**changes: Any) -> TurnTraceDocument:
    values: dict[str, Any] = {
        "id": turn_trace_id(_TURN_ID),
        "user_id": _OID,
        "session_id": _SESSION_ID,
        "turn_id": _TURN_ID,
        "rewrite_outcome": RewriteOutcome.SKIPPED,
        "created_at": _NOW,
    }
    return TurnTraceDocument(**(values | changes))


def _result(n: int) -> SearchResultRecord:
    return SearchResultRecord(
        chunk_key="k" * 256,
        document_id=_IDS[0],
        chunk_set_id=_IDS[1],
        search_score=0.0331 * n,
        reranker_score=None if n % 2 else 2.75,
        served=True,
    )


def _kept(rank: int, outcome: KeptOutcome = KeptOutcome.SENT) -> KeptSource:
    return KeptSource(
        rank=rank,
        source_id=None if outcome is KeptOutcome.FLAGGED else f"S{rank}",
        chunk_key="k" * 256,
        document_id=_IDS[0],
        document_version_id=_IDS[2],
        chunk_set_id=_IDS[1],
        reranker_score=3.1,
        outcome=outcome,
    )


def _retrieval(**changes: Any) -> RetrievalRecord:
    values: dict[str, Any] = {
        "search_index_id": _IDS[3],
        "physical_index_id": _IDS[4],
        "physical_index_name": "docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g2",
        "index_source": IndexSource.PROJECT_DEFAULT,
        "query_embedding_deployment": "embed-query-te3-large-v1",
        "embedding_model": "text-embedding-3-large",
        "embedding_dimensions": 1_024,
        "candidate_count": 50,
        "keep_count": 16,
        "reranker_threshold": 2.0,
        "min_confident_results": 1,
        "rerank_partial": False,
        "broaden_attempted": True,
        "broadened": True,
        "broaden_outcome": BroadenOutcome.REPLACED,
        "search_passage": "p" * 1_200,
        "first_pass": [_result(n) for n in range(50)],
        "broadened_pass": [_result(n) for n in range(50)],
        "kept": [_kept(rank) for rank in range(1, 16)] + [_kept(16, KeptOutcome.FLAGGED)],
        "screening": ScreeningRecord(
            outcome=ScreeningOutcome.FLAGGED, calls=4, segments=18, characters=39_000, flagged=1
        ),
    }
    return RetrievalRecord(**(values | changes))


def _model_input(content_bytes: int) -> ModelInput:
    return ModelInput(
        rules_version="rules-v1",
        marker=_MARKER,
        deployment="chat",
        model="gpt-5.6-sol",
        max_completion_tokens=12_000,
        reasoning_effort=ReasoningEffort.LOW,
        messages=[
            PromptMessage(role="system", content="Rules."),
            PromptMessage(role="user", content="s" * content_bytes),
            PromptMessage(role="user", content="[[BEGIN QUESTION x]]"),
        ],
        prompt_tokens=28_000,
        dropped_turns=2,
        dropped_source_ids=["S16"],
    )


def _deployments() -> TraceDeployments:
    chat = ChatDeployment(
        deployment="chat",
        model="gpt-5.6-sol",
        model_version="2026-07-09",
        reasoning_effort=ReasoningEffort.LOW,
    )
    return TraceDeployments(
        chat=chat,
        small=chat.model_copy(update={"deployment": "chat-small", "model": "gpt-5.6-terra"}),
        query_embedding=EmbeddingDeployment(
            deployment="embed-query-te3-large-v1", model="text-embedding-3-large", dimensions=1_024
        ),
    )


def test_a_trace_of_a_turn_that_ended_early() -> None:
    item = _trace().to_item()

    assert item["type"] == "turn_trace"
    assert item["schemaVersion"] == 1
    assert item["turnId"] == str(_TURN_ID)
    for name in ("standaloneQuery", "searchQuery", "retrieval", "modelInput", "promptBudget"):
        assert item[name] is None
    assert item["deployments"] is None
    assert item["timings"] == dict.fromkeys(
        [
            "rewriteMs",
            "queryEmbeddingMs",
            "searchMs",
            "servedFilterMs",
            "searchPassageMs",
            "broadenedSearchMs",
            "screeningMs",
            "assemblyMs",
            "timeToFirstTokenMs",
            "generationMs",
            "totalMs",
        ]
    )
    assert item["contentFilterDegraded"] is False
    assert item["redactedAt"] is None


def test_the_id_derives_from_the_turn_id() -> None:
    with pytest.raises(ValidationError, match="turnId"):
        _trace(id=_TURN_ID)


def test_a_full_trace_reads_back_equal() -> None:
    trace = _trace(
        rewrite_outcome=RewriteOutcome.REWRITTEN,
        standalone_query="Which form starts a supplier change request?",
        search_query="supplier change request form",
        retrieval=_retrieval(),
        model_input=_model_input(10),
        prompt_budget=PromptBudget(
            budget_tokens=40_000,
            completion_reserve_tokens=12_000,
            memory_facts_included=3,
            summary_included=True,
        ),
        deployments=_deployments(),
        timings=TraceTimings(search_ms=180, total_ms=5_400),
    )

    stored = trace.to_item()

    assert stored["modelInput"]["droppedSourceIds"] == ["S16"]
    assert stored["retrieval"]["kept"][-1]["sourceId"] is None
    assert TurnTraceDocument.from_item(stored) == trace


def test_the_omitted_model_input_reads_back_as_omitted() -> None:
    trace = _trace(model_input=_model_input(10)).with_model_input_omitted()

    stored = trace.to_item()

    assert stored["modelInput"] == {"omitted": "size_limit"}
    assert TurnTraceDocument.from_item(stored).model_input == OmittedModelInput(
        omitted="size_limit"
    )


def test_a_stored_model_input_with_a_newer_field_never_reads_as_omitted() -> None:
    stored = _trace(model_input=_model_input(10)).to_item()
    stored["modelInput"]["fieldOfANewerRelease"] = 1

    assert isinstance(TurnTraceDocument.from_item(stored).model_input, ModelInput)


@pytest.mark.parametrize(
    "changes",
    [
        {"standalone_query": "q" * 1_201},
        {"search_query": "q" * 4_001},
        {"retrieval": {"firstPass": [{}] * 51}},
        {"model_input": {"omitted": "other"}},
        {"rewrite_outcome": "retried"},
    ],
)
def test_limits_are_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _trace(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"first_pass": [_result(0)] * 51},
        {"kept": [_kept(1)] * 17},
        {"search_passage": "p" * 1_201},
        {"index_source": "admin_choice"},
        {"broaden_outcome": "skipped"},
    ],
)
def test_retrieval_limits_are_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _retrieval(**changes)


@pytest.mark.parametrize("marker", ["9F86D081884C7D65", "9f86d081884c7d6", "x" * 16])
def test_the_marker_is_16_lower_case_hex_characters(marker: str) -> None:
    with pytest.raises(ValidationError):
        ModelInput.model_validate(_model_input(1).model_dump() | {"marker": marker})


def test_messages_use_only_the_three_roles() -> None:
    with pytest.raises(ValidationError):
        PromptMessage(role="developer", content="x")  # type: ignore[arg-type]


def test_t093_a_trace_at_its_largest_model_input_fits_its_size_limit() -> None:
    # The model input reaches about 800 KB at the largest prompt budget.
    trace = _trace(
        standalone_query="q" * 1_200,
        search_query="q" * 4_000,
        retrieval=_retrieval(),
        model_input=_model_input(800_000),
        deployments=_deployments(),
    )

    assert trace.json_size() <= TurnTraceDocument.max_bytes == 1_300_000
    trace.to_item()


def test_t093_an_oversized_trace_is_refused_until_its_model_input_is_omitted() -> None:
    trace = _trace(retrieval=_retrieval(), model_input=_model_input(1_300_000))

    with pytest.raises(DocumentTooLargeError):
        trace.to_item()
    assert trace.with_model_input_omitted().to_item()["modelInput"] == {"omitted": "size_limit"}


def test_redaction_time_is_stored() -> None:
    assert _trace(redacted_at=_NOW).to_item()["redactedAt"] == "2026-10-14T09:31:00.000Z"
