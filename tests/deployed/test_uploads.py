"""Deployed upload, index and dispatcher rows (`84-testing.md` sections 5.3, 5.5 and 5.9): the
large-upload test T-064 (`smoke`, the upload half of SP-9), the index delete test T-177 and the
runtime scale monitoring test T-448 (`full`, `dev`).

Every document a test uploads beyond the seed data is deleted by that test (section 6.6). What
the tests can't observe through the API, they leave to the rows that can: no deployed-test
identity holds a data role on the search service or the data account, so the physical indexes and
blobs an index delete removes are read through its run's items.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import pytest

from docchat_core.clients.tls import TLS_CONTEXT
from docchat_testing.adminapi import (
    FINISHED_RUN_STATES,
    AdminApi,
    problem_code,
)
from docchat_testing.apiclient import DeployedApiClient, DeployedEnvironment
from docchat_testing.fixtures.session import run_on_private_loop
from docchat_testing.logquery import Rows, kql_datetime, query_logs
from docchat_testing.seed import (
    LARGE_UPLOAD_BYTES,
    SeedData,
    byte_unique,
    delete_documents,
    fixture_bytes,
    generated_pdf,
    upload_file,
)
from docchat_testing.tokens import DeployedTokens

pytestmark = pytest.mark.deployed

LARGE_UPLOAD_TIMEOUT_SECONDS: Final = 1800
RUN_LIMIT: Final = timedelta(minutes=20)
MEMORY_RISE_LIMIT_BYTES: Final = 32 * 1_048_576
METRICS_WAIT_SECONDS: Final = 15 * 60
METRICS_POLL_SECONDS: Final = 60.0
BASELINE_WINDOW: Final = timedelta(minutes=10)
WORKSPACE_VARIABLE: Final = "DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"
INGESTION_APP_VARIABLE: Final = "DOCCHAT_INGESTION_APP_NAME"
DELETE_PREFIX: Final = "automated-tests-delete-"
INDEX_WAIT_SECONDS: Final = 600
DOCUMENT_WAIT_SECONDS: Final = 600
IDLE_WAIT_SECONDS: Final = 480
DISPATCHER_START_LIMIT: Final = timedelta(minutes=2)
INGESTION_PROBE_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
HTTP_ACCEPTED: Final = 202
HTTP_CONFLICT: Final = 409
HTTP_FORBIDDEN: Final = 403


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- T-064 ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LargeUpload:
    """The 50 MB upload: when its content request started and ended, and its run."""

    started_at: datetime
    uploaded_at: datetime
    run: dict[str, Any]


@pytest.fixture(scope="module")
def large_upload(
    deployed_environment: DeployedEnvironment,
    deployed_tokens: DeployedTokens,
    seed_data: SeedData,
    run_label: str,
) -> LargeUpload:
    """Upload a generated PDF of exactly 52,428,800 bytes into `automated-tests` through
    platform authentication, submit it and wait for its run; the document is deleted once the
    run has ended."""
    alias = deployed_environment.roles["admin"]

    async def token() -> str:
        return await deployed_tokens.token(alias)

    async def upload() -> LargeUpload:
        name = f"large-{run_label}.pdf"
        file = await asyncio.to_thread(
            lambda: upload_file(
                name, generated_pdf(4, label=f"large-{run_label}", size_bytes=LARGE_UPLOAD_BYTES)
            )
        )
        async with DeployedApiClient(deployed_environment.api_base_url, token) as client:
            api = AdminApi(client)
            index_id, project_id = seed_data.index_id, seed_data.project_id
            reservation = await api.reserve(index_id, file, project_id)
            try:
                started_at = datetime.now(UTC)
                await api.upload_content(index_id, reservation.version_id, file.data)
                uploaded_at = datetime.now(UTC)
                run_id = (await api.submit(index_id, [reservation.version_id]))["runId"]
                run = await api.wait_for_run(str(run_id), within=RUN_LIMIT.total_seconds() + 60)
            finally:
                await delete_documents(
                    api, index_id, [reservation.document_id], within=DOCUMENT_WAIT_SECONDS
                )
        return LargeUpload(started_at=started_at, uploaded_at=uploaded_at, run=run)

    return run_on_private_loop(upload, timeout=LARGE_UPLOAD_TIMEOUT_SECONDS)


@pytest.mark.smoke
@pytest.mark.full
@pytest.mark.timeout(LARGE_UPLOAD_TIMEOUT_SECONDS)
def test_t064_a_50_mb_upload_streams_through_and_ingests_within_20_minutes(
    large_upload: LargeUpload,
) -> None:
    run = large_upload.run["run"]
    assert run["state"] == "completed"
    assert run["counts"]["succeeded"] == run["counts"]["total"] == 1
    assert utc(run["completedAt"]) - utc(run["createdAt"]) <= RUN_LIMIT


def api_site_name(environment: DeployedEnvironment) -> str:
    host = urlsplit(environment.api_base_url).hostname or ""
    return host.split(".", 1)[0]


def memory_query(site: str, upload: LargeUpload) -> str:
    """The API site's `MemoryWorkingSet` (diagnostic setting `AllMetrics` into `AzureMetrics`,
    `85-observability.md` section 2): the average over the 10 minutes before the upload and
    the highest reading while it streamed."""
    before = upload.started_at - BASELINE_WINDOW
    return f"""let site = "{site}";
