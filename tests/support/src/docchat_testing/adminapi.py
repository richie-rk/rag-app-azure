"""The administrator operations the deployed suites drive (`84-testing.md` sections 6.5 to 6.7;
`11-api.md` sections 3.7 to 3.12; `14-ingestion.md` section 2).

`AdminApi` wraps a `DeployedApiClient` signed in as an administrator. It finds projects and
indexes by name and creates them when absent, so a suite that runs every night reuses what an
earlier run created; a create that loses a race with another job (`409 project_name_taken`,
`409 display_name_taken`) reads the winner instead. Writes guarded by the project's `rowVersion`
re-read the project and try again on `412`, because the token-holding jobs of one workflow run
change the same project concurrently. Uploads follow the API's four steps without the advisory
preflight (reserve, content, submit); the content travels as a re-iterable stream of 1 MiB
pieces, so a 50 MB body never passes through one event-loop callback and a retried call can send
it again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from docchat_testing.apiclient import DeployedApiClient

FINISHED_RUN_STATES: Final = frozenset({"completed", "completed_with_errors", "failed"})
FINISHED_ITEM_STATES: Final = frozenset({"succeeded", "skipped", "failed", "cancelled"})
DOCUMENT_STAGES: Final = (
    "validate",
    "extract",
    "enrich",
    "chunk",
    "embed",
    "index",
    "render",
    "flip",
    "clean_up",
)
"""The stages of a document item in the order it reaches them (`11-api.md` section 4.8)."""

PAGE_LIMIT: Final = 100
UPLOAD_PIECE_BYTES: Final = 1_048_576
DEFAULT_POLL_SECONDS: Final = 15.0
VERSION_RETRIES: Final = 5
"""Attempts of a `rowVersion`-guarded write before a `412` is given up as a real conflict."""

HTTP_OK: Final = 200
HTTP_CREATED: Final = 201
HTTP_ACCEPTED: Final = 202
HTTP_NOT_FOUND: Final = 404
HTTP_CONFLICT: Final = 409
HTTP_PRECONDITION_FAILED: Final = 412


class AdminApiError(Exception):
    """An administrator call answered a status its caller doesn't handle; the message names the
    operation, the status and the problem code, never a request body."""

    def __init__(self, operation: str, response: httpx.Response) -> None:
        self.status = response.status_code
        self.code = problem_code(response)
        super().__init__(f"{operation} answered {self.status} {self.code or ''}".rstrip())


class WaitTimeoutError(Exception):
    """A condition the suite polls for didn't hold within its limit."""


class UnusableResourceError(Exception):
    """A project or index the suites reuse by name is in a state no suite can work in (an
    archived project, a `failed` or `deleting` index); an operator repairs or removes it."""


def problem_code(response: httpx.Response) -> str | None:
    """The `code` of a problem body, or `None` for any other body (the platform's own `401` and
    `403` carry none)."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def if_match(row_version: str) -> dict[str, str]:
    """The `If-Match` header for a `rowVersion`, wrapped in double quotes (`11-api.md` 1.9)."""
    return {"If-Match": f'"{row_version}"'}


def stage_reached(stage: str | None, wanted: str) -> bool:
    """Whether an item at `stage` has reached `wanted` or a later document stage."""
    if stage not in DOCUMENT_STAGES:
        return False
    return DOCUMENT_STAGES.index(stage) >= DOCUMENT_STAGES.index(wanted)


class UploadBody:
    """A file's bytes as an upload body: an async iterable of 1 MiB pieces that starts afresh
    on every iteration, so the client's one retry can send it again."""

    def __init__(self, data: bytes, piece: int = UPLOAD_PIECE_BYTES) -> None:
        self._data = memoryview(data)
        self._piece = piece

    def __len__(self) -> int:
        return len(self._data)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._pieces()

    async def _pieces(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._data), self._piece):
            yield bytes(self._data[start : start + self._piece])


@dataclass(frozen=True, slots=True)
class UploadFile:
    """One file to upload: its display name, bytes and SHA-256 (64 lower-case hex digits)."""

    name: str
    data: bytes
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class Reservation:
    """`adminReserveUpload`'s answer."""

    document_id: str
    version_id: str
    version_number: int
    reused: bool


