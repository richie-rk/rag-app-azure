"""The four upload operations: `adminPreflightUploads`, `adminReserveUpload`,
`adminUploadContent` and `adminSubmitUploads` (`14-ingestion.md` sections 2, 3.2, 3.3, 5.5 and
17.1; `11-api.md` sections 1.3, 1.7, 1.11, 1.12, 3.11.5 to 3.11.9 and 4.7; `82-security.md`
SEC-45 to SEC-48, SEC-53 and SEC-95; ADRs 0039 and 0042).

An administrator uploads files to one logical index in four steps: preflight (statuses per file,
nothing written), reserve (a document and an `uploading` version), content (the bytes streamed
into the version's blob and committed once) and submit (one `upload` run with one `ingest` item
per committed version, then one queue message). After the pre-body checks of the route class and
the schema validation, each operation runs the gate order of section 3.11.5: the index
(`404 index_not_found`), the malware-scanning gate (`503 malware_scanning_not_ready` while
scanning is enabled and no scanner canary came back clean in 48 hours), the index status
(`409 index_not_accepting_uploads` for `failed` and `deleting`), then its own checks. The four
skip the per-user write limit, because their own bounds cap them, and take no `If-Match`: the
SHA-256 constraints and the version state guard every write.

`adminUploadContent` reads `request.stream()` with no body model, so the file is never buffered
whole: it cuts the stream into 4 MiB blocks, checks the first block's signature before anything is
staged, hashes and counts every block off the event loop, stages each under a per-request nonce
and commits the block list once with `If-None-Match: *` in the Cool tier. A worker holds at most
`DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER` streams; the next one answers `429 upload_busy`.

The `documents` container and the ingestion queue come from the process's client registry
through the `upload_storage` dependency, which tests replace. The section 4 schemas it shares
with the document operations (`VersionSummary` and the references) live in `admin_common`, and
the queue message is `docchat_api.http.queue.send_run_message`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import secrets
import unicodedata
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Annotated, Any, Final, Protocol, cast
from uuid import UUID

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.storage.blob import BlobBlock, ContentSettings, StandardBlobTier
from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam
from opentelemetry.trace import Span
from pydantic import Field, StringConstraints
from starlette.requests import ClientDisconnect

from docchat_api.http.access import access
from docchat_api.http.audit import report_audit
from docchat_api.http.models import RequestModel, ResponseModel
from docchat_api.http.problems import DependencyUnavailable, ValidationFailed
from docchat_api.http.queue import IngestionQueuePort, ingestion_queue, send_run_message
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute, route_options
from docchat_api.http.state import ApiProcess, current_process
from docchat_api.routes.admin_common import DocumentRef, ProjectRef, VersionSummary, failure_message
from docchat_core.clients import ClientRegistry
from docchat_core.errors import DocChatProblem, FieldIssue
from docchat_core.ingestion.models import (
    ContentType,
    DocumentVersionState,
    IndexStatus,
    IngestionRefusedError,
    PreflightRejection,
    PreflightStatus,
    RunKind,
    SubmitOutcome,
    TraceContext,
)
from docchat_core.ingestion.names import (
    BlobContainer,
    InvalidDocumentNameError,
    document_name,
    source_path,
)
from docchat_core.ingestion.settings import UPLOAD_MAX_FILES_PER_BATCH_LIMIT
from docchat_core.models import CanonicalUuid
from docchat_core.sql.engine import SqlDeadlock, SqlTimeout, SqlUnavailable, SqlUniqueViolation
from docchat_core.sql.repositories.uploads import (
    PreflightMatch,
    UploadRefusedError,
    UploadVersion,
    check_project_mapped,
    commit_upload,
    fail_upload,
    read_preflight_matches,
    read_upload_gate,
    read_upload_version,
    reserve_upload,
    submit_uploads,
)
from docchat_core.telemetry import set_attributes, start_span
from docchat_core.telemetry.instruments import counter, histogram

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "admin-uploads"
FILE_NAME_MAX_LENGTH: Final = 300
USER_NAME_MAX_LENGTH: Final = 256
FILE_SIZE_MAX: Final = 1_099_511_627_776
"""The schema's bound of a declared size; `DOCCHAT_UPLOAD_MAX_BYTES` is checked by the handler."""
VERSION_NUMBER_MAX: Final = 2_147_483_647
FAILURE_CODE_MAX_LENGTH: Final = 64
FAILURE_MESSAGE_MAX_LENGTH: Final = 400

