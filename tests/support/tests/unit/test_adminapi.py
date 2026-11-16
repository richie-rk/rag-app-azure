from __future__ import annotations

import json

import httpx
import pytest
import respx

from docchat_testing.adminapi import (
    AdminApi,
    AdminApiError,
    UnusableResourceError,
    UploadBody,
    UploadFile,
    WaitTimeoutError,
    if_match,
    problem_code,
    stage_reached,
)
from docchat_testing.apiclient import DeployedApiClient

BASE = "https://app-docchat-api-dev.example"
PROJECT = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
INDEX = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e"
USER = "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f"
DOCUMENT = "4d5e6f7a-8b9c-4d0e-9f2a-3b4c5d6e7f8a"
RUN = "5e6f7a8b-9c0d-4e1f-8a3b-4c5d6e7f8a9b"
VERSION = "6f7a8b9c-0d1e-4f2a-9b4c-5d6e7f8a9b0c"


def problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"status": status, "code": code})


async def bearer() -> str:
    return "token"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def admin(clock: Clock | None = None) -> AdminApi:
    clock = clock or Clock()
    return AdminApi(DeployedApiClient(BASE, bearer), sleep=clock.sleep, monotonic=clock.monotonic)


def test_helpers() -> None:
    assert if_match("AAAAAAAAB9E=") == {"If-Match": '"AAAAAAAAB9E="'}
    assert problem_code(problem(409, "index_mapped")) == "index_mapped"
    assert problem_code(httpx.Response(401)) is None
    assert problem_code(httpx.Response(403, json=["x"])) is None
    assert stage_reached("embed", "embed")
    assert stage_reached("flip", "embed")
    assert not stage_reached("chunk", "embed")
    assert not stage_reached(None, "embed")
    assert not stage_reached("create_index", "embed")


async def test_upload_body_starts_afresh_on_each_iteration() -> None:
    body = UploadBody(b"abcdefghij", piece=4)
    assert len(body) == 10
    first = [piece async for piece in body]
    second = [piece async for piece in body]
    assert first == second == [b"abcd", b"efgh", b"ij"]


@respx.mock
async def test_lists_follow_every_cursor_and_names_match_case_insensitively() -> None:
    route = respx.get(f"{BASE}/v1/admin/projects").mock(
        side_effect=[
            httpx.Response(200, json={"items": [{"name": "Automated"}], "nextCursor": "c1"}),
            httpx.Response(
                200,
                json={
                    "items": [{"name": "automated TESTS", "projectId": PROJECT}],
                    "nextCursor": None,
                },
            ),
        ]
    )
    api = admin()
    found = await api.find_project("Automated tests")
    await api.client.aclose()
    assert found == {"name": "automated TESTS", "projectId": PROJECT}
    first, second = (call.request.url.params for call in route.calls)
    assert dict(first) == {"limit": "100", "status": "all", "q": "Automated tests"}
    assert second["cursor"] == "c1"


@respx.mock
async def test_an_unexpected_status_names_the_operation_and_code() -> None:
    respx.get(f"{BASE}/v1/admin/projects/{PROJECT}").mock(
        return_value=problem(403, "admin_required")
    )
    api = admin()
    with pytest.raises(
        AdminApiError, match="adminGetProject answered 403 admin_required"
    ) as raised:
        await api.get_project(PROJECT)
    await api.client.aclose()
    assert (raised.value.status, raised.value.code) == (403, "admin_required")


@respx.mock
async def test_versioned_writes_reread_the_project_after_412() -> None:
    respx.get(f"{BASE}/v1/admin/projects/{PROJECT}").mock(
        side_effect=[
            httpx.Response(200, json={"rowVersion": "AAAAAAAAAAE="}),
            httpx.Response(200, json={"rowVersion": "AAAAAAAAAAI="}),
        ]
    )
    put = respx.put(f"{BASE}/v1/admin/projects/{PROJECT}/members/{USER}").mock(
        side_effect=[problem(412, "precondition_failed"), httpx.Response(201, json={})]
    )
    api = admin()
    await api.ensure_member(PROJECT, USER)
    await api.client.aclose()
    assert [call.request.headers["If-Match"] for call in put.calls] == [
        '"AAAAAAAAAAE="',
        '"AAAAAAAAAAI="',
    ]


