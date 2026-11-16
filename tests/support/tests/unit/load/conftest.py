"""An in-memory API for the `docchat-load` tests: `listMyProjects`, `createChatTurn` streams,
`getCurrentUser` and `getSession`, keyed by the caller's bearer token."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, override

import httpx
import pytest

PROJECT_ID = "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
BASE_URL = "https://app-docchat-api-load-abc123.azurewebsites.net"


def stream_body(session_id: str, outcome: str) -> bytes:
    turn_id = str(uuid.uuid4())
    meta = {
        "type": "meta",
        "sessionId": session_id,
        "turnId": turn_id,
        "sessionCreated": True,
        "replayed": False,
        "correlationId": "0" * 31 + "1",
    }
    events: list[dict[str, Any]] = [meta, {"type": "delta", "text": "An answer."}]
    if outcome == "error":
        events.append(
            {
                "type": "error",
                "code": "model_unavailable",
                "message": "The answer service isn't available.",
                "retryable": True,
                "retryAfterSeconds": None,
                "correlationId": "0" * 31 + "1",
            }
        )
    else:
        events.append({"type": "citations", "citations": [], "allowedLinks": []})
        usage = {
            "promptTokens": 1,
            "completionTokens": 1,
            "reasoningTokens": 0,
            "cachedTokens": 0,
            "totalTokens": 2,
            "estimated": False,
        }
        events.append({"type": "done", "turnId": turn_id, "truncated": False, "usage": usage})
    lines = [json.dumps(event) for event in events]
    if outcome == "invalid":
        lines.insert(1, "{not json")
    return ("\n".join(lines) + "\n").encode("utf-8")


class FakeChatApi:
    """Answers as the API does for the calls the load commands make."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []
        """Outcomes of the next turns: `done` (the default), `error`, `invalid` or `status_<n>`."""
        self.failing_questions: set[str] = set()
        """Questions whose every turn answers `503`."""
        self.turns: list[dict[str, Any]] = []
        self.owners: dict[str, str] = {}
        self.session_turns: dict[str, list[dict[str, Any]]] = {}
        self.me_calls: list[str] = []
        self.calls: list[str] = []

    @staticmethod
    def caller(request: httpx.Request) -> str:
        return str(request.headers.get("Authorization", "")).removeprefix("Bearer ")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if request.method == "GET" and path == "/v1/projects":
            items = [{"projectId": PROJECT_ID, "name": "Load test", "description": ""}]
            return httpx.Response(200, json={"items": items, "nextCursor": None})
        if request.method == "GET" and path == "/v1/me":
            self.me_calls.append(self.caller(request))
            return httpx.Response(200, json={"userId": str(uuid.uuid4())})
        if request.method == "POST" and path == "/v1/chat/turns":
            return self.turn(request)
        if request.method == "GET" and path.startswith("/v1/sessions/"):
            session_id = path.rsplit("/", 1)[1]
            if self.owners.get(session_id) != self.caller(request):
                return httpx.Response(404, json={"code": "session_not_found"})
            detail = {
                "sessionId": session_id,
                "projectId": PROJECT_ID,
                "turns": self.session_turns.get(session_id, []),
            }
            return httpx.Response(200, json=detail)
        return httpx.Response(404, json={"code": "route_not_found"})

    def turn(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.turns.append(body | {"token": self.caller(request)})
        outcome = self.outcomes.pop(0) if self.outcomes else "done"
        if body.get("question") in self.failing_questions:
            outcome = "status_503"
        if outcome.startswith("status_"):
            return httpx.Response(int(outcome.removeprefix("status_")), json={"code": "x"})
        session_id = body.get("sessionId") or str(uuid.uuid4())
        self.owners.setdefault(session_id, self.caller(request))
        self.session_turns.setdefault(session_id, []).append(
            {"status": "completed" if outcome == "done" else "failed", "errorCode": None}
        )
        return httpx.Response(
            200,
            content=stream_body(session_id, outcome),
            headers={"Content-Type": "application/x-ndjson"},
        )

    @property
    def transport(self) -> httpx.MockTransport:
        # Each call yields to the loop once, as a real exchange does, so a worker that sends many
        # requests in a row never holds the loop past the guard.
        async def exchange(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0)
            return self.handler(request)

        return httpx.MockTransport(exchange)


@pytest.fixture
def chat_api() -> FakeChatApi:
    return FakeChatApi()


ROW_VERSION = "AAAAAAAAB9E="
PAGEWISE = {"key": "pagewise", "version": 1, "defaultParameters": {"pagesPerChunk": 1}}


def page(items: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "nextCursor": None})