BLOCK_SIZE: Final = 4_194_304
"""A stream holds at most one block of file data in memory (`14-ingestion.md` section 2.5)."""
NONCE_BYTES: Final = 16
SIGNATURE_WINDOW: Final = 1024
PDF_SIGNATURE: Final = b"%PDF-"
ZIP_SIGNATURE: Final = b"PK\x03\x04"
"""`50 4B 03 04`, the local file header of a ZIP archive: refuses OLE compound files (encrypted
Office files, binary Word and PowerPoint files) named `.docx` or `.pptx`."""
OCTET_STREAM: Final = "application/octet-stream"
CONDITION_NOT_MET: Final = "ConditionNotMet"
BLOB_ALREADY_EXISTS: Final = "BlobAlreadyExists"
ALREADY_COMMITTED: Final = frozenset({CONDITION_NOT_MET, BLOB_ALREADY_EXISTS})
"""The refusals of a commit with `If-None-Match: *` whose path already holds a blob: the storage
service and Azurite answer `409 BlobAlreadyExists` (`80-data-model.md` acceptance criterion 28),
and `14-ingestion.md` section 21 names `412 ConditionNotMet`; both mean the path is written."""

CLOSED_STATUSES: Final = frozenset({IndexStatus.FAILED, IndexStatus.DELETING})
SLOTS_STATE: Final = "docchat_upload_slots"

UPLOAD_SPAN: Final = "docchat.ingestion.upload"
SUBMIT_SPAN: Final = "docchat.ingestion.submit"
UPLOADS_COMPLETED: Final = counter("docchat.uploads.completed")
UPLOADS_SIZE: Final = histogram("docchat.uploads.size")

# --- Problems --------------------------------------------------------------------------------


class IndexNotFound(DocChatProblem):
    code = "index_not_found"


class MalwareScanningNotReady(DocChatProblem):
    """`503`, `retryAfterSeconds` 3,600: no clean scanner canary within 48 hours."""

    code = "malware_scanning_not_ready"


class IndexNotAcceptingUploads(DocChatProblem):
    code = "index_not_accepting_uploads"


class BatchTooLarge(DocChatProblem):
    """`422`, its text naming `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH`."""

    code = "batch_too_large"


class InvalidFileName(DocChatProblem):
    code = "invalid_file_name"


class UnsupportedType(DocChatProblem):
    code = "unsupported_type"


class EmptyFile(DocChatProblem):
    code = "empty_file"


class ProjectNotMapped(DocChatProblem):
    code = "project_not_mapped"


class ContentUnchanged(DocChatProblem):
    code = "content_unchanged"


class DuplicateContent(DocChatProblem):
    code = "duplicate_content"


class UpdateNotConfirmed(DocChatProblem):
    code = "update_not_confirmed"


class VersionNotFound(DocChatProblem):
    code = "version_not_found"


class VersionNotUploading(DocChatProblem):
    code = "version_not_uploading"


class ContentLengthRequired(DocChatProblem):
    code = "content_length_required"


class FileTooLarge(DocChatProblem):
    code = "file_too_large"


class SizeMismatch(DocChatProblem):
    code = "size_mismatch"


class FileSignatureMismatch(DocChatProblem):
    code = "file_signature_mismatch"


class HashMismatch(DocChatProblem):
    code = "hash_mismatch"


class UploadBusy(DocChatProblem):
    """`429`, `retryAfterSeconds` 5: every stream slot of this worker is taken."""

    code = "upload_busy"


class StorageUnavailable(DocChatProblem):
    """`503`: Blob Storage failed after the client's retries."""

    code = "storage_unavailable"


_REFUSALS: Final[Mapping[str, type[DocChatProblem]]] = {
    "content_unchanged": ContentUnchanged,
    "duplicate_content": DuplicateContent,
    "update_not_confirmed": UpdateNotConfirmed,
    "index_not_found": IndexNotFound,
    "index_not_accepting_uploads": IndexNotAcceptingUploads,
}

_GATE_CODES: Final = (
    "index_not_found",
    "index_not_accepting_uploads",
    "malware_scanning_not_ready",
)

# --- Models ----------------------------------------------------------------------------------

FileName = Annotated[str, Field(min_length=1, max_length=FILE_NAME_MAX_LENGTH)]
FileSize = Annotated[int, Field(ge=0, le=FILE_SIZE_MAX)]
Sha256Text = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]
"""64 hexadecimal characters in either case; compared in lower case."""
VersionNumber = Annotated[int, Field(ge=1, le=VERSION_NUMBER_MAX)]


class UploadPreflightFile(RequestModel):
    name: FileName
    size_bytes: FileSize
    sha256: Sha256Text


class UploadPreflightRequest(RequestModel):
    files: Annotated[
        list[UploadPreflightFile], Field(min_length=1, max_length=UPLOAD_MAX_FILES_PER_BATCH_LIMIT)
    ]