let start = {kql_datetime(upload.started_at)}; let end = {kql_datetime(upload.uploaded_at)};
let readings = AzureMetrics
  | where Resource =~ site and MetricName == "MemoryWorkingSet";
let baseline = toscalar(readings
  | where TimeGenerated between ({kql_datetime(before)} .. start) | summarize avg(Average));
readings
| where TimeGenerated between (bin(start, 1m) .. end)
| summarize peak = max(Maximum), readings = count()
| extend baseline = baseline"""


@pytest.mark.smoke
@pytest.mark.full
@pytest.mark.timeout(LARGE_UPLOAD_TIMEOUT_SECONDS)
def test_t064_api_memory_rises_by_less_than_32_mb_during_the_upload(
    deployed_environment: DeployedEnvironment, large_upload: LargeUpload
) -> None:
    # id-docchat-citest reads the workspace through Log Analytics Reader on log-docchat-dev in
    # dev and at rg-docchat-load in load (83-infrastructure.md section 6.8).
    kql = memory_query(api_site_name(deployed_environment), large_upload)
    workspace = os.environ[WORKSPACE_VARIABLE]

    async def readings() -> Rows:
        # Platform metrics reach the workspace minutes after the fact.
        deadline = asyncio.get_running_loop().time() + METRICS_WAIT_SECONDS
        while True:
            rows = await query_logs(workspace, kql, "PT2H")
            done = bool(rows and rows[0]["readings"] and rows[0]["baseline"] is not None)
            if done or asyncio.get_running_loop().time() >= deadline:
                return rows
            await asyncio.sleep(METRICS_POLL_SECONDS)

    rows = run_on_private_loop(readings, timeout=METRICS_WAIT_SECONDS + 120)
    assert rows, "the MemoryWorkingSet query answered no row"
    assert rows[0]["readings"], "no MemoryWorkingSet reading covers the upload"
    rise = float(rows[0]["peak"]) - float(rows[0]["baseline"])
    assert rise < MEMORY_RISE_LIMIT_BYTES, f"API memory rose by {rise:.0f} bytes"


# --- T-177 ------------------------------------------------------------------------------------


async def remove_index(api: AdminApi, index_id: str) -> None:
    """Unmap the index from every project and delete it; an index still busy is left for the
    next run."""
    detail = await api.get_index(index_id)
    if detail is None or detail["status"] == "deleting":
        return
    for project in detail["mappedProjects"]:
        await api.unmap_index(str(project["projectId"]), index_id)
    detail = await api.get_index(index_id)
    if detail is None:
        return
    response = await api.delete_index(index_id, str(detail["rowVersion"]))
    if response.status_code == HTTP_ACCEPTED:
        await api.wait_for_run(str(response.json()["runId"]), within=INDEX_WAIT_SECONDS)


@pytest.mark.full
async def test_t177_index_delete_refuses_a_mapped_index_and_removes_an_unmapped_one(
    admin_api: AdminApi, seed_data: SeedData, run_label: str
) -> None:
    api = admin_api
    for leftover in await api.find_indexes(DELETE_PREFIX):
        await remove_index(api, str(leftover["indexId"]))
    started = await api.create_index(await api.index_body(f"{DELETE_PREFIX}{run_label}"))
    index_id = str(started["index"]["indexId"])
    try:
        await api.wait_for_run(str(started["runId"]), within=INDEX_WAIT_SECONDS)
        await api.ensure_mapping(seed_data.project_id, index_id, is_default=False)
        data = byte_unique(fixture_bytes("headings.docx"), "headings.docx", f"delete-{run_label}")
        reservation = await api.upload(
            index_id, upload_file("headings.docx", data), seed_data.project_id
        )
        run_id = (await api.submit(index_id, [reservation.version_id]))["runId"]
        await api.wait_for_run(str(run_id), within=DOCUMENT_WAIT_SECONDS)

        detail = await api.get_index(index_id)
        assert detail is not None
        refused = await api.delete_index(index_id, str(detail["rowVersion"]))
        assert refused.status_code == HTTP_CONFLICT
        assert problem_code(refused) == "index_mapped"

        await api.unmap_index(seed_data.project_id, index_id)
        detail = await api.get_index(index_id)
        assert detail is not None
        physical = [
            part
            for part in (detail["activePhysicalIndex"], detail["buildingPhysicalIndex"])
            if part
        ]
        response = await api.delete_index(index_id, str(detail["rowVersion"]))
        assert response.status_code == HTTP_ACCEPTED
        assert response.json()["index"]["status"] == "deleting"
        delete_run = await api.wait_for_run(
            str(response.json()["runId"]), within=INDEX_WAIT_SECONDS
        )
    finally:
        await remove_index(api, index_id)
    assert delete_run["run"]["state"] == "completed"
    operations = [item["operation"] for item in delete_run["items"]]
    assert operations.count("delete_physical_index") >= len(physical) >= 1
    assert operations.count("purge_index_blobs") == 1
    assert all(item["state"] == "succeeded" for item in delete_run["items"])
    assert await api.get_index(index_id) is None


# --- T-448 ------------------------------------------------------------------------------------


async def ingestion_host_refused(app_name: str) -> str:
    """How the ingestion app's default host answers a request from this runner on the
    internet: `403` from the platform, or no connection; anything else fails."""
    url = f"https://{app_name}.azurewebsites.net/"
    try:
        async with httpx.AsyncClient(
            timeout=INGESTION_PROBE_TIMEOUT, follow_redirects=False, verify=TLS_CONTEXT
        ) as client:
            response = await client.get(url)
    except httpx.TransportError as error:
        return f"no connection ({type(error).__name__})"
    assert response.status_code == HTTP_FORBIDDEN, f"{url} answered {response.status_code}"
    return "403"


@pytest.mark.full
async def test_t448_a_queue_message_starts_the_dispatcher_within_2_minutes(
    admin_api: AdminApi, seed_data: SeedData, run_label: str
) -> None:
    api = admin_api
    await ingestion_host_refused(os.environ[INGESTION_APP_VARIABLE])

    async def idle() -> bool | None:
        return True if not await api.open_runs() else None

    # Only an idle dispatcher shows that the message, not the work in hand, started it.
    await api.wait_until(idle, within=IDLE_WAIT_SECONDS, what="every run ending")
    data = byte_unique(fixture_bytes("tables.pdf"), "tables.pdf", f"docchat-scale-{run_label}")
    file = upload_file(f"scale-{run_label}.pdf", data)
    reservation = await api.upload(seed_data.index_id, file, seed_data.project_id)
    try:
        run_id = str((await api.submit(seed_data.index_id, [reservation.version_id]))["runId"])

        async def started() -> dict[str, Any] | None:
            run = (await api.get_run(run_id))["run"]
            return run if run["startedAt"] or run["state"] in FINISHED_RUN_STATES else None

        run = await api.wait_until(
            started, within=DISPATCHER_START_LIMIT.total_seconds() + 60, what="the run starting"
        )
        assert run["startedAt"], f"the run ended {run['state']} without starting"
        assert utc(run["startedAt"]) - utc(run["createdAt"]) <= DISPATCHER_START_LIMIT
        await api.wait_for_run(run_id, within=DOCUMENT_WAIT_SECONDS)
    finally:
        await delete_documents(
            api, seed_data.index_id, [reservation.document_id], within=DOCUMENT_WAIT_SECONDS
        )