class FakeLoadApi(FakeChatApi):
    """The chat calls plus the administrator calls of seeding and S4: projects, indexes,
    mappings, uploads and runs. An index is `ready` from its second read; a submitted version is
    ingested at once; a run is `running` at its first read and `completed` afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self.projects: dict[str, dict[str, Any]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}
        self.index_reads: dict[str, int] = {}
        self.mappings: dict[tuple[str, str], bool] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.run_reads: dict[str, int] = {}
        self.preflight_batches: list[int] = []
        self.submissions: list[list[str]] = []

    @override
    def handler(  # noqa: PLR0911, PLR0912 - one branch per route of the fake
        self, request: httpx.Request
    ) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/v1/admin/"):
            return super().handler(request)
        self.calls.append(f"{request.method} {path}")
        parts = path.removeprefix("/v1/admin/").split("/")
        is_json = request.headers.get("Content-Type", "").startswith("application/json")
        body = json.loads(request.content) if is_json and request.content else {}
        method = request.method
        if parts == ["projects"]:
            if method == "GET":
                q = request.url.params.get("q", "").casefold()
                return page([p for p in self.projects.values() if p["name"].casefold() == q])
            project = {
                "projectId": str(uuid.uuid4()),
                "name": body["name"],
                "isDefault": body.get("isDefault", False),
                "status": "active",
                "rowVersion": ROW_VERSION,
            }
            self.projects[project["projectId"]] = project
            return httpx.Response(201, json=project)
        if parts[0] == "projects" and len(parts) == 2:
            return httpx.Response(200, json=self.projects[parts[1]])
        if parts[0] == "projects" and parts[2] == "indexes":
            if method == "GET":
                return page(
                    [
                        {"indexId": index_id, "isDefault": default}
                        for (project, index_id), default in self.mappings.items()
                        if project == parts[1]
                    ]
                )
            self.mappings[parts[1], parts[3]] = body["isDefault"]
            return httpx.Response(201, json={})
        if parts == ["index-options"]:
            defaults = {
                "methodKey": "recursive",
                "embeddingModel": "text-embedding-3-large",
                "embeddingDimensions": 1024,
                "languageAnalyzer": "standard.lucene",
            }
            return httpx.Response(200, json={"methods": [PAGEWISE], "defaults": defaults})
        if parts == ["indexes"]:
            if method == "GET":
                q = request.url.params.get("q", "").casefold()
                found = [i for i in self.indexes.values() if i["displayName"].casefold() == q]
                return page(found)
            index = body | {"indexId": str(uuid.uuid4()), "status": "provisioning"}
            self.indexes[index["indexId"]] = index
            return httpx.Response(202, json={"index": index, "runId": str(uuid.uuid4())})
        if parts[0] == "indexes" and len(parts) == 2:
            index_id = parts[1]
            self.index_reads[index_id] = self.index_reads.get(index_id, 0) + 1
            if self.index_reads[index_id] > 1:
                self.indexes[index_id]["status"] = "ready"
            return httpx.Response(200, json=self.indexes[index_id])
        if parts[0] == "indexes" and parts[2:] == ["uploads", "preflight"]:
            files = body["files"]
            self.preflight_batches.append(len(files))
            return httpx.Response(
                200,
                json={
                    "results": [{"name": f["name"], "status": "new", "reason": None} for f in files]
                },
            )
        if parts[0] == "indexes" and parts[2:] == ["uploads"]:
            version_id = str(uuid.uuid4())
            self.versions[version_id] = body | {
                "indexId": parts[1],
                "documentId": str(uuid.uuid4()),
                "state": "uploading",
            }
            return httpx.Response(
                201,
                json={
                    "documentId": self.versions[version_id]["documentId"],
                    "versionId": version_id,
                    "versionNumber": 1,
                    "reused": False,
                },
            )
        if parts[0] == "indexes" and parts[2] == "uploads" and parts[4:] == ["content"]:
            version = self.versions[parts[3]]
            version["state"] = "pending"
            return httpx.Response(
                200, json={"version": {"versionId": parts[3], "state": "pending"}}
            )
        if parts[0] == "indexes" and parts[2:] == ["uploads", "submit"]:
            ids = body["versionIds"]
            self.submissions.append(ids)
            for version_id in ids:
                self.versions[version_id]["state"] = "ingested"
            run_id = str(uuid.uuid4())
            self.runs[run_id] = {
                "runId": run_id,
                "state": "running",
                "createdAt": "2026-11-03T10:00:00Z",
                "completedAt": None,
                "counts": {"succeeded": 0, "failed": 0, "total": len(ids)},
            }
            return httpx.Response(202, json={"runId": run_id, "results": []})
        if parts[0] == "indexes" and parts[2:] == ["documents"]:
            return page(
                [
                    {
                        "documentId": v["documentId"],
                        "name": v["name"],
                        "currentVersion": {"sha256": v["sha256"], "state": v["state"]},
                        "latestVersion": {"sha256": v["sha256"], "state": v["state"]},
                    }
                    for v in self.versions.values()
                    if v["indexId"] == parts[1] and v["state"] == "ingested"
                ]
            )
        if parts[0] == "ingestion-runs":
            run = self.runs[parts[1]]
            self.run_reads[parts[1]] = self.run_reads.get(parts[1], 0) + 1
            if self.run_reads[parts[1]] > 1:
                run |= {
                    "state": "completed",
                    "completedAt": "2026-11-03T11:00:00Z",
                    "counts": run["counts"] | {"succeeded": run["counts"]["total"]},
                }
            return httpx.Response(200, json={"run": run, "items": [], "nextItemsCursor": None})
        return httpx.Response(404, json={"code": "route_not_found"})


@pytest.fixture
def load_api() -> FakeLoadApi:
    return FakeLoadApi()