class UploadPreflightResult(ResponseModel):
    """One file's status; `name` is the name after NFC and trimming."""

    name: Annotated[str, Field(max_length=FILE_NAME_MAX_LENGTH)]
    status: PreflightStatus
    reason: PreflightRejection | None
    document_id: UUID | None
    existing_document: DocumentRef | None
    current_version_number: VersionNumber | None


class UploadPreflightResponse(ResponseModel):
    results: Annotated[
        list[UploadPreflightResult],
        Field(min_length=1, max_length=UPLOAD_MAX_FILES_PER_BATCH_LIMIT),
    ]


class UploadReservationRequest(RequestModel):
    name: FileName
    size_bytes: FileSize
    sha256: Sha256Text
    project_id: CanonicalUuid
    confirm_update: bool = False


class UploadReservation(ResponseModel):
    document_id: UUID
    version_id: UUID
    version_number: VersionNumber
    reused: bool


class UploadContentResult(ResponseModel):
    version: VersionSummary


class UploadSubmitRequest(RequestModel):
    version_ids: Annotated[
        list[CanonicalUuid], Field(min_length=1, max_length=UPLOAD_MAX_FILES_PER_BATCH_LIMIT)
    ]


class UploadSubmitVersionResult(ResponseModel):
    version_id: UUID
    outcome: SubmitOutcome


class UploadSubmitResult(ResponseModel):
    run_id: UUID | None
    results: Annotated[
        list[UploadSubmitVersionResult],
        Field(min_length=1, max_length=UPLOAD_MAX_FILES_PER_BATCH_LIMIT),
    ]


def version_summary(version: UploadVersion) -> VersionSummary:
    return VersionSummary(
        version_id=version.id,
        version_number=version.version_number,
        state=version.state,
        size_bytes=version.size_bytes,
        sha256=version.sha256.hex(),
        content_type=version.content_type,
        scan_status=version.scan_status,
        failure_code=version.failure_code,
        failure_message=failure_message(version.failure_code),
        uploaded_by=version.uploaded_by,
        uploaded_by_name=version.uploaded_by_name,
        uploaded_via_project=ProjectRef(project_id=version.project_id, name=version.project_name),
        reserved_at=version.reserved_at,
        committed_at=version.committed_at,
        ingested_at=version.ingested_at,
        document_pdf_status=version.document_pdf_status,
    )


# --- Storage, stream slots and the queue -----------------------------------------------------


class DocumentBlob(Protocol):
    """The `BlobClient` calls of an upload (`14-ingestion.md` section 18.6); never a download."""

    async def stage_block(self, block_id: str, data: IO[bytes]) -> object: ...

    async def commit_block_list(
        self,
        block_list: list[BlobBlock],
        *,
        content_settings: ContentSettings,
        standard_blob_tier: StandardBlobTier,
        match_condition: MatchConditions,
    ) -> Mapping[str, Any]: ...

    async def get_blob_properties(self) -> Any: ...

    async def delete_blob(self) -> None: ...


class DocumentsContainer(Protocol):
    """The `documents` container: a blob client per path."""

    def get_blob_client(self, blob: str) -> DocumentBlob: ...


@dataclass(frozen=True, slots=True)
class UploadStorage:
    documents: DocumentsContainer
    queue: IngestionQueuePort


def upload_storage() -> UploadStorage:
    """The process's `documents` container client and `ingestion-requests` queue client, from
    the client registry the lifespan opened."""
    registry = ClientRegistry.current()
    return UploadStorage(
        # The SDK's signatures are wider than the protocols, which name only the calls made.
        documents=cast("DocumentsContainer", registry.blob_container(BlobContainer.DOCUMENTS)),
        queue=ingestion_queue(),
    )


class UploadSlots:
    """A worker's upload stream slots, taken without waiting. One event loop serves the worker
    alone, so taking and releasing need no lock."""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("a worker has at least one upload stream slot")
        self.size = size
        self.in_use = 0

    def take(self) -> bool:
        if self.in_use >= self.size:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        if self.in_use < 1:
            raise RuntimeError("no upload stream slot is taken")
        self.in_use -= 1


def upload_slots(
    request: Request, process: Annotated[ApiProcess, Depends(current_process)]
) -> UploadSlots:
    """The worker's slots, created at the first upload with
    `DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER` of them; one app serves one worker."""
    slots = getattr(request.app.state, SLOTS_STATE, None)
    if not isinstance(slots, UploadSlots):
        slots = UploadSlots(process.settings.upload.upload_concurrent_streams_per_worker)
        setattr(request.app.state, SLOTS_STATE, slots)
    return slots


