"""The chat request and the shared public models (T-211; `12-chat-pipeline.md` sections 2.1, 11.3
and 14.2)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from docchat_api.chat.models import (
    REQUEST_EXAMPLE,
    ChatTurnRequest,
    Citation,
    TurnUsageSummary,
    normalise_question,
)
from docchat_api.http.problems import field_issues
from docchat_core.cosmos.conversations import CitationContentType, CitationView, UsageTotals


def body(**changes: Any) -> dict[str, Any]:
    return {**REQUEST_EXAMPLE, **changes}


def issues(payload: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(ValidationError) as failure:
        ChatTurnRequest.model_validate(payload)
    return [
        (issue.path, issue.reason)
        for issue in field_issues(failure.value.errors(include_input=False))
    ]


def test_the_example_is_accepted_and_lower_cased() -> None:
    request = ChatTurnRequest.model_validate(body(projectId=REQUEST_EXAMPLE["projectId"].upper()))

    assert str(request.project_id) == REQUEST_EXAMPLE["projectId"]
    assert request.session_id is None
    assert request.question == REQUEST_EXAMPLE["question"]


def test_t211_unknown_fields_and_non_canonical_uuids_are_refused() -> None:
    assert ("body.indexId", "unknown_field") in issues(body(indexId="x"))
    assert issues(body(projectId="{3f2b8c1e-7d4a-4e9b-9c55-0a6f1d2e8b74}")) == [
        ("body.projectId", "invalid_uuid")
    ]
    assert issues(body(sessionId="3f2b8c1e7d4a4e9b9c550a6f1d2e8b74")) == [
        ("body.sessionId", "invalid_uuid")
    ]
    assert issues({key: value for key, value in body().items() if key != "clientTurnId"}) == [
        ("body.clientTurnId", "required")
    ]


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        pytest.param("x" * 4_001, "too_long", id="4001-code-points"),
        ("", "empty"),
        ("   \n\t ", "empty"),
        ("\u200b\u2060 \u200b", "empty"),
        ("\u0301\u0301", "empty"),
        ("what\x00now", "invalid_characters"),
        ("bell\x07", "invalid_characters"),
        ("lone \ud800 surrogate", "invalid_characters"),
    ],
)
def test_t211_refused_questions(question: str, reason: str) -> None:
    assert issues(body(question=question)) == [("body.question", reason)]


def test_t211_exactly_4000_code_points_after_normalisation_and_trimming() -> None:
    question = "  " + "x" * 3_998 + "\r\n" + "y  "

    request = ChatTurnRequest.model_validate(body(question=question))

    assert len(request.question) == 4_000
    assert "\r" not in request.question


def test_t211_one_character_of_category_p_and_inner_format_characters() -> None:
    assert ChatTurnRequest.model_validate(body(question="?")).question == "?"
    assert ChatTurnRequest.model_validate(body(question="a\u200bb")).question == "a\u200bb"


def test_normalisation_order() -> None:
    assert normalise_question("  e\u0301\r\nx\r ") == "\u00e9\nx"


def test_citation_and_usage_projections() -> None:
    view = CitationView(
        number=2,
        title="Handbook",
        page_number=7,
        heading_path=["Training"],
        content_type=CitationContentType.DOCX,
        open_as="pdf_page",
    )
    citation = Citation.of(view)
    totals = UsageTotals.over([])

    assert citation.model_dump(by_alias=True, mode="json") == {
        "number": 2,
        "title": "Handbook",
        "pageNumber": 7,
        "headingPath": ["Training"],
        "contentType": CitationContentType.DOCX.value,
        "openAs": "pdf_page",
    }
    assert TurnUsageSummary.of(totals).model_dump(by_alias=True) == {
        "promptTokens": 0,
        "completionTokens": 0,
        "reasoningTokens": 0,
        "cachedTokens": 0,
        "totalTokens": 0,
        "estimated": False,
    }