@respx.mock
async def test_member_removal_sends_the_project_version_and_reports_the_removal() -> None:
    respx.get(f"{BASE}/v1/admin/projects/{PROJECT}").mock(
        return_value=httpx.Response(200, json={"rowVersion": "AAAAAAAAAAE="})
    )
    delete = respx.delete(f"{BASE}/v1/admin/projects/{PROJECT}/members/{USER}").mock(
        side_effect=[
            httpx.Response(200, json={"removed": True, "projectRowVersion": "AAAAAAAAAAI="}),
            httpx.Response(200, json={"removed": False, "projectRowVersion": "AAAAAAAAAAE="}),
        ]
    )
    api = admin()
    assert await api.remove_member(PROJECT, USER) is True
    assert await api.remove_member(PROJECT, USER) is False
    await api.client.aclose()
    assert delete.calls[0].request.headers["If-Match"] == '"AAAAAAAAAAE="'


@respx.mock
async def test_versioned_writes_give_up_after_repeated_412_or_another_refusal() -> None:
    respx.get(f"{BASE}/v1/admin/projects/{PROJECT}").mock(
        return_value=httpx.Response(200, json={"rowVersion": "AAAAAAAAAAE="})
    )
    respx.put(f"{BASE}/v1/admin/projects/{PROJECT}/members/{USER}").mock(
        return_value=problem(412, "precondition_failed")
    )
    respx.delete(f"{BASE}/v1/admin/projects/{PROJECT}/indexes/{INDEX}").mock(
        return_value=problem(409, "default_index_in_use")
    )
    api = admin()
    with pytest.raises(AdminApiError, match="412"):
        await api.ensure_member(PROJECT, USER)
    with pytest.raises(AdminApiError, match="default_index_in_use"):
        await api.unmap_index(PROJECT, INDEX)
    await api.client.aclose()


@respx.mock
async def test_a_default_mapping_is_never_demoted() -> None:
    respx.get(f"{BASE}/v1/admin/projects/{PROJECT}/indexes").mock(
        return_value=httpx.Response(
            200, json={"items": [{"indexId": INDEX, "isDefault": True}], "nextCursor": None}
        )
    )
    put = respx.put(f"{BASE}/v1/admin/projects/{PROJECT}/indexes/{INDEX}")
    api = admin()
    await api.ensure_mapping(PROJECT, INDEX, is_default=False)
    await api.ensure_mapping(PROJECT, INDEX, is_default=True)
    await api.client.aclose()
    assert not put.called


@respx.mock
async def test_index_lookups() -> None:
    respx.get(f"{BASE}/v1/admin/indexes").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "indexId": INDEX,
                        "displayName": "automated-tests-delete-1",
                        "status": "ready",
                    },
                    {"indexId": PROJECT, "displayName": "automated-tests", "status": "failed"},
                ],
                "nextCursor": None,
            },
        )
    )
    respx.get(f"{BASE}/v1/admin/indexes/{INDEX}").mock(return_value=problem(404, "index_not_found"))
    respx.get(f"{BASE}/v1/admin/index-options").mock(
        return_value=httpx.Response(
            200,
            json={
                "methods": [],
                "defaults": {
                    "methodKey": "recursive",
                    "embeddingModel": "m",
                    "embeddingDimensions": 1024,
                    "languageAnalyzer": "standard.lucene",
                },
            },
        )
    )
    api = admin()
    prefixed = await api.find_indexes("automated-tests-delete-")
    assert [item["indexId"] for item in prefixed] == [INDEX]
    assert await api.get_index(INDEX) is None
    with pytest.raises(UnusableResourceError, match="automated-tests is failed"):
        await api.ensure_index("automated-tests")
    with pytest.raises(ValueError, match="no active method 'recursive'"):
        await api.index_body("other")
    await api.client.aclose()


@respx.mock
async def test_index_writes_send_the_index_version() -> None:
    patch = respx.patch(f"{BASE}/v1/admin/indexes/{INDEX}").mock(
        return_value=httpx.Response(200, json={"status": "reingest_required"})
    )
    reingest = respx.post(f"{BASE}/v1/admin/indexes/{INDEX}/reingest").mock(
        return_value=httpx.Response(202, json={"mode": "new_physical_index"})
    )
    delete = respx.delete(f"{BASE}/v1/admin/indexes/{INDEX}").mock(
        return_value=problem(409, "index_mapped")
    )
    api = admin()
    assert (await api.update_index(INDEX, "AAAAAAAAAAE=", {"languageAnalyzer": "en.lucene"}))[
        "status"
    ] == "reingest_required"
    assert (await api.start_reingest(INDEX, "AAAAAAAAAAI="))["mode"] == "new_physical_index"
    assert (await api.delete_index(INDEX, "AAAAAAAAAAM=")).status_code == 409
    await api.client.aclose()
    assert patch.calls.last.request.headers["If-Match"] == '"AAAAAAAAAAE="'
    assert json.loads(reingest.calls.last.request.content) == {}
    assert delete.calls.last.request.headers["If-Match"] == '"AAAAAAAAAAM="'