# --- The gate and the file rules -------------------------------------------------------------


async def check_gate(process: ApiProcess, index_id: UUID) -> IndexStatus:
    """The gate order of `11-api.md` section 3.11.5: index, scanner canary, status."""
    gate = await read_upload_gate(
        process.sql,
        index_id=index_id,
        scanning_enabled=process.settings.upload.storage_malware_scanning_enabled,
    )
    if gate is None:
        raise IndexNotFound()
    if not gate.scanner_ready:
        raise MalwareScanningNotReady()
    if gate.status in CLOSED_STATUSES:
        raise IndexNotAcceptingUploads()
    return gate.status


def check_batch(count: int, limit: int, path: str) -> None:
    """`422 batch_too_large` above `DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH`, whose value the text
    names; the schema keeps its `maxItems` at 100."""
    if count > limit:
        raise BatchTooLarge(
            errors=[FieldIssue(path, "too_many_items")], detail_values={"maxFilesPerBatch": limit}
        )


def display_name(name: str) -> str:
    """The name after Unicode NFC and trimming: the source name an answer shows."""
    return unicodedata.normalize("NFC", name).strip()


@dataclass(frozen=True, slots=True)
class CheckedFile:
    """One preflight file after the rules that need no I/O."""

    name: str
    sha256: bytes
    normalized_name: str | None = None
    rejection: PreflightRejection | None = None


def check_file(file: UploadPreflightFile, max_bytes: int) -> CheckedFile:
    """The name rules (`invalid_name`, then `unsupported_type`), then the size (`empty_file`,
    `too_large`)."""
    sha256 = bytes.fromhex(file.sha256)
    shown = display_name(file.name)
    try:
        checked = document_name(file.name)
    except InvalidDocumentNameError as refused:
        rejection = (
            PreflightRejection.INVALID_NAME
            if refused.code == "invalid_file_name"
            else PreflightRejection.UNSUPPORTED_TYPE
        )
        return CheckedFile(shown, sha256, rejection=rejection)
    if file.size_bytes == 0:
        return CheckedFile(shown, sha256, rejection=PreflightRejection.EMPTY_FILE)
    if file.size_bytes > max_bytes:
        return CheckedFile(shown, sha256, rejection=PreflightRejection.TOO_LARGE)
    return CheckedFile(shown, sha256, normalized_name=checked.normalized_name)


def mark_duplicates(files: Sequence[CheckedFile]) -> list[CheckedFile]:
    """`duplicate_in_batch` for a file whose normalised name or SHA-256 equals an earlier file's
    of the request; only files still in the batch count, because a rejected file isn't
    uploaded."""
    names: set[str] = set()
    hashes: set[bytes] = set()
    marked: list[CheckedFile] = []
    for file in files:
        kept = file
        if file.rejection is None and file.normalized_name is not None:
            if file.normalized_name in names or file.sha256 in hashes:
                kept = CheckedFile(
                    file.name, file.sha256, rejection=PreflightRejection.DUPLICATE_IN_BATCH
                )
            else:
                names.add(file.normalized_name)
                hashes.add(file.sha256)
        marked.append(kept)
    return marked


def preflight_result(file: CheckedFile, matches: Sequence[PreflightMatch]) -> UploadPreflightResult:
    """The file's status, in the order of precedence of `14-ingestion.md` section 2.3 item 4."""
    if file.rejection is not None or file.normalized_name is None:
        return UploadPreflightResult(
            name=file.name,
            status=PreflightStatus.REJECTED,
            reason=file.rejection,
            document_id=None,
            existing_document=None,
            current_version_number=None,
        )
    same_name = [
        match
        for match in matches
        if match.match_kind == "name" and match.normalized_name == file.normalized_name
    ]
    document_id = same_name[0].document_id if same_name else None
    current = next((match.version_number for match in same_name if match.is_current), None)
    held = {match.sha256 for match in same_name if match.sha256 is not None}
    elsewhere = next(
        (
            match
            for match in matches
            if match.match_kind == "hash"
            and match.sha256 == file.sha256
            and match.document_id != document_id
        ),
        None,
    )
    existing = None
    if file.sha256 in held:
        status = PreflightStatus.UNCHANGED
    elif elsewhere is not None:
        status = PreflightStatus.DUPLICATE
        existing = DocumentRef(document_id=elsewhere.document_id, name=elsewhere.source_name)
    elif held:
        status = PreflightStatus.UPDATE
    else:
        status = PreflightStatus.NEW
        document_id = None
        current = None
    return UploadPreflightResult(
        name=file.name,
        status=status,
        reason=None,
        document_id=document_id,
        existing_document=existing,
        current_version_number=current,
    )


