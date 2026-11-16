"""`purge_index_blobs` (`14-ingestion.md` section 8.12 item 3): both prefixes emptied in batches of
at most 256, `done: false` when the time budget is spent, the versions marked purged once both
prefixes are empty, and storage failures retryable."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from azure.core.exceptions import ServiceRequestError

from docchat_ingestion.activities import purge_index_blobs as activity

ITEM = UUID("7d3e4f50-6172-4c83-ad94-b5c6d7e8f9a0")
INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
OTHER = UUID("4a3d1e0f-9c8b-42d7-b6e5-f4a3d2c1b0a9")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")


def _envelope(budget: int = 1200) -> dict[str, Any]:
    return {
        "itemId": str(ITEM),
        "claimNumber": 1,
        "runId": str(RUN),
        "settings": {
            "activityTimeBudgetSeconds": budget,
            "maxPagesPerDocument": 2000,
            "pagewisePagesPerExtract": 40,
            "maxParseSecondsPerDocument": 7200,
            "embeddingBatchMaxInputs": 64,
            "embeddingBatchMaxTokens": 60000,
            "embeddingConcurrency": 2,
            "indexBatchMaxDocuments": 100,
            "indexBatchMaxBytes": 8000000,
            "tableSummaryConcurrency": 4,
            "tableSummaryMaxInputTokens": 6000,
            "tableSummaryMaxOutputTokens": 300,
        },
    }


def _provider(process: Any) -> Any:
    async def provide() -> Any:
        return process

    return provide


def _sql(fakes: SimpleNamespace, *after: Any) -> Any:
    return fakes.FakeSql(fakes.Rows(rowcount=1), fakes.Rows([INDEX]), *after)


async def test_both_prefixes_are_emptied_and_the_versions_marked(fakes: SimpleNamespace) -> None:
    documents = fakes.FakeContainer(
        *(f"{INDEX}/{n:04d}/1/source" for n in range(300)), f"{OTHER}/a/1/source"
    )
    renditions = fakes.FakeContainer(f"{INDEX}/d/1/pages/1.pdf", f"{OTHER}/d/1/pages/1.pdf")
    sql = _sql(fakes, fakes.Rows(rowcount=2))

    result = await activity.run(
        _envelope(), _provider(fakes.process(sql=sql, documents=documents, renditions=renditions))
    )

    assert result["ok"] is True
    assert (result["done"], result["deletedBlobs"]) == (True, 301)
    assert list(documents.blobs) == [f"{OTHER}/a/1/source"]
    assert list(renditions.blobs) == [f"{OTHER}/d/1/pages/1.pdf"]
    fence = sql.connection.executed[0]
    assert fence.params["stage"] == "purge_blobs"
    assert fence.params["dateadd_1"] == 2160
    assert "SET storage_purged_at=SYSUTCDATETIME()" in sql.connection.executed[-1].sql


async def test_a_spent_budget_returns_not_done(fakes: SimpleNamespace) -> None:
    documents = fakes.FakeContainer(*(f"{INDEX}/{n:04d}/1/source" for n in range(300)))

    class SlowClock:
        def __init__(self) -> None:
            self.ticks = 0.0

        def monotonic(self) -> float:
            self.ticks += 100
            return self.ticks

    process = fakes.process(sql=_sql(fakes), documents=documents, clock=SlowClock())

    result = await activity.run(_envelope(budget=60), _provider(process))

    assert (result["ok"], result["done"], result["deletedBlobs"]) == (True, False, 256)
    assert len(documents.blobs) == 44


async def test_a_refused_delete_is_storage_unavailable(fakes: SimpleNamespace) -> None:
    blob = f"{INDEX}/a/1/source"
    documents = fakes.FakeContainer(blob)
    documents.delete_status[blob] = 403

    result = await activity.run(
        _envelope(), _provider(fakes.process(sql=_sql(fakes), documents=documents))
    )

    assert (result["errorCode"], result["retryable"]) == ("storage_unavailable", True)


async def test_an_unreachable_account_is_storage_unavailable(fakes: SimpleNamespace) -> None:
    documents = fakes.FakeContainer(f"{INDEX}/a/1/source")

    async def broken(*names: str, **kwargs: Any) -> Any:
        raise ServiceRequestError("unreachable")

    documents.delete_blobs = broken

    result = await activity.run(
        _envelope(), _provider(fakes.process(sql=_sql(fakes), documents=documents))
    )

    assert result["errorCode"] == "storage_unavailable"


async def test_a_lost_claim_lists_nothing(fakes: SimpleNamespace) -> None:
    documents = fakes.FakeContainer(f"{INDEX}/a/1/source")

    result = await activity.run(
        _envelope(),
        _provider(fakes.process(sql=fakes.FakeSql(fakes.Rows(rowcount=0)), documents=documents)),
    )

    assert result["errorCode"] == "lease_lost"
    assert documents.deleted == []