@respx.mock
async def test_uploads_reserve_then_stream_the_content() -> None:
    reserve = respx.post(f"{BASE}/v1/admin/indexes/{INDEX}/uploads").mock(
        side_effect=[
            httpx.Response(
                201,
                json={
                    "documentId": DOCUMENT,
                    "versionId": VERSION,
                    "versionNumber": 2,
                    "reused": False,
                },
            ),
            problem(409, "content_unchanged"),
        ]
    )
    content = respx.put(f"{BASE}/v1/admin/indexes/{INDEX}/uploads/{VERSION}/content").mock(
        return_value=httpx.Response(200, json={"version": {"state": "pending"}})
    )
    file = UploadFile(name="a.pdf", data=b"%PDF-" + b"x" * 2_000_000, sha256="ab" * 32)
    api = admin()
    reservation = await api.upload(INDEX, file, PROJECT, confirm_update=True)
    with pytest.raises(AdminApiError, match="content_unchanged"):
        await api.reserve(INDEX, file, PROJECT)
    await api.client.aclose()
    assert (reservation.document_id, reservation.version_number) == (DOCUMENT, 2)
    assert json.loads(reserve.calls[0].request.content) == {
        "name": "a.pdf",
        "sizeBytes": file.size,
        "sha256": file.sha256,
        "projectId": PROJECT,
        "confirmUpdate": True,
    }
    sent = content.calls.last.request
    assert sent.headers["Content-Length"] == str(file.size)
    assert "Transfer-Encoding" not in sent.headers
    assert sent.headers["Content-Type"] == "application/octet-stream"


@respx.mock
async def test_document_delete_rereads_after_412_and_ignores_a_deleted_document() -> None:
    url = f"{BASE}/v1/admin/indexes/{INDEX}/documents/{DOCUMENT}"
    respx.get(url).mock(
        side_effect=[
            httpx.Response(200, json={"rowVersion": "AAAAAAAAAAE="}),
            httpx.Response(200, json={"rowVersion": "AAAAAAAAAAI="}),
            problem(404, "document_not_found"),
        ]
    )
    respx.delete(url).mock(
        side_effect=[problem(412, "precondition_failed"), httpx.Response(202, json={"runId": RUN})]
    )
    api = admin()
    assert await api.delete_document(INDEX, DOCUMENT) == RUN
    assert await api.delete_document(INDEX, DOCUMENT) is None
    await api.client.aclose()


@respx.mock
async def test_runs_collect_every_item_and_waits_poll_until_the_end() -> None:
    url = f"{BASE}/v1/admin/ingestion-runs/{RUN}"
    respx.get(url).mock(
        side_effect=[
            httpx.Response(
                200, json={"run": {"state": "running"}, "items": [], "nextItemsCursor": None}
            ),
            httpx.Response(
                200, json={"run": {"state": "completed"}, "items": [1], "nextItemsCursor": "i"}
            ),
            httpx.Response(
                200, json={"run": {"state": "completed"}, "items": [2], "nextItemsCursor": None}
            ),
        ]
    )
    clock = Clock()
    api = admin(clock)
    detail = await api.wait_for_run(RUN, within=60)
    await api.client.aclose()
    assert detail["items"] == [1, 2]
    assert clock.waits == [15.0]


async def test_waits_end_with_a_timeout() -> None:
    clock = Clock()
    api = admin(clock)

    async def never() -> int | None:
        return None

    with pytest.raises(WaitTimeoutError, match="the thing didn't happen within 60 s"):
        await api.wait_until(never, within=60, what="the thing")
    await api.client.aclose()
    assert clock.waits == [15.0] * 4


@respx.mock
async def test_open_runs_lists_queued_and_running_runs() -> None:
    route = respx.get(f"{BASE}/v1/admin/ingestion-runs").mock(
        side_effect=[
            httpx.Response(200, json={"items": [{"runId": "q"}], "nextCursor": None}),
            httpx.Response(200, json={"items": [{"runId": "r"}], "nextCursor": None}),
        ]
    )
    api = admin()
    assert [run["runId"] for run in await api.open_runs()] == ["q", "r"]
    await api.client.aclose()
    assert [call.request.url.params["state"] for call in route.calls] == ["queued", "running"]