# --- adminPreflightUploads -------------------------------------------------------------------

Process = Annotated[ApiProcess, Depends(current_process)]

EXAMPLE_SHA256: Final = "73b42435e5b4f2d1236c0444208788574db695cc1d77f6a9a7899020f3d44f06"


def _request_example(example: Mapping[str, Any]) -> dict[str, Any]:
    return {"requestBody": {"content": {"application/json": {"example": dict(example)}}}}


@router.post(
    "/admin/indexes/{indexId}/uploads/preflight",
    operation_id="adminPreflightUploads",
    tags=[TAG],
    response_model=UploadPreflightResponse,
    openapi_extra=_request_example(
        {"files": [{"name": "travel-policy.pdf", "sizeBytes": 482113, "sha256": EXAMPLE_SHA256}]}
    ),
)
@route_options(write_limited=False)
@access("admin", scoped_params=["indexId"], error_codes=[*_GATE_CODES, "batch_too_large"])
async def admin_preflight_uploads(
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    body: UploadPreflightRequest,
    process: Process,
) -> UploadPreflightResponse:
    upload = process.settings.upload
    await check_gate(process, index_id)
    check_batch(len(body.files), upload.upload_max_files_per_batch, "body.files")
    files = mark_duplicates([check_file(file, upload.upload_max_bytes) for file in body.files])
    kept = [file for file in files if file.rejection is None and file.normalized_name is not None]
    matches: list[PreflightMatch] = []
    if kept:
        matches = await read_preflight_matches(
            process.sql,
            index_id=index_id,
            names=[file.normalized_name for file in kept if file.normalized_name is not None],
            hashes=[file.sha256 for file in kept],
        )
    return UploadPreflightResponse(results=[preflight_result(file, matches) for file in files])


# --- adminReserveUpload ----------------------------------------------------------------------


@router.post(
    "/admin/indexes/{indexId}/uploads",
    operation_id="adminReserveUpload",
    tags=[TAG],
    response_model=UploadReservation,
    status_code=201,
    openapi_extra=_request_example(
        {
            "name": "travel-policy.pdf",
            "sizeBytes": 482113,
            "sha256": EXAMPLE_SHA256,
            "projectId": "ef87ff47-57f2-42a1-8c93-4ea2bca4792c",
            "confirmUpdate": False,
        }
    ),
)
@route_options(write_limited=False)
@access(
    "admin",
    scoped_params=["indexId", "body:/projectId"],
    error_codes=[
        *_GATE_CODES,
        "content_unchanged",
        "duplicate_content",
        "update_not_confirmed",
        "invalid_file_name",
        "unsupported_type",
        "empty_file",
        "project_not_mapped",
    ],
)
async def admin_reserve_upload(
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    body: UploadReservationRequest,
    principal: CurrentPrincipal,
    process: Process,
) -> UploadReservation:
    upload = process.settings.upload
    await check_gate(process, index_id)
    try:
        name = document_name(body.name)
    except InvalidDocumentNameError as refused:
        problem = InvalidFileName if refused.code == "invalid_file_name" else UnsupportedType
        raise problem(errors=[FieldIssue("body.name", refused.reason)]) from None
    size_issue = [FieldIssue("body.sizeBytes", "out_of_range")]
    if body.size_bytes == 0:
        raise EmptyFile(errors=size_issue)
    if body.size_bytes > upload.upload_max_bytes:
        raise ValidationFailed(errors=size_issue)
    if not await check_project_mapped(process.sql, project_id=body.project_id, index_id=index_id):
        raise ProjectNotMapped(errors=[FieldIssue("body.projectId", "not_allowed")])
    try:
        reservation = await reserve_upload(
            process.sql,
            index_id=index_id,
            name=name,
            size_bytes=body.size_bytes,
            sha256=bytes.fromhex(body.sha256),
            project_id=body.project_id,
            oid=principal.oid,
            confirm_update=body.confirm_update,
            scan_required=upload.storage_malware_scanning_enabled,
        )
    except UploadRefusedError as refusal:
        raise _REFUSALS[refusal.code]() from None
    except (SqlUniqueViolation, SqlDeadlock):
        # A second race on the name, or a deadlock the unit wasn't retried for: the
        # transaction rolled back, so nothing is half-done.
        raise DependencyUnavailable() from None
    return UploadReservation(
        document_id=reservation.document_id,
        version_id=reservation.version_id,
        version_number=reservation.version_number,
        reused=reservation.reused,
    )


# --- adminUploadContent ----------------------------------------------------------------------


