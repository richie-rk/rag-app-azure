"""The start of the deployed crash-resume test T-587 (`84-testing.md` section 6.7 step 1), marker
`resume`: the `resume-start` job of `deployed-tests.yaml` runs it as `admin` alone.

It empties the index `automated-tests-resume`, submits a generated 120-page PDF alone (run 1) and
then the four fixture documents made byte-unique together (run 2), so at `dev`'s parallelism of 1
run 2 waits behind run 1. Once run 1's large item has reached Embed it writes `reports/resume.json`
(`{runIds, largeItemId, embedSeenAt}`); the job publishes the IDs, and `resume-restart` restarts
the ingestion app and runs `docchat_testing.resume_check` against them (step 2).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Final

import pytest

from docchat_testing.adminapi import (
    FINISHED_ITEM_STATES,
    FINISHED_RUN_STATES,
    AdminApi,
    stage_reached,
)
from docchat_testing.seed import (
    FIXTURE_DOCUMENTS,
    RESUME_INDEX_NAME,
    byte_unique,
    ensure_mapped_index,
    fixture_bytes,
    generated_pdf,
    remove_all_documents,
    upload_file,
)
from docchat_testing.tokens import REPOSITORY_ROOT

pytestmark = [pytest.mark.deployed, pytest.mark.resume]

START_TIMEOUT_SECONDS: Final = 3600
LARGE_PAGES: Final = 120
EMBED_WAIT_SECONDS: Final = 45 * 60
DELETE_WAIT_SECONDS: Final = 15 * 60
REPORT: Final = REPOSITORY_ROOT / "reports" / "resume.json"


def large_item(detail: dict[str, Any]) -> dict[str, Any]:
    """Run 1's one `ingest` item."""
    items = [item for item in detail["items"] if item["operation"] == "ingest"]
    assert len(items) == 1, f"run 1 holds {len(items)} ingest items"
    item: dict[str, Any] = items[0]
    return item


async def wait_for_embed(api: AdminApi, run_id: str) -> tuple[str, datetime]:
    """Poll run 1 every 15 s until its large item's stage is `embed` or later; the item ID and
    the time that was seen. An item that fails, or a run that ends first, fails the test."""

    async def reached() -> tuple[str, datetime] | None:
        detail = await api.get_run(run_id)
        item = large_item(detail)
        if item["state"] == "failed":
            pytest.fail(f"the large item failed ({item['outcomeCode']})", pytrace=False)
        if item["state"] in FINISHED_ITEM_STATES or detail["run"]["state"] in FINISHED_RUN_STATES:
            pytest.fail("run 1 ended before its large item was seen at embed", pytrace=False)
        if stage_reached(item["stage"], "embed"):
            return str(item["itemId"]), datetime.now(UTC)
        return None

    return await api.wait_until(
        reached, within=EMBED_WAIT_SECONDS, what="the large item reaching embed"
    )


def write_report(run_ids: list[str], item_id: str, seen_at: datetime) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "runIds": run_ids,
        "largeItemId": item_id,
        "embedSeenAt": seen_at.isoformat().replace("+00:00", "Z"),
    }
    REPORT.write_text(json.dumps(report) + "\n", encoding="utf-8")


@pytest.mark.timeout(START_TIMEOUT_SECONDS)
async def test_t587_start_the_runs_the_restart_interrupts(
    admin_api: AdminApi, run_label: str
) -> None:
    api = admin_api
    index = await ensure_mapped_index(api, RESUME_INDEX_NAME)
    await remove_all_documents(api, index.index_id, within=DELETE_WAIT_SECONDS)

    name = f"resume-{run_label}-large.pdf"
    large = await asyncio.to_thread(
        lambda: upload_file(name, generated_pdf(LARGE_PAGES, label=f"resume-{run_label}-large"))
    )
    reservation = await api.upload(index.index_id, large, index.project_id)
    run_1 = (await api.submit(index.index_id, [reservation.version_id]))["runId"]
    assert run_1, "the large PDF wasn't queued"

    versions: list[str] = []
    for number, fixture in enumerate(FIXTURE_DOCUMENTS, start=1):
        data = byte_unique(fixture_bytes(fixture), fixture, f"docchat-resume-{run_label}-{number}")
        uploaded = await api.upload(index.index_id, upload_file(fixture, data), index.project_id)
        versions.append(uploaded.version_id)
    run_2 = (await api.submit(index.index_id, versions))["runId"]
    assert run_2, "the fixture documents weren't queued"

    item_id, seen_at = await wait_for_embed(api, str(run_1))
    write_report([str(run_1), str(run_2)], item_id, seen_at)
