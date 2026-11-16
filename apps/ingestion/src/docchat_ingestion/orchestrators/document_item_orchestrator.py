"""`document_item_orchestrator`, one claimed item of operation `ingest`, `cleanup_document` or
`rerender` (`14-ingestion.md` sections 6.1, 4.3, 4.7 and 4.8; ADRs 0034, 0050 and 0144).

Its instance ID is `docchat-item-{itemId}-{claimNumber}` and its input `ItemInput`, both from the
dispatcher. Every activity goes through `call_with_retry`, so a retryable failure waits on a
durable timer and holds no worker; Durable Functions checkpoints after each activity, so a
failure or a crash resumes at the step that failed and no completed step runs again.

`ingest`: Validate (a skip records the item `skipped`), Extract part by part (the first call of a
DOCX or PPTX version converts), Enrich and Embed until `done` (at most 20 calls each), Chunk,
Index, Render, the Flip (`requeued` ends with no outcome), Clean up after the Flip (3 attempts,
then the warning `cleanup_incomplete`) and the outcome: `succeeded`, or `skipped` with the Flip's
code. A failure records `failed` with its code and stage, then cleans up in discard mode (2
attempts; sweep duty 6 finishes the rest). `cleanup_document`: `purge_document_artifacts` until
`done`. `rerender`: `rerender_pages`, with no Validate and no clean-up.

An activity that returns `lease_lost` ends the sub-orchestration at once with no outcome: the
item belongs to a later claim or to the sweep. The orchestrator replays, so it does no I/O,
reads time only through the context, generates no ID, creates no span, writes no log record and
carries only IDs, codes, counts and blob paths in its inputs.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import azure.durable_functions as df
from pydantic import BaseModel, ValidationError

from docchat_core.ingestion.models import (
    DOCUMENT_OPERATIONS,
    LEASE_LOST,
    ItemInput,
    ItemOperation,
    ItemStage,
    ItemWarning,
)
from docchat_ingestion.activities.documents import (
    MAX_RESUMABLE_CALLS,
    BuildPlan,
    PartRange,
)
from docchat_ingestion.activities.flip_chunk_set import FlipResult
from docchat_ingestion.activities.validate_item import ValidateResult
from docchat_ingestion.orchestrators.retry import (
    ActivityOutput,
    OrchestrationContext,
    call_with_retry,
)
from docchat_ingestion.outcomes import ItemOutcome, ItemUsage, OutcomeState

NAME: Final = "document_item_orchestrator"
CLEANUP_AFTER_FLIP_ATTEMPTS: Final = 3
CLEANUP_DISCARD_ATTEMPTS: Final = 2
ACTIVITY_TIMEOUT: Final = "activity_timeout"
INTERNAL_ERROR: Final = "internal_error"
CLEANUP_INCOMPLETE: Final = "cleanup_incomplete"

STAGES: Final[Mapping[str, ItemStage]] = {
    "validate_item": ItemStage.VALIDATE,
    "extract_document": ItemStage.EXTRACT,
    "enrich_document": ItemStage.ENRICH,
    "chunk_document": ItemStage.CHUNK,
    "embed_chunks": ItemStage.EMBED,
    "index_chunks": ItemStage.INDEX,
    "render_pages": ItemStage.RENDER,
    "rerender_pages": ItemStage.RENDER,
    "flip_chunk_set": ItemStage.FLIP,
    "clean_up_item": ItemStage.CLEAN_UP,
    "purge_document_artifacts": ItemStage.PURGE_DOCUMENT,
}
"""The stage a failing activity records (section 4.8 rule 2)."""


class DocumentOrchestrationError(Exception):
    """The input isn't a document operation's `ItemInput`, or an activity answered outside its
    contract; the constant message keeps every value out of Durable history."""

    def __init__(self) -> None:
        super().__init__("document item orchestration invalid")


class ItemFailed(Exception):  # noqa: N818 - control flow of the failure path, never raised out
    """A step failed for good: its code and stage go to `record_item_outcome`."""

    def __init__(self, code: str, stage: ItemStage) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage


class ClaimLost(Exception):  # noqa: N818 - control flow, never raised out
    """An activity returned `lease_lost`."""


@dataclass(slots=True)
class Progress:
    """What the orchestrator gathers from recorded activity outputs: usage sums and warnings."""

    usage: ItemUsage = field(default_factory=ItemUsage)
    warnings: list[ItemWarning] = field(default_factory=list)

    def take_warnings(self, output: ActivityOutput) -> None:
        for warning in output.get("warnings") or ():
            self.warnings.append(ItemWarning.model_validate(warning, by_alias=True, by_name=False))


def item_input(data: object) -> ItemInput:
    try:
        item = ItemInput.model_validate(data, by_alias=True, by_name=False)
    except ValidationError:
        raise DocumentOrchestrationError from None
    if item.operation not in DOCUMENT_OPERATIONS:
        raise DocumentOrchestrationError
    return item


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _int(output: ActivityOutput, key: str) -> int:
    value = output.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(slots=True)
class Run:
    """One sub-orchestration's state, rebuilt on every replay from recorded outputs."""

    context: OrchestrationContext
    item: ItemInput
    progress: Progress = field(default_factory=Progress)

    @property
    def envelope(self) -> dict[str, Any]:
        return _dump(self.item.envelope())

    def call(
        self, name: str, extra: Mapping[str, Any] | None = None, *, attempts: int | None = None
    ) -> Generator[Any, Any, ActivityOutput]:
        """One activity through `call_with_retry`; a failure that is final raises `ItemFailed`,
        `lease_lost` raises `ClaimLost`."""
        output = yield from self.attempt(name, extra, attempts=attempts)
        if output.get("ok") is not True:
            code = output.get("errorCode")
            raise ItemFailed(code if isinstance(code, str) else INTERNAL_ERROR, STAGES[name])
        return output

    def attempt(
        self, name: str, extra: Mapping[str, Any] | None = None, *, attempts: int | None = None
    ) -> Generator[Any, Any, ActivityOutput]:
        """One activity through `call_with_retry`, its failure returned; only `lease_lost`
        raises (`ClaimLost`)."""
        retry = self.item.retry
        if attempts is not None:
            retry = retry.model_copy(update={"max_attempts": attempts})
        payload = {**self.envelope, **(extra or {})}
        output: ActivityOutput = yield from call_with_retry(self.context, name, payload, retry)
        if output.get("errorCode") == LEASE_LOST:
            raise ClaimLost
        return output

    def resumable(
        self, name: str, extra: Mapping[str, Any]
    ) -> Generator[Any, Any, list[ActivityOutput]]:
        """Call a resumable activity until `done`, at most 20 times; a 21st call isn't made and
        the item fails with `activity_timeout` (section 4.6)."""
        outputs: list[ActivityOutput] = []
        for _ in range(MAX_RESUMABLE_CALLS):
            output = yield from self.call(name, extra)
            outputs.append(output)
            if output.get("done") is True:
                return outputs
        raise ItemFailed(ACTIVITY_TIMEOUT, STAGES[name])

    def outcome(
        self, state: OutcomeState, code: str | None = None, stage: ItemStage | None = None
    ) -> Generator[Any, Any]:
        """`record_item_outcome` with the item envelope, the usage and the warnings."""
        envelope = self.item.envelope()
        payload = ItemOutcome(
            item_id=envelope.item_id,
            claim_number=envelope.claim_number,
            run_id=envelope.run_id,
            traceparent=envelope.traceparent,
            tracestate=envelope.tracestate,
            settings=envelope.settings,
            state=state,
            code=code,
            stage=stage,
            warnings=self.progress.warnings,
            usage=self.progress.usage,
        )
        yield from call_with_retry(
            self.context, "record_item_outcome", _dump(payload), self.item.retry
        )