def block_id(nonce: str, index: int) -> str:
    """`<nonce>-<block index as 8 zero-padded digits>`: 41 characters, the same length for every
    block, so two concurrent streams for one version stage disjoint blocks. The Blob client sends
    it base64-encoded, under the service's 64-byte limit."""
    return f"{nonce}-{index:08d}"


def wire_block_id(nonce: str, index: int) -> str:
    """The block ID as the service receives it: the base64 of `block_id`."""
    return base64.b64encode(block_id(nonce, index).encode("ascii")).decode("ascii")


def signature_matches(first_block: bytes, content_type: ContentType) -> bool:
    """PDF: `%PDF-` within the first 1,024 bytes; DOCX and PPTX: a ZIP local file header at
    offset 0."""
    if content_type is ContentType.PDF:
        return PDF_SIGNATURE in first_block[:SIGNATURE_WINDOW]
    return first_block.startswith(ZIP_SIGNATURE)


@dataclass(slots=True)
class ReceivedFile:
    """What a stream delivered: the staged block IDs, the byte count and the SHA-256."""

    block_ids: list[str] = field(default_factory=list)
    size: int = 0
    sha256: bytes = b""


async def blocks_of(chunks: AsyncIterator[bytes], *, declared: int) -> AsyncIterator[bytes]:
    """Cut the body into blocks of 4,194,304 bytes (the last one shorter), copying each byte
    once, so at most one block and the chunks of the next are held. A byte count above the
    declared size stops the stream with `422 size_mismatch`."""
    pending: list[bytes] = []
    held = 0
    received = 0
    async for chunk in chunks:
        if not chunk:
            continue
        received += len(chunk)
        if received > declared:
            raise SizeMismatch(errors=[FieldIssue("body", "out_of_range")])
        rest = memoryview(chunk)
        while held + len(rest) >= BLOCK_SIZE:
            need = BLOCK_SIZE - held
            pending.append(bytes(rest[:need]))
            rest = rest[need:]
            block = b"".join(pending)
            pending.clear()
            held = 0
            yield block
            del block
        if rest:
            pending.append(chunk if len(rest) == len(chunk) else bytes(rest))
            held += len(rest)
    if held:
        yield b"".join(pending)


async def receive_file(
    chunks: AsyncIterator[bytes],
    blob: DocumentBlob,
    *,
    declared: int,
    content_type: ContentType,
    nonce: str,
) -> ReceivedFile:
    """Check the first block's signature before anything is staged, then hash, count and stage
    each block; Blob errors after the client's retries are `503 storage_unavailable`."""
    received = ReceivedFile()
    hasher = hashlib.sha256()
    index = 0
    async for block in blocks_of(chunks, declared=declared):
        if index == 0 and not signature_matches(block, content_type):
            raise FileSignatureMismatch(errors=[FieldIssue("body", "invalid_format")])
        # Hashing 4 MiB takes milliseconds of CPU, which the event loop can't spare.
        await asyncio.to_thread(hasher.update, block)
        received.size += len(block)
        identifier = block_id(nonce, index)
        try:
            # A file object, not the bytes: the HTTP client warns that a body over 1 MiB passed
            # as bytes is written in one call, and writes a file object in chunks, yielding the
            # loop between them. `BytesIO` shares the block's buffer, so nothing is copied.
            await blob.stage_block(identifier, io.BytesIO(block))
        except AzureError:
            raise StorageUnavailable() from None
        received.block_ids.append(identifier)
        index += 1
        # Released before the next block is assembled, so two are never held at once.
        del block
    received.sha256 = hasher.digest()
    return received


async def commit_blob(
    blob: DocumentBlob, block_ids: Sequence[str], content_type: ContentType
) -> datetime:
    """Commit the block list once: `If-None-Match: *`, so a version's path is written once, and
    `x-ms-access-tier: Cool`. A refusal of `ALREADY_COMMITTED` means an earlier attempt or a
    concurrent stream already committed these exact bytes (only bytes that hashed to the reserved
    SHA-256 reach a commit), so the blob's `last_modified` is read instead."""
    try:
        committed = await blob.commit_block_list(
            [BlobBlock(block_id=identifier) for identifier in block_ids],
            content_settings=ContentSettings(content_type=content_type.value),
            standard_blob_tier=StandardBlobTier.COOL,
            match_condition=MatchConditions.IfMissing,
        )
    except (ResourceModifiedError, ResourceExistsError) as refused:
        # The storage client sets `error_code` on the errors it raises from a response.
        if getattr(refused, "error_code", None) not in ALREADY_COMMITTED:
            raise StorageUnavailable() from None
    except AzureError:
        raise StorageUnavailable() from None
    else:
        last_modified: datetime = committed["last_modified"]
        return last_modified
    try:
        properties = await blob.get_blob_properties()
    except AzureError:
        raise StorageUnavailable() from None
    existing: datetime = properties.last_modified
    return existing


