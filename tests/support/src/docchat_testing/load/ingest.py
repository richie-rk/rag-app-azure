"""`docchat-load ingest`, the S4 driver (`84-testing.md` sections 7.1 and 7.5; T-594).

`docchat-load ingest --env load --variant <a|b> --index load-ingest-<a|b> --corpus <dir>
--out load-results/s4-set<n>/` runs one ingestion set on the job's own runner:

1. reads the `load-admin` password from the shared test vault inside the job's read window and
   keeps it in memory, re-acquiring the admin token within 5 minutes of its expiry;
2. creates the index with the page-wise method at its default parameters, waits until it is
   `ready` and maps it to the project `Load test`;
3. preflights the corpus in batches of 50, then reserves and uploads it with 8 requests in flight
   through `DeployedApiClient`;
4. submits two `adminSubmitUploads` calls of 100 versions each within 5 s;
5. polls `adminGetIngestionRun` for both runs every 30 s until both are terminal or 330 minutes
   pass;
6. writes `ingest.json`: `{runs: [{runId, state, createdAt, completedAt, counts}], setStart,
   setEnd}`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from docchat_testing.adminapi import FINISHED_RUN_STATES, AdminApi, UploadFile
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.load.common import (
    PROJECT_NAME,
    LoadError,
    iso,
    utc_now,
    write_json,
)
from docchat_testing.seed import INDEX_METHOD, upload_file

PREFLIGHT_BATCH: Final = 50
UPLOADS_IN_FLIGHT: Final = 8
SUBMIT_SIZE: Final = 100
POLL_SECONDS: Final = 30.0
RUN_LIMIT_SECONDS: Final = 330 * 60.0
INDEX_READY_SECONDS: Final = 1800.0
RESULT_FILE: Final = "ingest.json"

type TokenSource = Callable[[], Awaitable[str]]


def read_corpus(paths: Sequence[Path]) -> list[UploadFile]:
    return [upload_file(path.name, path.read_bytes()) for path in paths]


async def prepare_index(api: AdminApi, index_name: str) -> tuple[str, str]:
    """The new index at the page-wise defaults, `ready` and mapped to `Load test`."""
    project = await api.find_project(PROJECT_NAME)
    if project is None:
        raise LoadError(
            "project_missing", f"project {PROJECT_NAME} is missing: run docchat-load seed"
        )
    if await api.find_index(index_name) is not None:
        raise LoadError("index_exists", f"index {index_name} already exists in this run")
    started = await api.create_index(await api.index_body(index_name, method_key=INDEX_METHOD))
    index_id = str(started["index"]["indexId"])
    await api.wait_for_index(index_id, {"ready"}, within=INDEX_READY_SECONDS)
    project_id = str(project["projectId"])
    await api.ensure_mapping(project_id, index_id, is_default=False)
    return project_id, index_id


async def upload_corpus(
    api: AdminApi, index_id: str, project_id: str, files: Sequence[UploadFile]
) -> list[str]:
    """Preflight in batches of 50, then reserve and upload with 8 in flight; the version IDs in
    corpus order."""
    for offset in range(0, len(files), PREFLIGHT_BATCH):
        batch = files[offset : offset + PREFLIGHT_BATCH]
        results = await api.preflight(index_id, batch)
        refused = [
            f"{file.name} ({result['status']})"
            for file, result in zip(batch, results, strict=True)
            if result["status"] != "new"
        ]
        if refused:
            raise LoadError("preflight_refused", f"not new: {', '.join(refused[:5])}")
    # Eight workers take the next file in turn, so no more than eight tasks ever exist.
    pending = iter(enumerate(files))
    version_ids: list[str] = [""] * len(files)

    async def worker() -> None:
        for position, file in pending:
            reservation = await api.upload(index_id, file, project_id)
            version_ids[position] = reservation.version_id

    await asyncio.gather(*(worker() for _ in range(min(UPLOADS_IN_FLIGHT, len(files)))))
    return version_ids


async def submit_halves(api: AdminApi, index_id: str, version_ids: Sequence[str]) -> list[str]:
    """Two submissions of 100 versions, one straight after the other (well within 5 s)."""
    run_ids: list[str] = []
    for offset in range(0, len(version_ids), SUBMIT_SIZE):
        result = await api.submit(index_id, version_ids[offset : offset + SUBMIT_SIZE])
        if not result.get("runId"):
            raise LoadError("submit_failed", "a submission queued no run")
        run_ids.append(str(result["runId"]))
    return run_ids


def run_record(detail: dict[str, Any]) -> dict[str, Any]:
    run = detail["run"]
    return {
        "runId": run["runId"],
        "state": run["state"],
        "createdAt": run["createdAt"],
        "completedAt": run.get("completedAt"),
        "counts": run["counts"],
    }


async def poll_runs(
    api: AdminApi,
    run_ids: Sequence[str],
    *,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> list[dict[str, Any]]:
    """Every 30 s until both runs are terminal or 330 minutes pass; their last records."""
    deadline = monotonic() + RUN_LIMIT_SECONDS
    while True:
        records = [run_record(await api.get_run(run_id)) for run_id in run_ids]
        if all(record["state"] in FINISHED_RUN_STATES for record in records):
            return records
        if monotonic() >= deadline:
            return records
        await sleep(POLL_SECONDS)


async def run_set(
    api_base_url: str,
    token: TokenSource,
    index_name: str,
    corpus: Sequence[Path],
    out: Path,
    *,
    transport: Any = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """One set; returns what it wrote to `ingest.json`."""
    files = read_corpus(corpus)
    async with DeployedApiClient(api_base_url, token, transport=transport) as client:
        api = AdminApi(client, sleep=sleep, monotonic=monotonic, poll_seconds=POLL_SECONDS)
        project_id, index_id = await prepare_index(api, index_name)
        set_start = now()
        version_ids = await upload_corpus(api, index_id, project_id, files)
        run_ids = await submit_halves(api, index_id, version_ids)
        runs = await poll_runs(api, run_ids, sleep=sleep, monotonic=monotonic)
        set_end = now()
    result = {"runs": runs, "setStart": iso(set_start), "setEnd": iso(set_end), "index": index_name}
    write_json(out / RESULT_FILE, result)
    return result
