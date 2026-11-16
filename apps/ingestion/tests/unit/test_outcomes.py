"""Unit tests of item outcomes and run end states (`14-ingestion.md` sections 6.11 and 6.13); the
failure side effects are `docchat_core.ingestion.runs`'s and tested there."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from docchat_core.errors import ITEM_CODES, ItemCodeKind
from docchat_core.ingestion.models import ItemWarning, RunState
from docchat_ingestion.outcomes import (
    SKIP_CODES,
    ItemOutcome,
    ItemUsage,
    collect_warnings,
    run_end_state,
)


@pytest.mark.parametrize(
    ("counts", "state"),
    [
        ({"succeeded": 0, "skipped": 0, "failed": 0, "cancelled": 0}, RunState.COMPLETED),
        ({"succeeded": 3, "skipped": 1, "failed": 0, "cancelled": 0}, RunState.COMPLETED),
        ({"succeeded": 0, "skipped": 0, "failed": 2, "cancelled": 0}, RunState.FAILED),
        (
            {"succeeded": 1, "skipped": 0, "failed": 2, "cancelled": 0},
            RunState.COMPLETED_WITH_ERRORS,
        ),
        (
            {"succeeded": 0, "skipped": 1, "failed": 2, "cancelled": 0},
            RunState.COMPLETED_WITH_ERRORS,
        ),
        (
            {"succeeded": 0, "skipped": 0, "failed": 1, "cancelled": 1},
            RunState.COMPLETED_WITH_ERRORS,
        ),
        ({"succeeded": 0, "skipped": 0, "failed": 0, "cancelled": 4}, RunState.COMPLETED),
    ],
)
def test_run_end_states(counts: dict[str, int], state: RunState) -> None:
    assert run_end_state(**counts) is state


def test_the_skip_codes_are_catalogued_skips() -> None:
    for code in SKIP_CODES:
        assert ITEM_CODES[code].kind in {ItemCodeKind.SKIP, ItemCodeKind.SKIP_OR_CANCEL}


def test_an_outcome_reads_with_or_without_the_envelope() -> None:
    item = uuid4()
    bare = ItemOutcome.model_validate(
        {
            "itemId": str(item),
            "claimNumber": 2,
            "state": "failed",
            "code": "render_failed",
            "stage": "render",
            "warnings": [],
            "usage": {
                "embeddingTokens": 0,
                "summaryInputTokens": 0,
                "summaryOutputTokens": 0,
                "docIntelPages": 0,
            },
        },
        by_alias=True,
        by_name=False,
    )
    assert bare.run_id is None
    assert bare.item_state.value == "failed"


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("succeeded", "already_served"),
        ("skipped", None),
        ("skipped", "render_failed"),
        ("failed", None),
    ],
)
def test_codes_follow_the_state(state: str, code: str | None) -> None:
    with pytest.raises(ValidationError):
        ItemOutcome(item_id=uuid4(), claim_number=1, state=state, code=code)  # type: ignore[arg-type]


def test_the_first_50_warnings_are_kept() -> None:
    warnings = [ItemWarning(code="table_summary_filtered", page=page) for page in range(1, 61)]
    outcome = ItemOutcome(item_id=uuid4(), claim_number=1, state="succeeded", warnings=warnings)
    assert [warning.page for warning in outcome.kept_warnings()] == list(range(1, 51))
    assert collect_warnings([warnings[:2], [], warnings[2:3]]) == warnings[:3]


def test_usage_sums() -> None:
    usage = ItemUsage().plus(embedding_tokens=5).plus(doc_intel_pages=2, summary_output_tokens=1)
    assert usage.model_dump(mode="json") == {
        "embeddingTokens": 5,
        "summaryInputTokens": 0,
        "summaryOutputTokens": 1,
        "docIntelPages": 2,
    }