async def discard_blob(blob: DocumentBlob) -> None:
    """Delete a blob whose commit SQL refused; an absent blob counts as deleted. A failed delete
    is left to the purge of failed versions (`14-ingestion.md` section 5.3)."""
    try:
        await blob.delete_blob()
    except ResourceNotFoundError:
        return
    except AzureError:
        return


def declared_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None or not value.isascii() or not value.isdigit():
        return None
    return int(value)


@dataclass(slots=True)
class UploadReport:
    """What the upload span and the two metrics record once the request ends."""

    outcome: str = "failed"
    error_code: str | None = None
    size: int | None = None


def _finish_upload(span: Span, report: UploadReport, content_type: ContentType) -> None:
    attributes: dict[str, object] = {"docchat.outcome": report.outcome}
    if report.error_code is not None:
        attributes["docchat.error_code"] = report.error_code
    if report.size is not None:
        attributes["docchat.ingestion.upload_bytes"] = report.size
        UPLOADS_SIZE.record(report.size, content_type=content_type.value)
    set_attributes(span, **attributes)
    if report.outcome == "ok" or report.error_code is not None:
        UPLOADS_COMPLETED.add(1, outcome=report.outcome, error_code=report.error_code or "none")


async def _store_and_commit(
    request: Request,
    version: UploadVersion,
    *,
    process: ApiProcess,
    storage: UploadStorage,
    actor: UUID,
    report: UploadReport,
) -> VersionSummary:
    blob = storage.documents.get_blob_client(
        source_path(version.index_id, version.document_id, version.version_number)
    )
    received = await receive_file(
        request.stream(),
        blob,
        declared=version.size_bytes,
        content_type=version.content_type,
        nonce=secrets.token_hex(NONCE_BYTES),
    )
    if received.size != version.size_bytes:
        raise SizeMismatch(errors=[FieldIssue("body", "out_of_range")])
    if received.sha256 != version.sha256:
        raise HashMismatch(errors=[FieldIssue("body", "invalid_format")])
    last_modified = await commit_blob(blob, received.block_ids, version.content_type)
    report.size = received.size
    result = await commit_upload(
        process.sql, version=version, last_modified=last_modified, actor=actor
    )
    if result.outcome == "duplicate" and result.code is not None:
        await discard_blob(blob)
        # When the mark fails, the version stays `uploading` and the stale-upload sweep
        # removes it.
        with contextlib.suppress(SqlUnavailable, SqlTimeout):
            await fail_upload(process.sql, version_id=version.id, code=result.code)
        raise _REFUSALS[result.code]()
    if result.outcome == "not_uploading" or result.version is None:
        # A document delete failed the version meanwhile; its clean-up item purges the blob.
        raise VersionNotUploading()
    report_audit(request, result.events, admin=True)
    return version_summary(result.version)


@router.put(
    "/admin/indexes/{indexId}/uploads/{versionId}/content",
    operation_id="adminUploadContent",
    tags=[TAG],
    response_model=UploadContentResult,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {OCTET_STREAM: {"schema": {"type": "string", "format": "binary"}}},
        },
        "parameters": [
            {
                "name": "Content-Length",
                "in": "header",
                "required": True,
                "schema": {"type": "integer", "minimum": 1},
            }
        ],
    },
)
@route_options(content_types=(OCTET_STREAM,), body_limit=None, write_limited=False)
@access(
    "admin",
    scoped_params=["indexId", "versionId"],
    error_codes=[
        *_GATE_CODES,
        "version_not_found",
        "version_not_uploading",
        "content_unchanged",
        "duplicate_content",
        "content_length_required",
        "file_too_large",
        "unsupported_media_type",
        "size_mismatch",
        "file_signature_mismatch",
        "hash_mismatch",
        "upload_busy",
        "storage_unavailable",
    ],
)
async def admin_upload_content(
    *,
    request: Request,
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    version_id: Annotated[CanonicalUuid, PathParam(alias="versionId")],
    principal: CurrentPrincipal,
    process: Process,
    storage: Annotated[UploadStorage, Depends(upload_storage)],
    slots: Annotated[UploadSlots, Depends(upload_slots)],
) -> UploadContentResult:
    await check_gate(process, index_id)
    version = await read_upload_version(process.sql, index_id=index_id, version_id=version_id)
    if version is None:
        raise VersionNotFound()
    report = UploadReport()
    with start_span(
        UPLOAD_SPAN,
        **{
            "docchat.index_id": str(index_id),
            "docchat.document_id": str(version.document_id),
            "docchat.version_id": str(version.id),
        },
    ) as span:
        try:
            summary = await _upload(
                request,
                version,
                process=process,
                storage=storage,
                slots=slots,
                actor=principal.oid,
                report=report,
            )
        except ClientDisconnect:
            # Nothing was committed; the version stays `uploading` for a retry.
            report.outcome = "failed"
            raise
        except DocChatProblem as problem:
            report.error_code = problem.entry.code
            raise
        except (SqlUnavailable, SqlTimeout):
            report.error_code = DependencyUnavailable.code
            raise
        except Exception:
            report.error_code = "internal_error"
            raise
        else:
            report.outcome = "ok"
        finally:
            _finish_upload(span, report, version.content_type)
    return UploadContentResult(version=summary)


