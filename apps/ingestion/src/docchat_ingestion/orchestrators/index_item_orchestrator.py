"""`index_item_orchestrator`, one claimed index operation (`14-ingestion.md` sections 8.12, 6.1 and
4.7; ADRs 0045 and 0052).

Operations `create_physical_index`, `delete_physical_index` and `purge_index_blobs`: one activity
through `call_with_retry` (the purge repeated until `done`, at most 20 calls, a 21st call being the
item's `activity_timeout`), then `record_item_outcome`, `succeeded` or `failed` with the activity's
code and stage. An activity that returns `lease_lost` ends the sub-orchestration at once with no
outcome, because the item belongs to a later claim or to the sweep. The instance ID
(`docchat-item-{itemId}-{claimNumber}`) and the input (`ItemInput`) come from the dispatcher.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from typing import Any, Final

import azure.durable_functions as df
from pydantic import ValidationError

from docchat_core.ingestion.models import LEASE_LOST, ItemInput, ItemOperation, ItemStage, ItemState
from docchat_ingestion.orchestrators.retry import (
    ActivityOutput,
    OrchestrationContext,
    call_with_retry,
)

NAME: Final = "index_item_orchestrator"
RECORD_OUTCOME: Final = "record_item_outcome"
MAX_RESUMABLE_CALLS: Final = 20
ACTIVITY_TIMEOUT: Final = "activity_timeout"

ACTIVITIES: Final[Mapping[ItemOperation, tuple[str, ItemStage]]] = {
    ItemOperation.CREATE_PHYSICAL_INDEX: ("create_search_index", ItemStage.CREATE_INDEX),
    ItemOperation.DELETE_PHYSICAL_INDEX: ("delete_search_index", ItemStage.DELETE_INDEX),
    ItemOperation.PURGE_INDEX_BLOBS: ("purge_index_blobs", ItemStage.PURGE_BLOBS),
}

NO_USAGE: Final[Mapping[str, int]] = {
    "embeddingTokens": 0,
    "summaryInputTokens": 0,
    "summaryOutputTokens": 0,
    "docIntelPages": 0,
}
"""Index operations call no model and no layout service."""


class IndexOrchestrationError(Exception):
    """The input isn't an index operation's `ItemInput`; the constant message keeps every value
    out of Durable history."""

    def __init__(self) -> None:
        super().__init__("index item input invalid")


def item_input(data: object) -> ItemInput:
    try:
        item = ItemInput.model_validate(data, by_alias=True, by_name=False)
    except ValidationError:
        raise IndexOrchestrationError from None
    if item.operation not in ACTIVITIES:
        raise IndexOrchestrationError
    return item


def outcome_input(
    item: ItemInput, state: ItemState, code: str | None, stage: ItemStage | None
) -> dict[str, Any]:
    """`record_item_outcome`'s input (section 6.11)."""
    return {
        "itemId": str(item.item_id),
        "claimNumber": item.claim_number,
        "state": state.value,
        "code": code,
        "stage": None if stage is None else stage.value,
        "warnings": [],
        "usage": dict(NO_USAGE),
    }


def index_item_orchestrator(context: OrchestrationContext) -> Generator[Any, Any]:
    item = item_input(context.get_input())
    activity, stage = ACTIVITIES[item.operation]
    envelope = item.envelope().model_dump(mode="json")
    result: ActivityOutput = yield from call_with_retry(context, activity, envelope, item.retry)
    calls = 1
    if item.operation is ItemOperation.PURGE_INDEX_BLOBS:
        while result.get("ok") is True and result.get("done") is not True:
            if calls >= MAX_RESUMABLE_CALLS:
                result = {"ok": False, "errorCode": ACTIVITY_TIMEOUT}
                break
            result = yield from call_with_retry(context, activity, envelope, item.retry)
            calls += 1
    if result.get("errorCode") == LEASE_LOST:
        return
    if result.get("ok") is True:
        outcome = outcome_input(item, ItemState.SUCCEEDED, None, None)
    else:
        code = result.get("errorCode")
        outcome = outcome_input(
            item, ItemState.FAILED, code if isinstance(code, str) else "internal_error", stage
        )
    yield from call_with_retry(context, RECORD_OUTCOME, outcome, item.retry)


bp: Final = df.Blueprint()
bp.orchestration_trigger(context_name="context")(index_item_orchestrator)
