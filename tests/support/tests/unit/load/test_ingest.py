from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from docchat_testing.adminapi import AdminApi, Reservation, UploadFile
from docchat_testing.clock import FakeClock
from docchat_testing.load.common import LoadError
from docchat_testing.load.corpus import build_corpus
from docchat_testing.load.ingest import (
    POLL_SECONDS,
    PREFLIGHT_BATCH,
    UPLOADS_IN_FLIGHT,
    poll_runs,
    run_set,
)

BASE = "https://app-docchat-api-load-abc123.azurewebsites.net"


async def admin_token() -> str:
    return "tok-load-admin"


def add_project(api: Any) -> str:
    api.projects["p-load"] = {
        "projectId": "p-load",
        "name": "Load test",
        "isDefault": True,
        "status": "active",
        "rowVersion": "AAAAAAAAB9E=",
    }
    return "p-load"


@pytest.fixture
def corpus(tmp_path: Path) -> list[Path]:
    return build_corpus("a", 200, tmp_path / "corpus")


async def test_a_set_preflights_uploads_submits_two_halves_and_waits(
    load_api: Any, corpus: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = add_project(load_api)
    clock = FakeClock(datetime(2026, 11, 3, 10, 0, tzinfo=UTC))
    concurrency = {"now": 0, "most": 0}
    original = AdminApi.upload

    async def counted(self: AdminApi, index_id: str, file: UploadFile, project: str) -> Reservation:
        concurrency["now"] += 1
        concurrency["most"] = max(concurrency["most"], concurrency["now"])
        await asyncio.sleep(0)
        try:
            return await original(self, index_id, file, project)
        finally:
            concurrency["now"] -= 1

    monkeypatch.setattr(AdminApi, "upload", counted)
    out = tmp_path / "s4-set1"

    result = await run_set(
        BASE,
        admin_token,
        "load-ingest-a",
        corpus,
        out,
        transport=load_api.transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=clock.now,
    )

    [index] = load_api.indexes.values()
    assert index["displayName"] == "load-ingest-a"
    assert index["methodKey"] == "pagewise"
    assert index["methodParameters"] == {"pagesPerChunk": 1}
    assert load_api.mappings == {(project_id, index["indexId"]): False}
    assert load_api.preflight_batches == [PREFLIGHT_BATCH] * 4
    assert concurrency["most"] == UPLOADS_IN_FLIGHT == 8
    assert [len(ids) for ids in load_api.submissions] == [100, 100]
    names = [load_api.versions[i]["name"] for ids in load_api.submissions for i in ids]
    assert names == [path.name for path in corpus]
    assert [run["state"] for run in result["runs"]] == ["completed", "completed"]
    assert all(run["counts"]["succeeded"] == 100 for run in result["runs"])
    assert set(result["runs"][0]) == {"runId", "state", "createdAt", "completedAt", "counts"}
    assert json.loads((out / "ingest.json").read_text("utf-8")) == result
    assert result["setStart"] < result["setEnd"]


async def test_runs_are_polled_every_30_s_for_at_most_330_minutes() -> None:
    class Stuck:
        reads = 0

        async def get_run(self, run_id: str) -> dict[str, Any]:
            Stuck.reads += 1
            run = {"runId": run_id, "state": "running", "createdAt": "x", "counts": {}}
            return {"run": run, "items": []}

    clock = FakeClock()
    records = await poll_runs(
        Stuck(),  # type: ignore[arg-type]
        ["r1", "r2"],
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert [record["state"] for record in records] == ["running", "running"]
    assert clock.monotonic() - 1000.0 == 330 * 60
    assert Stuck.reads == 2 * (330 * 60 // int(POLL_SECONDS) + 1)


async def test_a_set_needs_the_seeded_project_and_a_new_index(
    load_api: Any, corpus: list[Path], tmp_path: Path
) -> None:
    clock = FakeClock()
    arguments = {
        "transport": load_api.transport,
        "sleep": clock.sleep,
        "monotonic": clock.monotonic,
        "now": clock.now,
    }
    with pytest.raises(LoadError, match="run docchat-load seed"):
        await run_set(BASE, admin_token, "load-ingest-a", corpus, tmp_path, **arguments)
    add_project(load_api)
    load_api.indexes["i0"] = {"indexId": "i0", "displayName": "load-ingest-a", "status": "ready"}
    with pytest.raises(LoadError, match="already exists"):
        await run_set(BASE, admin_token, "load-ingest-a", corpus, tmp_path, **arguments)


async def test_a_file_that_isnt_new_stops_the_set(
    load_api: Any, corpus: list[Path], tmp_path: Path
) -> None:
    add_project(load_api)
    original = load_api.handler

    def duplicate(request: Any) -> Any:
        response = original(request)
        if request.url.path.endswith("/uploads/preflight"):
            body = response.json()
            body["results"][3]["status"] = "duplicate"
            return httpx.Response(200, json=body)
        return response

    load_api.handler = duplicate
    clock = FakeClock(datetime(2026, 11, 3, tzinfo=UTC) + timedelta(hours=1))
    with pytest.raises(LoadError, match="duplicate"):
        await run_set(
            BASE,
            admin_token,
            "load-ingest-a",
            corpus,
            tmp_path,
            transport=load_api.transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            now=clock.now,
        )
    assert load_api.submissions == []
