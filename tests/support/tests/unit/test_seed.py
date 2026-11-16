from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from docchat_testing.adminapi import AdminApi
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.seed import (
    DOCUMENTS_DIR,
    FIXTURE_DOCUMENTS,
    INDEX_NAME,
    PROJECT_NAME,
    SeedError,
    byte_unique,
    delete_documents,
    ensure_mapped_index,
    ensure_seed_data,
    fixture_bytes,
    generated_pdf,
    sha256_hex,
    upload_file,
)

BASE = "https://app-docchat-api-dev.example"
MEMBER = "4c1f7d2e-8a3b-4e5f-9a6b-7c8d9e0f1a2b"
PAGEWISE_DEFAULTS = {"pagesPerChunk": 1}
RECURSIVE_DEFAULTS = {"targetTokens": 512, "overlapPercent": 25, "tableMaxTokens": 1600}
ROW_VERSION = "AAAAAAAAB9E="


def new_id() -> str:
    return str(uuid.uuid4())


def problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"status": status, "code": code})


class FakeAdminApi:
    """An in-memory administrator API with the answers the seed relies on: names unique, a
    project's first mapping its default, `If-Match` on the project's `rowVersion`, uploads that
    ingest as soon as they are submitted (or fail, for names in `failing`)."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.members: set[tuple[str, str]] = set()
        self.mappings: dict[tuple[str, str], bool] = {}
        self.indexes: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.failing: set[str] = set()
        self.calls: list[str] = []
        self.version_counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        for (method, pattern), action in self.routes().items():
            match = re.fullmatch(pattern, path)
            if request.method == method and match:
                return action(request, *match.groups())
        return problem(404, "route_not_found")

    def routes(self) -> dict[tuple[str, str], Callable[..., httpx.Response]]:
        uid = "([0-9a-f-]{36})"
        return {
            ("GET", "/v1/admin/projects"): self.list_projects,
            ("POST", "/v1/admin/projects"): self.create_project,
            ("GET", f"/v1/admin/projects/{uid}"): self.get_project,
            ("PUT", f"/v1/admin/projects/{uid}/members/{uid}"): self.add_member,
            ("GET", f"/v1/admin/projects/{uid}/indexes"): self.list_mappings,
            ("PUT", f"/v1/admin/projects/{uid}/indexes/{uid}"): self.map_index,
            ("GET", "/v1/admin/index-options"): self.options,
            ("GET", "/v1/admin/indexes"): self.list_indexes,
            ("POST", "/v1/admin/indexes"): self.create_index,
            ("POST", f"/v1/admin/indexes/{uid}/uploads/preflight"): self.preflight,
            ("POST", f"/v1/admin/indexes/{uid}/uploads"): self.reserve,
            ("PUT", f"/v1/admin/indexes/{uid}/uploads/{uid}/content"): self.content,
            ("POST", f"/v1/admin/indexes/{uid}/uploads/submit"): self.submit,
            ("GET", f"/v1/admin/indexes/{uid}/documents"): self.list_documents,
            ("GET", f"/v1/admin/indexes/{uid}/documents/{uid}"): self.get_document,
            ("DELETE", f"/v1/admin/indexes/{uid}/documents/{uid}"): self.delete_document,
            ("GET", f"/v1/admin/ingestion-runs/{uid}"): self.get_run,
        }

    @staticmethod
    def page(items: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(200, json={"items": items, "nextCursor": None})

    def list_projects(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "").casefold()
        return self.page([p for p in self.projects.values() if p["name"].casefold().startswith(q)])

    def create_project(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(p["name"].casefold() == body["name"].casefold() for p in self.projects.values()):
            return problem(409, "project_name_taken")
        project = {"projectId": new_id(), "name": body["name"], "status": "active"}
        project["rowVersion"] = ROW_VERSION
        self.projects[project["projectId"]] = project
        return httpx.Response(201, json=project)

    def get_project(self, request: httpx.Request, project_id: str) -> httpx.Response:
        return httpx.Response(200, json=self.projects[project_id])

    def check_version(self, request: httpx.Request, project_id: str) -> httpx.Response | None:
        if request.headers.get("If-Match") != f'"{self.projects[project_id]["rowVersion"]}"':
            return problem(412, "precondition_failed")
        return None

    def bump(self, project_id: str) -> None:
        self.projects[project_id]["rowVersion"] = f"AAAAAAAAB{len(self.calls) % 10}E="

    def add_member(self, request: httpx.Request, project_id: str, user: str) -> httpx.Response:
        refused = self.check_version(request, project_id)
        if refused is not None:
            return refused
        created = (project_id, user) not in self.members
        self.members.add((project_id, user))
        if created:
            self.bump(project_id)
        return httpx.Response(201 if created else 200, json={})

    def list_mappings(self, request: httpx.Request, project_id: str) -> httpx.Response:
        return self.page(
            [
                {"indexId": index_id, "isDefault": default}
                for (project, index_id), default in self.mappings.items()
                if project == project_id
            ]
        )

    def map_index(self, request: httpx.Request, project_id: str, index_id: str) -> httpx.Response:
        refused = self.check_version(request, project_id)
        if refused is not None:
            return refused
        default = json.loads(request.content)["isDefault"]
        if not default and not any(p == project_id for p, _ in self.mappings):
            return problem(409, "default_index_required")
        if default:
            for key in [k for k in self.mappings if k[0] == project_id]:
                self.mappings[key] = False
        created = (project_id, index_id) not in self.mappings
        self.mappings[project_id, index_id] = default
        self.bump(project_id)
        return httpx.Response(201 if created else 200, json={})

    def options(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "methods": [
                    {"key": "pagewise", "version": 1, "defaultParameters": PAGEWISE_DEFAULTS},
                    {"key": "recursive", "version": 1, "defaultParameters": RECURSIVE_DEFAULTS},
                ],
                "defaults": {
                    "methodKey": "recursive",
                    "methodVersion": 1,
                    "embeddingModel": "text-embedding-3-large",
                    "embeddingDimensions": 1024,
                    "languageAnalyzer": "standard.lucene",
                },
            },
        )

    def list_indexes(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "").casefold()
        return self.page(
            [i for i in self.indexes.values() if i["displayName"].casefold().startswith(q)]
        )

    def create_index(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        index = {"indexId": new_id(), "status": "provisioning", "rowVersion": ROW_VERSION} | body
        self.indexes[index["indexId"]] = index
        return httpx.Response(202, json={"index": index, "runId": new_id()})

    def live(self, index_id: str) -> list[dict[str, Any]]:
        return [d for d in self.documents.values() if d["indexId"] == index_id and not d["deleted"]]

    def preflight(self, request: httpx.Request, index_id: str) -> httpx.Response:
        results = []
        for file in json.loads(request.content)["files"]:
            same = [
                d for d in self.live(index_id) if d["name"].casefold() == file["name"].casefold()
            ]
            active = [
                v
                for d in same
                for v in d["versions"]
                if v["state"] in {"pending", "ingesting", "ingested"}
            ]
            status = "new"
            if any(v["sha256"] == file["sha256"] for v in active):
                status = "unchanged"
            elif active:
                status = "update"
            results.append(
                {"name": file["name"], "status": status, "reason": None}
                | {"documentId": same[0]["documentId"] if same else None}
            )
        return httpx.Response(200, json={"results": results})

    def reserve(self, request: httpx.Request, index_id: str) -> httpx.Response:
        body = json.loads(request.content)
        same = [d for d in self.live(index_id) if d["name"].casefold() == body["name"].casefold()]
        if same and not body["confirmUpdate"]:
            return problem(409, "update_not_confirmed")
        document = same[0] if same else None
        if document is None:
            document = {"documentId": new_id(), "indexId": index_id, "name": body["name"]}
            document |= {"versions": [], "deleted": False, "rowVersion": ROW_VERSION}
            self.documents[document["documentId"]] = document
        self.version_counter += 1
        version = {"versionId": new_id(), "sha256": body["sha256"], "state": "uploading"}
        document["versions"].append(version)
        return httpx.Response(
            201,
            json={
                "documentId": document["documentId"],
                "versionId": version["versionId"],
                "versionNumber": len(document["versions"]),
                "reused": False,
            },
        )

    def version(self, version_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for document in self.documents.values():
            for version in document["versions"]:
                if version["versionId"] == version_id:
                    return document, version
        raise KeyError(version_id)

    def content(self, request: httpx.Request, index_id: str, version_id: str) -> httpx.Response:
        _, version = self.version(version_id)
        data = request.read()
        assert int(request.headers["Content-Length"]) == len(data)
        assert request.headers["Content-Type"] == "application/octet-stream"
        assert sha256_hex(data) == version["sha256"]
        version["state"] = "pending"
        return httpx.Response(200, json={"version": version})

    def submit(self, request: httpx.Request, index_id: str) -> httpx.Response:
        results = []
        for version_id in json.loads(request.content)["versionIds"]:
            document, version = self.version(version_id)
            outcome = "not_pending"
            if version["state"] == "pending":
                outcome = "queued"
                failed = document["name"] in self.failing
                version["state"] = "failed" if failed else "ingested"
                version["failureCode"] = "parse_failed" if failed else None
            results.append({"versionId": version_id, "outcome": outcome})
        run_id = new_id()
        self.runs[run_id] = {"run": {"runId": run_id, "state": "completed"}, "items": []}
        return httpx.Response(202, json={"runId": run_id, "results": results})

    def summary(self, document: dict[str, Any]) -> dict[str, Any]:
        ingested = [v for v in document["versions"] if v["state"] == "ingested"]
        return {
            "documentId": document["documentId"],
            "name": document["name"],
            "rowVersion": document["rowVersion"],
            "currentVersion": ingested[-1] if ingested else None,
            "latestVersion": document["versions"][-1] if document["versions"] else None,
        }

    def list_documents(self, request: httpx.Request, index_id: str) -> httpx.Response:
        return self.page([self.summary(d) for d in self.live(index_id)])

    def get_document(self, request: httpx.Request, index_id: str, doc_id: str) -> httpx.Response:
        document = self.documents.get(doc_id)
        if document is None or document["deleted"]:
            return problem(404, "document_not_found")
        return httpx.Response(200, json=self.summary(document) | {"versions": document["versions"]})

    def delete_document(self, request: httpx.Request, index_id: str, doc_id: str) -> httpx.Response:
        document = self.documents[doc_id]
        if request.headers.get("If-Match") != f'"{document["rowVersion"]}"':
            return problem(412, "precondition_failed")
        document["deleted"] = True
        run_id = new_id()
        self.runs[run_id] = {"run": {"runId": run_id, "state": "completed"}, "items": []}
        return httpx.Response(202, json={"runId": run_id})

    def get_run(self, request: httpx.Request, run_id: str) -> httpx.Response:
        return httpx.Response(200, json=self.runs[run_id] | {"nextItemsCursor": None})


async def bearer() -> str:
    return "token"


async def no_sleep(seconds: float) -> None:
    return None


def admin(fake: FakeAdminApi) -> AdminApi:
    client = DeployedApiClient(BASE, bearer, transport=httpx.MockTransport(fake.handler))
    return AdminApi(client, sleep=no_sleep)


async def test_seed_data_is_created_once_and_reused() -> None:
    fake = FakeAdminApi()
    api = admin(fake)
    first = await ensure_seed_data(api, member_id=MEMBER, within=60)
    project = fake.projects[first.project_id]
    assert project["name"] == PROJECT_NAME
    index = fake.indexes[first.index_id]
    assert index["displayName"] == INDEX_NAME
    assert index["methodKey"] == "pagewise"
    assert index["methodParameters"] == PAGEWISE_DEFAULTS
    assert fake.mappings == {(first.project_id, first.index_id): True}
    assert fake.members == {(first.project_id, MEMBER)}
    assert set(first.document_ids) == set(FIXTURE_DOCUMENTS)
    assert all(d["versions"][-1]["state"] == "ingested" for d in fake.documents.values())

    calls = len(fake.calls)
    second = await ensure_seed_data(api, member_id=MEMBER, within=60)
    await api.client.aclose()
    assert second == first
    later = fake.calls[calls:]
    assert not any(
        call.startswith(("POST /v1/admin/projects", "POST /v1/admin/indexes ")) for call in later
    )
    assert not any(call.endswith(("/content", "/submit")) for call in later)
    assert len(fake.projects) == 1
    assert len(fake.indexes) == 1
    assert len(fake.documents) == len(FIXTURE_DOCUMENTS)


async def test_seed_resubmits_versions_left_pending_and_uploads_changed_fixtures() -> None:
    fake = FakeAdminApi()
    api = admin(fake)
    seeded = await ensure_seed_data(api, member_id=None, within=60)
    tables = fake.documents[seeded.document_ids["tables.pdf"]]
    tables["versions"][-1]["state"] = "pending"
    scanned = fake.documents[seeded.document_ids["scanned.pdf"]]
    scanned["versions"][-1]["sha256"] = "0" * 64
    await ensure_seed_data(api, member_id=None, within=60)
    await api.client.aclose()
    assert tables["versions"][-1]["state"] == "ingested"
    assert len(scanned["versions"]) == 2
    assert scanned["versions"][-1]["state"] == "ingested"
    assert fake.members == set()


async def test_seed_stops_when_a_fixture_fails_to_ingest() -> None:
    fake = FakeAdminApi()
    fake.failing.add("scanned.pdf")
    api = admin(fake)
    with pytest.raises(SeedError, match=r"scanned\.pdf failed to ingest \(parse_failed\)"):
        await ensure_seed_data(api, member_id=None, within=60)
    await api.client.aclose()


async def test_project_name_race_reads_the_winner() -> None:
    fake = FakeAdminApi()
    winner = {"projectId": new_id(), "name": PROJECT_NAME, "status": "active"}
    original = fake.list_projects
    listed: list[int] = []

    def racing(request: httpx.Request) -> httpx.Response:
        # The first lookup misses; another job creates the project before this one's create.
        listed.append(1)
        if len(listed) == 1:
            fake.projects[winner["projectId"]] = winner | {"rowVersion": ROW_VERSION}
            return fake.page([])
        return original(request)

    fake.list_projects = racing  # type: ignore[method-assign]
    api = admin(fake)
    mapped = await ensure_mapped_index(api, "automated-tests-resume")
    await api.client.aclose()
    assert mapped.project_id == winner["projectId"]
    assert len(fake.projects) == 1
    default = next(i for i in fake.indexes.values() if i["displayName"] == INDEX_NAME)
    resume = next(i for i in fake.indexes.values() if i["displayName"] != INDEX_NAME)
    assert resume["methodKey"] == "recursive"
    assert fake.mappings == {
        (mapped.project_id, default["indexId"]): True,
        (mapped.project_id, resume["indexId"]): False,
    }


async def test_delete_documents_waits_for_each_delete_run() -> None:
    fake = FakeAdminApi()
    api = admin(fake)
    seeded = await ensure_seed_data(api, member_id=None, within=60)
    ids = list(seeded.document_ids.values())
    await delete_documents(api, seeded.index_id, [*ids, new_id()], within=60)
    await api.client.aclose()
    assert all(d["deleted"] for d in fake.documents.values())
    assert sum(call.startswith("GET /v1/admin/ingestion-runs/") for call in fake.calls) == 4


def zip_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
        archive.comment = b"original"
    return buffer.getvalue()


def test_byte_unique_marks_pdfs_after_eof_and_zips_in_their_comment() -> None:
    pdf = generated_pdf(1, label="x")
    marked = byte_unique(pdf, "tables.pdf", "docchat-resume-42-1")
    assert marked == pdf + b"%docchat-resume-42-1\n"
    assert byte_unique(pdf.rstrip(b"\n"), "a.PDF", "l").endswith(b"%%EOF\n%l\n")
    archive = zip_archive()
    unique = byte_unique(archive, "headings.docx", "docchat-resume-42-3")
    with zipfile.ZipFile(io.BytesIO(unique)) as reopened:
        assert reopened.comment == b"docchat-resume-42-3"
        assert reopened.read("word/document.xml") == b"<w:document/>"
    assert sha256_hex(unique) != sha256_hex(archive)
    with pytest.raises(ValueError, match="only PDF, DOCX and PPTX"):
        byte_unique(pdf, "notes.txt", "l")
    with pytest.raises(ValueError, match="no end-of-central-directory"):
        byte_unique(b"PK\x03\x04", "a.pptx", "l")
    with pytest.raises(ValueError, match="bytes after"):
        byte_unique(archive + b"x", "a.pptx", "l")
    with pytest.raises(ValueError, match="no %%EOF"):
        byte_unique(b"%PDF-1.7\n", "a.pdf", "l")
    with pytest.raises(ValueError, match="printable ASCII"):
        byte_unique(pdf, "a.pdf", "é")


def test_generated_pdf_has_its_pages_and_an_exact_size() -> None:
    pdf = generated_pdf(3, label="resume-7-large")
    assert pdf.startswith(b"%PDF-1.7")
    assert pdf.endswith(b"%%EOF\n")
    assert b"/Count 3" in pdf
    assert pdf.count(b"/Type /Page ") == 3
    assert b"(resume-7-large: page 3) Tj" in pdf
    startxref = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n", 1)[0])
    assert pdf[startxref:].startswith(b"xref\n")
    for size in (len(pdf) + 200, len(pdf) + 10_000, 1_048_576):
        sized = generated_pdf(3, label="resume-7-large", size_bytes=size)
        assert len(sized) == size
        offsets = re.findall(rb"(\d{10}) 00000 n ", sized)
        for number, offset in enumerate(offsets, start=1):
            assert sized[int(offset) :].startswith(b"%d 0 obj\n" % number)
    for too_small in (100, len(pdf) + 1):
        with pytest.raises(ValueError, match="exactly"):
            generated_pdf(3, label="resume-7-large", size_bytes=too_small)
    with pytest.raises(ValueError, match="at least one page"):
        generated_pdf(0, label="x")


def test_fixture_documents_exist() -> None:
    for name in FIXTURE_DOCUMENTS:
        assert (DOCUMENTS_DIR / name).is_file()
    file = upload_file("tables.pdf", fixture_bytes("tables.pdf"))
    assert file.sha256 == sha256_hex((Path(DOCUMENTS_DIR) / "tables.pdf").read_bytes())
    assert file.size > 0