# --- Operation `ingest` ---------------------------------------------------------------------------


def ingest(run: Run) -> Generator[Any, Any]:
    progress = run.progress
    plan: BuildPlan | None = None
    try:
        validated = yield from run.call("validate_item")
        result = ValidateResult.model_validate(validated, by_alias=True, by_name=False)
        if result.action == "skip":
            yield from run.outcome("skipped", result.skip_code)
            return
        plan = BuildPlan.model_validate(result.plan_fields(), by_alias=True, by_name=False)
        with_plan = {"plan": _dump(plan)}
        part: dict[str, Any] | None = None
        while True:
            extracted = yield from run.call("extract_document", {**with_plan, "part": part})
            progress.usage = progress.usage.plus(doc_intel_pages=_int(extracted, "docIntelPages"))
            progress.take_warnings(extracted)
            next_part = extracted.get("nextPart")
            if next_part is None:
                break
            part = _dump(PartRange.model_validate(next_part, by_alias=True, by_name=False))
        for enriched in (yield from run.resumable("enrich_document", with_plan)):
            progress.usage = progress.usage.plus(
                summary_input_tokens=_int(enriched, "inputTokens"),
                summary_output_tokens=_int(enriched, "outputTokens"),
            )
            progress.take_warnings(enriched)
        yield from run.call("chunk_document", with_plan)
        for embedded in (yield from run.resumable("embed_chunks", with_plan)):
            progress.usage = progress.usage.plus(embedding_tokens=_int(embedded, "embeddingTokens"))
        yield from run.call("index_chunks", with_plan)
        yield from run.call("render_pages", with_plan)
        flipped = yield from run.call("flip_chunk_set", with_plan)
    except ClaimLost:
        return
    except ItemFailed as failure:
        yield from fail(run, failure, plan)
        return
    flip = FlipResult.model_validate(flipped, by_alias=True, by_name=False)
    if flip.outcome == "requeued":
        # The item is queued again and builds into its new target from Validate.
        return
    try:
        cleaned = yield from run.attempt(
            "clean_up_item",
            {**with_plan, "flip": flipped, "mode": "flipped"},
            attempts=CLEANUP_AFTER_FLIP_ATTEMPTS,
        )
    except ClaimLost:
        return
    if cleaned.get("ok") is not True:
        progress.warnings.append(ItemWarning(code=CLEANUP_INCOMPLETE))
    if flip.outcome == "skipped":
        yield from run.outcome("skipped", flip.code)
    else:
        yield from run.outcome("succeeded")