type Check[T] = Callable[[], Awaitable[T | None]]


class AdminApi:
    """The administrator operations of the deployed suites over one `DeployedApiClient`."""

    def __init__(
        self,
        client: DeployedApiClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.client = client
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_seconds = poll_seconds

    # --- Plumbing -------------------------------------------------------------------------

    async def call(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        expected: Collection[int],
        **kwargs: Any,
    ) -> httpx.Response:
        """One call; any status outside `expected` raises `AdminApiError`."""
        response = await self.client.request(method, path, **kwargs)
        if response.status_code not in expected:
            raise AdminApiError(operation, response)
        return response

    async def _json(self, operation: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.call(operation, method, path, expected=(HTTP_OK,), **kwargs)
        body: dict[str, Any] = response.json()
        return body

    async def _pages(
        self, operation: str, path: str, params: Mapping[str, str | int] | None = None
    ) -> list[dict[str, Any]]:
        """Every item of a paged list, following `nextCursor`."""
        query: dict[str, str | int] = {"limit": PAGE_LIMIT, **(params or {})}
        items: list[dict[str, Any]] = []
        while True:
            page = await self._json(operation, "GET", path, params=query)
            items.extend(page["items"])
            cursor = page.get("nextCursor")
            if not cursor:
                return items
            query["cursor"] = cursor

    async def wait_until[T](self, check: Check[T], *, within: float, what: str) -> T:
        """Poll `check` every poll interval until it returns a value, for at most `within`
        seconds."""
        deadline = self._monotonic() + within
        while True:
            value = await check()
            if value is not None:
                return value
            if self._monotonic() >= deadline:
                raise WaitTimeoutError(f"{what} didn't happen within {within:.0f} s")
            await self._sleep(self._poll_seconds)

    # --- Projects, members and mappings ---------------------------------------------------

    async def find_project(self, name: str) -> dict[str, Any] | None:
        wanted = name.casefold()
        for item in await self._pages(
            "adminListProjects", "/v1/admin/projects", {"status": "all", "q": name}
        ):
            if str(item["name"]).casefold() == wanted:
                return item
        return None

    async def get_project(self, project_id: str) -> dict[str, Any]:
        return await self._json("adminGetProject", "GET", f"/v1/admin/projects/{project_id}")

    async def ensure_project(self, name: str, *, description: str = "") -> dict[str, Any]:
        """The active project named `name`, created (not default, no members) when absent."""
        found = await self.find_project(name)
        if found is None:
            response = await self.call(
                "adminCreateProject",
                "POST",
                "/v1/admin/projects",
                expected=(HTTP_CREATED, HTTP_CONFLICT),
                json={"name": name, "description": description, "isDefault": False},
            )
            if response.status_code == HTTP_CREATED:
                created: dict[str, Any] = response.json()
                return created
            if problem_code(response) != "project_name_taken":
                raise AdminApiError("adminCreateProject", response)
            found = await self.find_project(name)
            if found is None:
                raise AdminApiError("adminCreateProject", response)
        if found["status"] != "active":
            raise UnusableResourceError(f"project {name} is {found['status']}")
        return await self.get_project(str(found["projectId"]))

    async def _versioned_project_write(
        self,
        operation: str,
        method: str,
        path: str,
        project_id: str,
        *,
        expected: Collection[int],
        **kwargs: Any,
    ) -> httpx.Response:
        async def attempt() -> httpx.Response:
            project = await self.get_project(project_id)
            return await self.client.request(
                method, path, headers=if_match(str(project["rowVersion"])), **kwargs
            )

        response = await attempt()
        for _ in range(VERSION_RETRIES - 1):
            if response.status_code != HTTP_PRECONDITION_FAILED:
                break
            response = await attempt()
        if response.status_code not in expected:
            raise AdminApiError(operation, response)
        return response

    async def ensure_member(self, project_id: str, user_id: str) -> None:
        """Make `user_id` (a signed-in user's object ID) a member of the project."""
        await self._versioned_project_write(
            "adminAddProjectMember",
            "PUT",
            f"/v1/admin/projects/{project_id}/members/{user_id}",
            project_id,
            expected=(HTTP_OK, HTTP_CREATED),
        )

    async def remove_member(self, project_id: str, user_id: str) -> bool:
        """Remove `user_id`'s membership of the project; whether a membership was removed."""
        response = await self._versioned_project_write(
            "adminRemoveProjectMember",
            "DELETE",
            f"/v1/admin/projects/{project_id}/members/{user_id}",
            project_id,
            expected=(HTTP_OK,),
        )
        return bool(response.json()["removed"])

    async def project_indexes(self, project_id: str) -> list[dict[str, Any]]:
        return await self._pages(
            "adminListProjectIndexes", f"/v1/admin/projects/{project_id}/indexes"
        )

    async def ensure_mapping(self, project_id: str, index_id: str, *, is_default: bool) -> None:
        """Map the index to the project; a default mapping asked for as a non-default one is
        left as it is, because a project must keep its default."""
        for mapping in await self.project_indexes(project_id):
            if mapping["indexId"] == index_id and (mapping["isDefault"] or not is_default):
                return
        await self._versioned_project_write(
            "adminMapProjectIndex",
            "PUT",
            f"/v1/admin/projects/{project_id}/indexes/{index_id}",
            project_id,
            expected=(HTTP_OK, HTTP_CREATED),
            json={"isDefault": is_default},
        )

    async def unmap_index(self, project_id: str, index_id: str) -> None:
        await self._versioned_project_write(
            "adminUnmapProjectIndex",
            "DELETE",
            f"/v1/admin/projects/{project_id}/indexes/{index_id}",
            project_id,
            expected=(HTTP_OK,),
        )

    # --- Indexes --------------------------------------------------------------------------

    async def index_options(self) -> dict[str, Any]:
        return await self._json("adminGetIndexOptions", "GET", "/v1/admin/index-options")

    async def find_index(self, name: str) -> dict[str, Any] | None:
        """The index (any status but `deleted`) whose display name is `name`."""
        wanted = name.casefold()
        for item in await self._pages("adminListIndexes", "/v1/admin/indexes", {"q": name}):
            if str(item["displayName"]).casefold() == wanted:
                return item
        return None

    async def find_indexes(self, prefix: str) -> list[dict[str, Any]]:
        """Every index (any status but `deleted`) whose display name starts with `prefix`."""
        wanted = prefix.casefold()
        return [
            item
            for item in await self._pages("adminListIndexes", "/v1/admin/indexes", {"q": prefix})
            if str(item["displayName"]).casefold().startswith(wanted)
        ]

    async def get_index(self, index_id: str) -> dict[str, Any] | None:
        """The index's `IndexDetail`, or `None` once it is deleted."""
        response = await self.call(
            "adminGetIndex",
            "GET",
            f"/v1/admin/indexes/{index_id}",
            expected=(HTTP_OK, HTTP_NOT_FOUND),
        )
        if response.status_code == HTTP_NOT_FOUND:
            return None
        detail: dict[str, Any] = response.json()
        return detail

    async def index_body(self, name: str, *, method_key: str | None = None) -> dict[str, Any]:
        """An `IndexCreate` body with the catalogue's defaults: the default method (or the
        highest active version of `method_key`) at its default parameters, the default
        embedding model, dimensions and analyzer."""
        options = await self.index_options()
        defaults = options["defaults"]
        key = defaults["methodKey"] if method_key is None else method_key
        methods = [method for method in options["methods"] if method["key"] == key]
        if not methods:
            raise ValueError(f"the method catalogue has no active method {key!r}")
        method = max(methods, key=lambda option: int(option["version"]))
        return {
            "displayName": name,
            "methodKey": key,
            "methodVersion": int(method["version"]),
            "methodParameters": method["defaultParameters"],
            "embeddingModel": defaults["embeddingModel"],
            "embeddingDimensions": defaults["embeddingDimensions"],
            "languageAnalyzer": defaults["languageAnalyzer"],
        }

    async def create_index(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """`adminCreateIndex`: the `IndexRunStarted` answer."""
        response = await self.call(
            "adminCreateIndex", "POST", "/v1/admin/indexes", expected=(HTTP_ACCEPTED,), json=body
        )
        started: dict[str, Any] = response.json()
        return started

    async def ensure_index(self, name: str, *, method_key: str | None = None) -> dict[str, Any]:
        """The index named `name`, created with the catalogue's defaults when absent; one in
        status `failed` or `deleting` can't serve a suite and raises."""
        found = await self.find_index(name)
        if found is None:
            body = await self.index_body(name, method_key=method_key)
            response = await self.call(
                "adminCreateIndex",
                "POST",
                "/v1/admin/indexes",
                expected=(HTTP_ACCEPTED, HTTP_CONFLICT),
                json=body,
            )
            if response.status_code == HTTP_ACCEPTED:
                found = response.json()["index"]
            elif problem_code(response) == "display_name_taken":
                found = await self.find_index(name)
            if found is None:
                raise AdminApiError("adminCreateIndex", response)
        if found["status"] in {"failed", "deleting"}:
            raise UnusableResourceError(f"index {name} is {found['status']}")
        return found

    async def update_index(
        self, index_id: str, row_version: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        response = await self.call(
            "adminUpdateIndex",
            "PATCH",
            f"/v1/admin/indexes/{index_id}",
            expected=(HTTP_OK,),
            headers=if_match(row_version),
            json=body,
        )
        detail: dict[str, Any] = response.json()
        return detail

    async def start_reingest(self, index_id: str, row_version: str) -> dict[str, Any]:
        response = await self.call(
            "adminStartReingest",
            "POST",
            f"/v1/admin/indexes/{index_id}/reingest",
            expected=(HTTP_ACCEPTED,),
            headers=if_match(row_version),
            json={},
        )
        started: dict[str, Any] = response.json()
        return started

    async def delete_index(self, index_id: str, row_version: str) -> httpx.Response:
        """`adminDeleteIndex`'s raw answer; the caller decides what each status means."""
        return await self.client.request(
            "DELETE", f"/v1/admin/indexes/{index_id}", headers=if_match(row_version)
        )

    async def wait_for_index(
        self, index_id: str, statuses: Collection[str], *, within: float
    ) -> dict[str, Any]:
        """Poll the index until its status is one of `statuses`."""

        async def reached() -> dict[str, Any] | None:
            detail = await self.get_index(index_id)
            if detail is None:
                raise WaitTimeoutError(f"index {index_id} was deleted")
            return detail if detail["status"] in statuses else None

        return await self.wait_until(
            reached, within=within, what=f"index {index_id} reaching {sorted(statuses)}"
        )

    # --- Documents and uploads ------------------------------------------------------------

    async def list_documents(self, index_id: str) -> list[dict[str, Any]]:
        return await self._pages("adminListDocuments", f"/v1/admin/indexes/{index_id}/documents")

    async def get_document(self, index_id: str, document_id: str) -> dict[str, Any] | None:
        """The document's `DocumentDetail`, or `None` when it is deleted or absent."""
        response = await self.call(
            "adminGetDocument",
            "GET",
            f"/v1/admin/indexes/{index_id}/documents/{document_id}",
            expected=(HTTP_OK, HTTP_NOT_FOUND),
        )
        if response.status_code == HTTP_NOT_FOUND:
            return None
        detail: dict[str, Any] = response.json()
        return detail

    async def delete_document(self, index_id: str, document_id: str) -> str | None:
        """Delete a live document; the `document_delete` run's ID, or `None` when it was already
        gone. A `412` (the document moved since it was read) reads it again."""
        refusal: httpx.Response | None = None
        for _ in range(VERSION_RETRIES):
            detail = await self.get_document(index_id, document_id)
            if detail is None:
                return None
            refusal = await self.client.request(
                "DELETE",
                f"/v1/admin/indexes/{index_id}/documents/{document_id}",
                headers=if_match(str(detail["rowVersion"])),
            )
            if refusal.status_code == HTTP_ACCEPTED:
                return str(refusal.json()["runId"])
            if refusal.status_code == HTTP_NOT_FOUND:
                return None
            if refusal.status_code != HTTP_PRECONDITION_FAILED:
                break
        assert refusal is not None
        raise AdminApiError("adminDeleteDocument", refusal)

    async def preflight(self, index_id: str, files: Sequence[UploadFile]) -> list[dict[str, Any]]:
        body = {
            "files": [
                {"name": file.name, "sizeBytes": file.size, "sha256": file.sha256} for file in files
            ]
        }
        answer = await self._json(
            "adminPreflightUploads",
            "POST",
            f"/v1/admin/indexes/{index_id}/uploads/preflight",
            json=body,
        )
        results: list[dict[str, Any]] = answer["results"]
        return results

    async def reserve_response(
        self, index_id: str, file: UploadFile, project_id: str, *, confirm_update: bool = False
    ) -> httpx.Response:
        """`adminReserveUpload`'s raw answer, for the checks that expect a refusal."""
        return await self.client.request(
            "POST",
            f"/v1/admin/indexes/{index_id}/uploads",
            json={
                "name": file.name,
                "sizeBytes": file.size,
                "sha256": file.sha256,
                "projectId": project_id,
                "confirmUpdate": confirm_update,
            },
        )

    async def reserve(
        self, index_id: str, file: UploadFile, project_id: str, *, confirm_update: bool = False
    ) -> Reservation:
        response = await self.reserve_response(
            index_id, file, project_id, confirm_update=confirm_update
        )
        if response.status_code != HTTP_CREATED:
            raise AdminApiError("adminReserveUpload", response)
        body = response.json()
        return Reservation(
            document_id=str(body["documentId"]),
            version_id=str(body["versionId"]),
            version_number=int(body["versionNumber"]),
            reused=bool(body["reused"]),
        )

    async def upload_content(self, index_id: str, version_id: str, data: bytes) -> dict[str, Any]:
        """Stream the bytes into the reserved version; its `VersionSummary` (state `pending`)."""
        response = await self.call(
            "adminUploadContent",
            "PUT",
            f"/v1/admin/indexes/{index_id}/uploads/{version_id}/content",
            expected=(HTTP_OK,),
            content=UploadBody(data),
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))},
        )
        version: dict[str, Any] = response.json()["version"]
        return version

    async def upload(
        self, index_id: str, file: UploadFile, project_id: str, *, confirm_update: bool = False
    ) -> Reservation:
        """Reserve and upload one file; the version is `pending` and not yet submitted."""
        reservation = await self.reserve(index_id, file, project_id, confirm_update=confirm_update)
        await self.upload_content(index_id, reservation.version_id, file.data)
        return reservation

    async def submit(self, index_id: str, version_ids: Sequence[str]) -> dict[str, Any]:
        """`adminSubmitUploads`: the `UploadSubmitResult` (`runId` null when nothing queued)."""
        response = await self.call(
            "adminSubmitUploads",
            "POST",
            f"/v1/admin/indexes/{index_id}/uploads/submit",
            expected=(HTTP_OK, HTTP_ACCEPTED),
            json={"versionIds": list(version_ids)},
        )
        result: dict[str, Any] = response.json()
        return result

    # --- Runs -----------------------------------------------------------------------------

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """`adminGetIngestionRun` with every item, following `nextItemsCursor`."""
        path = f"/v1/admin/ingestion-runs/{run_id}"
        query: dict[str, str | int] = {"itemsLimit": PAGE_LIMIT}
        detail = await self._json("adminGetIngestionRun", "GET", path, params=query)
        items = list(detail["items"])
        cursor = detail.get("nextItemsCursor")
        while cursor:
            query["itemsCursor"] = cursor
            page = await self._json("adminGetIngestionRun", "GET", path, params=query)
            items.extend(page["items"])
            cursor = page.get("nextItemsCursor")
        return detail | {"items": items, "nextItemsCursor": None}

    async def wait_for_run(self, run_id: str, *, within: float) -> dict[str, Any]:
        """Poll the run until it ends; its detail with every item."""

        async def finished() -> dict[str, Any] | None:
            detail = await self.get_run(run_id)
            return detail if detail["run"]["state"] in FINISHED_RUN_STATES else None

        return await self.wait_until(finished, within=within, what=f"run {run_id} ending")

    async def open_runs(self) -> list[dict[str, Any]]:
        """Every `queued` or `running` run of the deployment."""
        runs: list[dict[str, Any]] = []
        for state in ("queued", "running"):
            runs += await self._pages(
                "adminListIngestionRuns", "/v1/admin/ingestion-runs", {"state": state}
            )
        return runs