async def _upload(
    request: Request,
    version: UploadVersion,
    *,
    process: ApiProcess,
    storage: UploadStorage,
    slots: UploadSlots,
    actor: UUID,
    report: UploadReport,
) -> VersionSummary:
    """The checks before the first byte is read, then the stream and the commit inside a stream
    slot, released whatever ends the request."""
    if version.state is DocumentVersionState.PENDING:
        # A retried request after a committed attempt: answered without reading the body.
        return version_summary(version)
    if version.state is not DocumentVersionState.UPLOADING:
        raise VersionNotUploading()
    declared = declared_length(request)
    if declared is None:
        raise ContentLengthRequired()
    if declared > process.settings.upload.upload_max_bytes:
        raise FileTooLarge()
    if declared != version.size_bytes:
        raise SizeMismatch(errors=[FieldIssue("header.Content-Length", "out_of_range")])
    if not slots.take():
        raise UploadBusy()
    try:
        return await _store_and_commit(
            request,
            version,
            process=process,
            storage=storage,
            actor=actor,
            report=report,
        )
    finally:
        slots.release()


# --- adminSubmitUploads ----------------------------------------------------------------------


def duplicate_issues(version_ids: Sequence[UUID]) -> list[FieldIssue]:
    """`duplicate` at `body.versionIds.<i>` for each repeat of an earlier ID."""
    seen: set[UUID] = set()
    issues: list[FieldIssue] = []
    for position, version_id in enumerate(version_ids):
        if version_id in seen:
            issues.append(FieldIssue(f"body.versionIds.{position}", "duplicate"))
        seen.add(version_id)
    return issues


@router.post(
    "/admin/indexes/{indexId}/uploads/submit",
    operation_id="adminSubmitUploads",
    tags=[TAG],
    response_model=UploadSubmitResult,
    status_code=202,
    responses={200: {"model": UploadSubmitResult, "description": "Nothing was queued"}},
    openapi_extra=_request_example({"versionIds": ["fd19225e-a461-49e2-9a2f-87b737e1f304"]}),
)
@route_options(write_limited=False)
@access(
    "admin",
    scoped_params=["indexId", "body:/versionIds"],
    error_codes=[*_GATE_CODES, "batch_too_large"],
)
async def admin_submit_uploads(
    *,
    request: Request,
    response: Response,
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    body: UploadSubmitRequest,
    principal: CurrentPrincipal,
    process: Process,
    storage: Annotated[UploadStorage, Depends(upload_storage)],
) -> UploadSubmitResult:
    issues = duplicate_issues(body.version_ids)
    if issues:
        raise ValidationFailed(errors=issues)
    await check_gate(process, index_id)
    check_batch(
        len(body.version_ids), process.settings.upload.upload_max_files_per_batch, "body.versionIds"
    )
    with start_span(
        SUBMIT_SPAN,
        **{"docchat.index_id": str(index_id), "docchat.ingestion.run_kind": RunKind.UPLOAD.value},
    ) as span:
        # Injected here, so the run's context and the message's parent is this span.
        trace = TraceContext.current()
        try:
            submitted = await submit_uploads(
                process.sql,
                index_id=index_id,
                version_ids=body.version_ids,
                actor=principal.oid,
                trace=trace,
            )
        except IngestionRefusedError as refusal:
            raise _REFUSALS[refusal.code]() from None
        result = submitted.result
        set_attributes(span, **{"docchat.ingestion.item_count": result.item_count})
        if result.run_id is not None:
            set_attributes(span, **{"docchat.run_id": str(result.run_id)})
            report_audit(request, submitted.events, admin=True)
            await send_run_message(storage.queue, result.run_id, trace)
        else:
            response.status_code = 200
    return UploadSubmitResult(
        run_id=result.run_id,
        results=[
            UploadSubmitVersionResult(version_id=item.version_id, outcome=item.outcome)
            for item in result.results
        ],
    )