def fail(run: Run, failure: ItemFailed, plan: BuildPlan | None) -> Generator[Any, Any]:
    """The failure path: the outcome first, then the discard clean-up of the build, when there is
    one (section 6.1 item 1)."""
    yield from run.outcome("failed", failure.code, failure.stage)
    if plan is None:
        return
    try:
        yield from run.attempt(
            "clean_up_item",
            {"plan": _dump(plan), "flip": None, "mode": "discard"},
            attempts=CLEANUP_DISCARD_ATTEMPTS,
        )
    except ClaimLost:
        return


# --- Operations `cleanup_document` and `rerender` -------------------------------------------------


def cleanup_document(run: Run) -> Generator[Any, Any]:
    try:
        yield from run.resumable("purge_document_artifacts", {})
    except ClaimLost:
        return
    except ItemFailed as failure:
        yield from run.outcome("failed", failure.code, failure.stage)
        return
    yield from run.outcome("succeeded")


def rerender(run: Run) -> Generator[Any, Any]:
    try:
        output = yield from run.call("rerender_pages")
    except ClaimLost:
        return
    except ItemFailed as failure:
        yield from run.outcome("failed", failure.code, ItemStage.RENDER)
        return
    if output.get("outcome") == "skipped":
        code = output.get("code")
        if not isinstance(code, str):
            raise DocumentOrchestrationError
        yield from run.outcome("skipped", code)
    else:
        yield from run.outcome("succeeded")


def document_item_orchestrator(context: OrchestrationContext) -> Generator[Any, Any]:
    item = item_input(context.get_input())
    run = Run(context, item)
    if item.operation is ItemOperation.INGEST:
        yield from ingest(run)
    elif item.operation is ItemOperation.CLEANUP_DOCUMENT:
        yield from cleanup_document(run)
    else:
        yield from rerender(run)


bp: Final = df.Blueprint()
bp.orchestration_trigger(context_name="context")(document_item_orchestrator)
