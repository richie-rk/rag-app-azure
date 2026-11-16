"""Item outcomes and run end states (`14-ingestion.md` sections 6.11, 6.13, 16.4, 16.5 and 17.2;
ADR 0034).

An item that ran ends `succeeded`, `skipped` with one of the skip codes or `failed` with a failure
code, its last stage, its warnings (the first 50) and its usage counts; one failed document
never fails its run, which ends `completed`, `completed_with_errors` or `failed` by the rule of
section 6.13 item 2.2. `ItemOutcome` is the input of `record_item_outcome`, which both
sub-orchestrations send. The end-state rule lives here once, for finalisation (sweep duty 9); the
failure side effects of section 6.11, which `record_item_outcome`, sweep duty 2 and
`docchat-ops ingestion fail-item` apply, are `docchat_core.ingestion.runs`'s.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final, Literal, Self

from pydantic import Field, model_validator

from docchat_core.errors import ITEM_CODES, ItemCodeKind
from docchat_core.ingestion.models import (
    MAX_ITEM_WARNINGS,
    ItemSettings,
    ItemStage,
    ItemState,
    ItemWarning,
    RunState,
    Traceparent,
)
from docchat_core.models import LowerCaseUuid
from docchat_ingestion.activities.support import WireModel

SKIP_CODES: Final = frozenset(
    {
        "already_served",
        "superseded_by_newer",
        "version_not_ready",
        "document_deleted",
        "target_retired",
        "target_repointed",
        "renditions_current",
    }
)
"""The codes an item that ran can be skipped with (section 6.13 item 1)."""

_FAILURE_KINDS: Final = frozenset({ItemCodeKind.FAILURE, ItemCodeKind.FAILURE_AFTER_RETRIES})

type OutcomeState = Literal["succeeded", "skipped", "failed"]


class ItemUsage(WireModel):
    """`usage {embeddingTokens, summaryInputTokens, summaryOutputTokens, docIntelPages}`: the sums
    over an item's activity calls (section 6.1 item 3)."""

    embedding_tokens: int = Field(default=0, ge=0)
    summary_input_tokens: int = Field(default=0, ge=0)
    summary_output_tokens: int = Field(default=0, ge=0)
    doc_intel_pages: int = Field(default=0, ge=0)

    def plus(
        self,
        *,
        embedding_tokens: int = 0,
        summary_input_tokens: int = 0,
        summary_output_tokens: int = 0,
        doc_intel_pages: int = 0,
    ) -> Self:
        return self.model_copy(
            update={
                "embedding_tokens": self.embedding_tokens + embedding_tokens,
                "summary_input_tokens": self.summary_input_tokens + summary_input_tokens,
                "summary_output_tokens": self.summary_output_tokens + summary_output_tokens,
                "doc_intel_pages": self.doc_intel_pages + doc_intel_pages,
            }
        )


class ItemOutcome(WireModel):
    """`record_item_outcome`'s input: `{itemId, claimNumber, state, code, stage, warnings,
    usage}` (section 6.11), with the rest of the item envelope when the caller has it, so the
    activity's span joins the run's trace (section 4.4 rule 3)."""

    item_id: LowerCaseUuid
    claim_number: int = Field(ge=1)
    run_id: LowerCaseUuid | None = None
    traceparent: Traceparent | None = None
    tracestate: str | None = Field(default=None, max_length=512)
    settings: ItemSettings | None = None
    state: OutcomeState
    code: str | None = None
    stage: ItemStage | None = None
    warnings: list[ItemWarning] = Field(default_factory=list)
    usage: ItemUsage = Field(default_factory=ItemUsage)

    @model_validator(mode="after")
    def _code_matches_state(self) -> Self:
        if self.state == "succeeded":
            if self.code is not None:
                raise ValueError("a succeeded item stores no code")
        elif self.state == "skipped":
            if self.code not in SKIP_CODES:
                raise ValueError("a skipped item names a skip code")
        else:
            entry = ITEM_CODES.get(self.code or "")
            if entry is None or entry.kind not in _FAILURE_KINDS:
                raise ValueError("a failed item names a failure code")
        return self

    @property
    def item_state(self) -> ItemState:
        return ItemState(self.state)

    def kept_warnings(self) -> list[ItemWarning]:
        """The first 50 warnings, in order (section 6.11)."""
        return self.warnings[:MAX_ITEM_WARNINGS]


def collect_warnings(groups: Iterable[Sequence[ItemWarning]]) -> list[ItemWarning]:
    """Every activity's warnings in call order; `record_item_outcome` keeps the first 50."""
    return [warning for group in groups for warning in group]


def run_end_state(*, succeeded: int, skipped: int, failed: int, cancelled: int) -> RunState:
    """Section 6.13 item 2.2: `failed` when at least one item failed and none succeeded or was
    skipped, `completed_with_errors` when some failed and some succeeded or were skipped,
    `completed` otherwise; cancelled items count with skipped ones, and a run with no items ends
    `completed`."""
    passed = succeeded + skipped + cancelled
    if failed and not passed:
        return RunState.FAILED
    if failed:
        return RunState.COMPLETED_WITH_ERRORS
    return RunState.COMPLETED
