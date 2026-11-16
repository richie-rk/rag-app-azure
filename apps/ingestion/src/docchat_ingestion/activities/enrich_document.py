"""`enrich_document`, the Enrich step (`14-ingestion.md` sections 6.4, 9.1 and 12; ADR 0051).

Enrich calls the plan's method's `enrich` (section 9.1 item 2). The recursive method, and the
page-wise method with `summarizeTables` false, are done at once with zero counts; otherwise the
page-wise method summarises every table of the plan's extract parts not yet in
`enrich/table-summaries.jsonl` (`docchat_ingestion.methods.pagewise.summaries`), saves the file
and returns `done: false` after `activityTimeBudgetSeconds` of work, so the orchestrator calls it
once more. A summary deployment that stays throttled or unavailable after the client's retries
saves progress and fails with the retryable `table_summary_unavailable` and the service's
`Retry-After`, which this step returns as its result.

The counts and tokens of the output are this call's; per-table warnings carry the table's page.
The method records `docchat.ingestion.tokens` and `docchat.ingestion.throttled` where it makes
its calls, so this step records neither.
"""

from __future__ import annotations

from typing import Final

from opentelemetry.trace import Span
from pydantic import Field

from docchat_core.ingestion.methods import EnrichResult as MethodEnrichResult
from docchat_core.ingestion.models import ActivityResult, ItemStage, ItemWarning
from docchat_ingestion.activities.documents import (
    DEADLINES,
    ActivityOutput,
    DocumentServices,
    PlanInput,
    ServicesProvider,
    blueprint,
    document_services,
    fence_and_read,
    item_ids,
    method_context,
    registered,
    run_document_activity,
)

NAME: Final = "enrich_document"
DEADLINE_SECONDS: Final = DEADLINES[NAME]


class EnrichResult(ActivityResult):
    """`{done, tables, summarised, filtered, discarded, inputTokens, outputTokens, warnings}` with
    the `ActivityResult` fields."""

    done: bool = False
    tables: int = Field(default=0, ge=0)
    summarised: int = Field(default=0, ge=0)
    filtered: int = Field(default=0, ge=0)
    discarded: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    warnings: list[ItemWarning] = Field(default_factory=list)

    @classmethod
    def of(cls, result: MethodEnrichResult) -> EnrichResult:
        return cls.success(
            done=result.done,
            tables=result.tables,
            summarised=result.summarised,
            filtered=result.filtered,
            discarded=result.discarded,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            warnings=list(result.warnings),
        )


async def enrich(services: DocumentServices, item: PlanInput, span: Span) -> EnrichResult:
    del span
    plan = item.plan
    facts = await fence_and_read(services, item, ItemStage.ENRICH, DEADLINE_SECONDS)
    implementation = registered(services, plan).implementation
    result = await implementation.enrich(
        method_context(item, facts), plan.method.validated_parameters()
    )
    return EnrichResult.of(result)


def _failure(code: str, retry_after: int | None) -> EnrichResult:
    return EnrichResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        PlanInput,
        enrich,
        stage=ItemStage.ENRICH,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def enrich_document(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(enrich_document)
