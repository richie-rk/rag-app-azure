"""The statements of the four upload operations (`14-ingestion.md` sections 2.1 to 2.6, 5.5 and
16.1; `11-api.md` sections 3.11.5 to 3.11.9; `80-data-model.md` sections 2.12, 2.13, 2.19 and
2.22; ADRs 0039, 0040 and 0042).

- **The gate** (`read_upload_gate`): the logical index's status and, while scanning is enabled,
  whether a scanner canary came back clean in the last 48 hours (one seek on
  `IX_scan_canaries_written_at`).
- **Preflight** (`read_preflight_matches`): one read-only `UNION ALL` of the same-name documents
  with their active versions and the active versions holding one of the hashes; the API decides
  each file's status from the rows.
- **Reserve** (`check_project_mapped`, `reserve_upload`): the project check, then one transaction
  that finds or creates the document under its update lock and reuses or inserts an `uploading`
  version; a race between two first reservations of a name loses on `UQ_documents_index_name`,
  and the loser re-runs once.
- **Content** (`read_upload_version`, `commit_upload`, `fail_upload`): the version as the request
  needs it (with the uploader's name and the project's for `VersionSummary`), the state change to
  `pending` with its audit row, and the failure mark after a SHA-256 index refused the commit.
- **Submit** (`submit_uploads`): `create_upload_run` and the audit row of the run.

Every function takes the `SqlEngine` first and its parameters as keywords and carries
`@request_path`, so the plan audit runs each once. State values the filtered indexes cover stay
literals in the text (`N'uploading'`), so SQL Server matches the filtered index; every value from
a request is a bound parameter. A refusal raised inside a transaction rolls it back, so nothing is
half-done. `app.document_versions` has an `INSERT` trigger, so no statement here reads it back
with an `OUTPUT` clause; a changed row is read with a second `SELECT` in the same transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import (
    BINARY,
    Connection,
    and_,
    bindparam,
    case,
    cast,
    func,
    insert,
    literal_column,
    null,
    select,
    union_all,
    update,
)
from sqlalchemy.dialects.mssql import BIT
from sqlalchemy.sql.dml import ReturningUpdate

from docchat_core.audit.models import (
    AuditEvent,
    DocumentUploadCommittedMetadata,
    IngestionRunSubmittedMetadata,
)
from docchat_core.audit.writer import write_audit_event
from docchat_core.ingestion.models import (
    ACTIVE_VERSION_STATES,
    ContentType,
    DocumentPdfStatus,
    DocumentVersionState,
    IndexStatus,
    ScanStatus,
    TraceContext,
)
from docchat_core.ingestion.names import DocumentName, source_path
from docchat_core.ingestion.runs import LOCK_ROW, UploadRunResult, create_upload_run
from docchat_core.sql.engine import SqlEngine, SqlUniqueViolation, request_path
from docchat_core.sql.repositories.common import ONE, ZERO, utc
from docchat_core.sql.tables.access import project_indexes, projects, users
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    scan_canaries,
    search_indexes,
)
from docchat_core.sql.tables.schema import BIT_FALSE, SYSUTCDATETIME, nvarchar_literal

# Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics.
UPLOAD_GATE: Final = "upload_gate"
PREFLIGHT_MATCHES: Final = "preflight_matches"
PROJECT_MAPPED: Final = "project_mapped"
RESERVE_UPLOAD: Final = "reserve_upload"
UPLOAD_VERSION: Final = "upload_version"
COMMIT_UPLOAD: Final = "commit_upload"
FAIL_UPLOAD: Final = "fail_upload"
SUBMIT_UPLOADS: Final = "submit_uploads"

CANARY_WINDOW_HOURS: Final = 48
"""A clean scanner canary keeps uploads open this long, so one missed daily canary is tolerated
(`14-ingestion.md` section 5.5 item 3)."""

PREFLIGHT_LIST_MAX: Final = 100
"""At most this many names, and as many hashes, are bound in the preflight query."""

DOCUMENT_NAME_KEY: Final = "UQ_documents_index_name"
DOCUMENT_SHA_KEY: Final = "UX_document_versions_document_sha"
INDEX_SHA_KEY: Final = "UX_document_versions_index_sha"

type ReserveRefusal = Literal["content_unchanged", "update_not_confirmed", "duplicate_content"]
type DuplicateCode = Literal["content_unchanged", "duplicate_content"]

DUPLICATE_CODES: Final[dict[str, DuplicateCode]] = {
    DOCUMENT_SHA_KEY: "content_unchanged",
    INDEX_SHA_KEY: "duplicate_content",
}
"""The code each filtered SHA-256 index gives a refused commit (`14-ingestion.md` 2.5 step 7)."""

_ACTIVE: Final = [nvarchar_literal(state) for state in ACTIVE_VERSION_STATES]
_UPLOADING: Final = nvarchar_literal(DocumentVersionState.UPLOADING)
_FAILED: Final = nvarchar_literal(DocumentVersionState.FAILED)
_PENDING: Final = nvarchar_literal(DocumentVersionState.PENDING)

# Fixed identifiers for the plan audit; a plan doesn't depend on whether the rows exist.
_EXAMPLE_INDEX: Final = UUID("4c1e7a52-9d3b-4f60-8a2e-5b6c7d8e9f01")
_EXAMPLE_PROJECT: Final = UUID("5d2f8b63-ae4c-4071-9b3f-6c7d8e9fa012")
_EXAMPLE_DOCUMENT: Final = UUID("6e3a9c74-bf5d-4182-8c4a-7d8e9fab1023")
_EXAMPLE_VERSION: Final = UUID("7f4bad85-c06e-4293-9d5b-8e9fabc12034")
_EXAMPLE_USER: Final = UUID("80a5be96-d17f-43a4-8e6c-9fabcd231045")
_EXAMPLE_SHA256: Final = bytes(range(32))
_EXAMPLE_TIME: Final = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
_EXAMPLE_NAME: Final = DocumentName("travel-policy.pdf", "travel-policy.pdf", ContentType.PDF)
_EXAMPLE_TRACE: Final = TraceContext("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")


class UploadRefusedError(Exception):
    """A reservation refused by the document's versions (`14-ingestion.md` section 2.4 steps 5
    and 6): `code` is the problem code. Raised inside the transaction, it rolls it back."""

    def __init__(self, code: ReserveRefusal) -> None:
        # The text is the code alone, so logging the exception can't leak a value.
        super().__init__(code)
        self.code: ReserveRefusal = code


def sql_time(value: datetime) -> datetime:
    """An aware instant as the naive UTC value a `DATETIME2(3)` column stores."""
    if value.tzinfo is None:
        raise ValueError("a naive datetime has no defined instant")
    return value.astimezone(UTC).replace(tzinfo=None)


# --- The gate ---------------------------------------------------------------------------------

INDEX_STATUS: Final = select(search_indexes.c.status).where(
    search_indexes.c.id == bindparam("index_id")
)

CLEAN_CANARY: Final = (
    select(ONE)
    .where(
        scan_canaries.c.result == nvarchar_literal("clean"),
        scan_canaries.c.written_at
        > func.dateadd(
            literal_column("hour"), literal_column(f"-{CANARY_WINDOW_HOURS}"), SYSUTCDATETIME
        ),
    )
    .limit(1)
)
"""`SELECT TOP (1) 1 FROM app.scan_canaries WHERE result = N'clean' AND written_at >
DATEADD(hour, -48, SYSUTCDATETIME())`: one seek on `IX_scan_canaries_written_at`."""


@dataclass(frozen=True, slots=True)
class UploadGate:
    """The logical index's status and whether the scanner is proven to work (always `True`
    while scanning is disabled)."""

    status: IndexStatus
    scanner_ready: bool


def _gate(connection: Connection, *, index_id: UUID, scanning_enabled: bool) -> UploadGate | None:
    status = connection.execute(INDEX_STATUS, {"index_id": index_id}).scalar_one_or_none()
    ready = True
    if scanning_enabled:
        ready = connection.execute(CLEAN_CANARY).first() is not None
    if status is None or status == IndexStatus.DELETED:
        return None
    return UploadGate(IndexStatus(status), ready)


@request_path(example_params={"index_id": _EXAMPLE_INDEX, "scanning_enabled": True})
async def read_upload_gate(
    engine: SqlEngine, *, index_id: UUID, scanning_enabled: bool
) -> UploadGate | None:
    """The index and the scan gate in one round trip; `None` when the index is missing or
    `deleted`. The caller answers the index, then the gate, then the status."""
    work = partial(_gate, index_id=index_id, scanning_enabled=scanning_enabled)
    return await engine.run(work, name=UPLOAD_GATE, retry="transient")


# --- Preflight --------------------------------------------------------------------------------


def _preflight() -> Any:
    d = documents.alias("d")
    v = document_versions.alias("v")
    active = v.c.state.in_(_ACTIVE)
    is_current = cast(case((v.c.id == d.c.current_version_id, ONE), else_=ZERO), BIT)
    by_name = (
        select(
            nvarchar_literal("name").label("match_kind"),
            d.c.id.label("document_id"),
            d.c.normalized_name,
            d.c.source_name,
            v.c.sha256,
            v.c.version_number,
            is_current.label("is_current"),
        )
        .select_from(d.outerjoin(v, and_(v.c.document_id == d.c.id, active)))
        .where(
            d.c.search_index_id == bindparam("index_id"),
            d.c.is_deleted == BIT_FALSE,
            d.c.normalized_name.in_(bindparam("names", expanding=True)),
        )
    )
    by_hash = (
        select(
            nvarchar_literal("hash"),
            v.c.document_id,
            d.c.normalized_name,
            d.c.source_name,
            v.c.sha256,
            v.c.version_number,
            is_current,
        )
        .select_from(v.join(d, d.c.id == v.c.document_id))
        .where(
            v.c.search_index_id == bindparam("index_id"),
            v.c.sha256.in_(bindparam("hashes", type_=BINARY(32), expanding=True)),
            active,
            d.c.is_deleted == BIT_FALSE,
        )
    )
    return union_all(by_name, by_hash)


PREFLIGHT_QUERY: Final = _preflight()
"""The read-only query of `14-ingestion.md` section 2.3 step 3: the first branch seeks
`UQ_documents_index_name` and the filtered `UX_document_versions_document_sha`, the second the
filtered `UX_document_versions_index_sha`."""


@dataclass(frozen=True, slots=True)
class PreflightMatch:
    """One row of the preflight query. A `name` row is a live document with the file's name and
    one of its active versions (no version: `sha256` and `version_number` are `None`); a `hash`
    row is an active version of the index holding one of the files' bytes."""

    match_kind: Literal["name", "hash"]
    document_id: UUID
    normalized_name: str
    source_name: str
    sha256: bytes | None
    version_number: int | None
    is_current: bool


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "names": ["travel-policy.pdf"],
        "hashes": [_EXAMPLE_SHA256],
    }
)
async def read_preflight_matches(
    engine: SqlEngine, *, index_id: UUID, names: Sequence[str], hashes: Sequence[bytes]
) -> list[PreflightMatch]:
    """The rows behind the preflight statuses, for at most 100 names and 100 hashes."""
    if not 1 <= len(names) <= PREFLIGHT_LIST_MAX or not 1 <= len(hashes) <= PREFLIGHT_LIST_MAX:
        raise ValueError("a preflight query binds 1 to 100 names and 1 to 100 hashes")
    rows = await engine.fetch_all(
        PREFLIGHT_QUERY,
        {"index_id": index_id, "names": list(names), "hashes": list(hashes)},
        name=PREFLIGHT_MATCHES,
    )
    return [
        PreflightMatch(
            match_kind=row.match_kind,
            document_id=row.document_id,
            normalized_name=row.normalized_name,
            source_name=row.source_name,
            sha256=None if row.sha256 is None else bytes(row.sha256),
            version_number=row.version_number,
            is_current=row.is_current == 1,
        )
        for row in rows
    ]


# --- Reserve ----------------------------------------------------------------------------------


def _project_mapped() -> Any:
    pi = project_indexes.alias("pi")
    p = projects.alias("p")
    return (
        select(ONE)
        .select_from(pi.join(p, p.c.id == pi.c.project_id))
        .where(
            pi.c.project_id == bindparam("project_id"),
            pi.c.search_index_id == bindparam("index_id"),
            p.c.status == nvarchar_literal("active"),
        )
    )


PROJECT_MAPPED_QUERY: Final = _project_mapped()
"""The reservation's project check (`14-ingestion.md` section 2.4 step 2)."""


@request_path(example_params={"project_id": _EXAMPLE_PROJECT, "index_id": _EXAMPLE_INDEX})
async def check_project_mapped(engine: SqlEngine, *, project_id: UUID, index_id: UUID) -> bool:
    """Whether `project_id` is an active project mapped to the index."""
    row = await engine.fetch_one(
        PROJECT_MAPPED_QUERY, {"project_id": project_id, "index_id": index_id}, name=PROJECT_MAPPED
    )
    return row is not None


DOCUMENT_BY_NAME: Final = (
    select(documents.c.id, documents.c.is_deleted, documents.c.source_name)
    .with_hint(documents, LOCK_ROW, "mssql")
    .where(
        documents.c.search_index_id == bindparam("index_id"),
        documents.c.normalized_name == bindparam("normalized_name"),
    )
)
"""Step 1: the document of the name, under its update lock."""

INSERT_DOCUMENT: Final = insert(documents).values(
    id=bindparam("document_id"),
    search_index_id=bindparam("index_id"),
    source_name=bindparam("source_name"),
    normalized_name=bindparam("normalized_name"),
    next_version_number=literal_column("1"),
    created_by=bindparam("oid"),
    updated_by=bindparam("oid"),
)
"""Step 2: the first reservation of the name creates the document under a new UUID."""

RESTORE_DOCUMENT: Final = (
    update(documents)
    .where(documents.c.id == bindparam("document_id"))
    .values(
        is_deleted=BIT_FALSE,
        deleted_at=null(),
        deleted_by=null(),
        source_name=bindparam("source_name"),
        updated_at=SYSUTCDATETIME,
        updated_by=bindparam("oid"),
    )
)
"""Step 3: a deleted name comes back under its document ID (ADR 0040)."""

RENAME_DOCUMENT: Final = (
    update(documents)
    .where(documents.c.id == bindparam("document_id"))
    .values(
        source_name=bindparam("source_name"),
        updated_at=SYSUTCDATETIME,
        updated_by=bindparam("oid"),
    )
)
"""Step 4: the display name follows the reservation's casing and spacing."""

ACTIVE_VERSIONS: Final = select(document_versions.c.id, document_versions.c.sha256).where(
    document_versions.c.document_id == bindparam("document_id"),
    document_versions.c.state.in_(_ACTIVE),
)
"""Step 5: the document's `pending`, `ingesting` and `ingested` versions."""

DUPLICATE_ELSEWHERE: Final = (
    select(document_versions.c.document_id)
    .where(
        document_versions.c.search_index_id == bindparam("index_id"),
        document_versions.c.sha256 == bindparam("sha256"),
        document_versions.c.state.in_(_ACTIVE),
        document_versions.c.document_id != bindparam("document_id"),
    )
    .limit(1)
)
"""Step 6: another live document of the index holding these bytes."""

OPEN_UPLOAD: Final = (
    select(document_versions.c.id, document_versions.c.version_number)
    .with_hint(document_versions, LOCK_ROW, "mssql")
    .where(
        document_versions.c.document_id == bindparam("document_id"),
        document_versions.c.state == _UPLOADING,
        document_versions.c.sha256 == bindparam("sha256"),
        document_versions.c.scan_required == bindparam("scan_required"),
    )
)
"""Step 7: an `uploading` version of the same bytes under the same scanning setting."""

REFRESH_UPLOAD: Final = (
    update(document_versions)
    .where(
        document_versions.c.id == bindparam("version_id"),
        document_versions.c.state == _UPLOADING,
    )
    .values(
        reserved_at=SYSUTCDATETIME,
        uploaded_by=bindparam("oid"),
        uploaded_via_project_id=bindparam("project_id"),
    )
)

NEXT_VERSION_NUMBER: Final[ReturningUpdate[Any]] = (
    update(documents)
    .where(documents.c.id == bindparam("document_id"))
    .values(next_version_number=documents.c.next_version_number + literal_column("1"))
    .returning(literal_column("deleted.next_version_number"))
)
"""Step 8: the number is taken under the document's update lock, so it is never reused."""

INSERT_VERSION: Final = insert(document_versions).values(
    id=bindparam("version_id"),
    document_id=bindparam("document_id"),
    search_index_id=bindparam("index_id"),
    version_number=bindparam("version_number"),
    sha256=bindparam("sha256"),
    size_bytes=bindparam("size_bytes"),
    content_type=bindparam("content_type"),
    source_name=bindparam("source_name"),
    blob_path=bindparam("blob_path"),
    uploaded_by=bindparam("oid"),
    uploaded_via_project_id=bindparam("project_id"),
    state=_UPLOADING,
    scan_required=bindparam("scan_required"),
    scan_status=bindparam("scan_status"),
)
"""Step 8: the new `uploading` version. The scan columns are set here and nowhere else in the
API; the insert trigger admits only `awaiting` with `scan_required = 1` or `not_required` with
`0`."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """`adminReserveUpload`'s answer."""

    document_id: UUID
    version_id: UUID
    version_number: int
    reused: bool


def _reserve(
    connection: Connection,
    *,
    index_id: UUID,
    name: DocumentName,
    size_bytes: int,
    sha256: bytes,
    project_id: UUID,
    oid: UUID,
    confirm_update: bool,
    scan_required: bool,
) -> Reservation:
    keys = {"index_id": index_id, "normalized_name": name.normalized_name}
    found = connection.execute(DOCUMENT_BY_NAME, keys).first()
    if found is None:
        document_id = uuid4()
        connection.execute(
            INSERT_DOCUMENT,
            keys | {"document_id": document_id, "source_name": name.source_name, "oid": oid},
        )
    else:
        document_id = found.id
        renamed = {"document_id": document_id, "source_name": name.source_name, "oid": oid}
        if found.is_deleted:
            connection.execute(RESTORE_DOCUMENT, renamed)
        elif found.source_name != name.source_name:
            connection.execute(RENAME_DOCUMENT, renamed)
    active = connection.execute(ACTIVE_VERSIONS, {"document_id": document_id}).all()
    if any(bytes(row.sha256) == sha256 for row in active):
        raise UploadRefusedError("content_unchanged")
    if active and not confirm_update:
        raise UploadRefusedError("update_not_confirmed")
    bytes_of = {"index_id": index_id, "document_id": document_id, "sha256": sha256}
    if connection.execute(DUPLICATE_ELSEWHERE, bytes_of).first() is not None:
        raise UploadRefusedError("duplicate_content")
    reuse = connection.execute(
        OPEN_UPLOAD,
        {"document_id": document_id, "sha256": sha256, "scan_required": scan_required},
    ).first()
    if reuse is not None:
        connection.execute(
            REFRESH_UPLOAD, {"version_id": reuse.id, "oid": oid, "project_id": project_id}
        )
        return Reservation(document_id, reuse.id, reuse.version_number, reused=True)
    number = int(connection.execute(NEXT_VERSION_NUMBER, {"document_id": document_id}).scalar_one())
    version_id = uuid4()
    connection.execute(
        INSERT_VERSION,
        {
            "version_id": version_id,
            "document_id": document_id,
            "index_id": index_id,
            "version_number": number,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "content_type": name.content_type.value,
            "source_name": name.source_name,
            "blob_path": source_path(index_id, document_id, number),
            "oid": oid,
            "project_id": project_id,
            "scan_required": scan_required,
            "scan_status": (
                ScanStatus.AWAITING if scan_required else ScanStatus.NOT_REQUIRED
            ).value,
        },
    )
    return Reservation(document_id, version_id, number, reused=False)


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "name": _EXAMPLE_NAME,
        "size_bytes": 482_113,
        "sha256": _EXAMPLE_SHA256,
        "project_id": _EXAMPLE_PROJECT,
        "oid": _EXAMPLE_USER,
        "confirm_update": False,
        "scan_required": False,
    }
)
async def reserve_upload(
    engine: SqlEngine,
    *,
    index_id: UUID,
    name: DocumentName,
    size_bytes: int,
    sha256: bytes,
    project_id: UUID,
    oid: UUID,
    confirm_update: bool,
    scan_required: bool,
) -> Reservation:
    """The reservation's transaction (`14-ingestion.md` section 2.4 step 3).

    The unit re-runs as a whole on a transient error (a deadlock included): after its rollback
    nothing of it remains, and a re-run after a lost commit acknowledgement finds and reuses the
    version it inserted. Two first reservations of one name race on `UQ_documents_index_name`:
    the loser re-runs once and then finds the winner's document; a second violation raises
    `SqlUniqueViolation`. A refusal raises `UploadRefusedError`.
    """
    work = partial(
        _reserve,
        index_id=index_id,
        name=name,
        size_bytes=size_bytes,
        sha256=sha256,
        project_id=project_id,
        oid=oid,
        confirm_update=confirm_update,
        scan_required=scan_required,
    )
    try:
        return await engine.run(work, name=RESERVE_UPLOAD, retry="transient")
    except SqlUniqueViolation as violation:
        if violation.constraint != DOCUMENT_NAME_KEY:
            raise
    return await engine.run(work, name=RESERVE_UPLOAD, retry="transient")


# --- Content ----------------------------------------------------------------------------------


def _version_query(*, by_index: bool) -> Any:
    v = document_versions.alias("v")
    p = projects.alias("p")
    u = users.alias("u")
    conditions = [v.c.id == bindparam("version_id")]
    if by_index:
        conditions.append(v.c.search_index_id == bindparam("index_id"))
    return (
        select(
            v.c.id,
            v.c.document_id,
            v.c.search_index_id,
            v.c.version_number,
            v.c.state,
            v.c.size_bytes,
            v.c.sha256,
            v.c.content_type,
            v.c.scan_status,
            v.c.failure_code,
            v.c.uploaded_by,
            u.c.display_name.label("uploaded_by_name"),
            v.c.uploaded_via_project_id,
            p.c.name.label("project_name"),
            v.c.reserved_at,
            v.c.committed_at,
            v.c.ingested_at,
            v.c.document_pdf_status,
        )
        .select_from(
            v.join(p, p.c.id == v.c.uploaded_via_project_id).outerjoin(
                u, u.c.object_id == v.c.uploaded_by
            )
        )
        .where(*conditions)
    )


UPLOAD_VERSION_QUERY: Final = _version_query(by_index=True)
"""The version of the path's index, with its uploader's and its project's names (seeks on
`PK_document_versions`, `PK_projects` and `PK_users`)."""

VERSION_AFTER_COMMIT: Final = _version_query(by_index=False)


@dataclass(frozen=True, slots=True)
class UploadVersion:
    """A version as `adminUploadContent` checks it and as `VersionSummary` shows it;
    `uploaded_by_name` is `None` when the uploader has no `app.users` row."""

    id: UUID
    document_id: UUID
    index_id: UUID
    version_number: int
    state: DocumentVersionState
    size_bytes: int
    sha256: bytes
    content_type: ContentType
    scan_status: ScanStatus
    failure_code: str | None
    uploaded_by: UUID
    uploaded_by_name: str | None
    project_id: UUID
    project_name: str
    reserved_at: datetime
    committed_at: datetime | None
    ingested_at: datetime | None
    document_pdf_status: DocumentPdfStatus | None


def _upload_version(row: Any) -> UploadVersion:
    return UploadVersion(
        id=row.id,
        document_id=row.document_id,
        index_id=row.search_index_id,
        version_number=row.version_number,
        state=DocumentVersionState(row.state),
        size_bytes=row.size_bytes,
        sha256=bytes(row.sha256),
        content_type=ContentType(row.content_type),
        scan_status=ScanStatus(row.scan_status),
        failure_code=row.failure_code,
        uploaded_by=row.uploaded_by,
        uploaded_by_name=row.uploaded_by_name,
        project_id=row.uploaded_via_project_id,
        project_name=row.project_name,
        reserved_at=utc(row.reserved_at),
        committed_at=None if row.committed_at is None else utc(row.committed_at),
        ingested_at=None if row.ingested_at is None else utc(row.ingested_at),
        document_pdf_status=(
            None if row.document_pdf_status is None else DocumentPdfStatus(row.document_pdf_status)
        ),
    )


@request_path(example_params={"index_id": _EXAMPLE_INDEX, "version_id": _EXAMPLE_VERSION})
async def read_upload_version(
    engine: SqlEngine, *, index_id: UUID, version_id: UUID
) -> UploadVersion | None:
    """The version when it belongs to the index; `None` otherwise (a version of another index
    and a random ID alike)."""
    row = await engine.fetch_one(
        UPLOAD_VERSION_QUERY, {"index_id": index_id, "version_id": version_id}, name=UPLOAD_VERSION
    )
    return None if row is None else _upload_version(row)


COMMIT_VERSION: Final = (
    update(document_versions)
    .where(
        document_versions.c.id == bindparam("version_id"),
        document_versions.c.state == _UPLOADING,
    )
    .values(
        state=_PENDING,
        committed_at=SYSUTCDATETIME,
        blob_last_modified=bindparam("last_modified"),
    )
)
"""`14-ingestion.md` section 2.5 step 7. The scan columns stay as the reservation's insert set
them; the API's column grant names neither."""

type CommitOutcome = Literal["committed", "already_pending", "not_uploading", "duplicate"]


@dataclass(frozen=True, slots=True)
class CommitResult:
    """The commit's outcome: `committed` (with the audit row written), `already_pending` (an
    earlier attempt committed it), `not_uploading` (a delete failed it meanwhile) or `duplicate`
    (a filtered SHA-256 index refused it; `code` names which). `version` is the row after the
    transaction, `None` when it is gone."""

    outcome: CommitOutcome
    version: UploadVersion | None = None
    code: DuplicateCode | None = None
    events: tuple[AuditEvent, ...] = ()


def upload_committed_event(version: UploadVersion, actor: UUID) -> AuditEvent:
    """`document.upload_committed`: target the version, the upload's project, metadata of IDs
    and sizes only (`14-ingestion.md` section 18.5)."""
    return AuditEvent(
        action="document.upload_committed",
        actor_type="user",
        actor_object_id=actor,
        target_type="document_version",
        target_id=str(version.id),
        project_id=version.project_id,
        metadata=DocumentUploadCommittedMetadata(
            index_id=version.index_id,
            document_id=version.document_id,
            version_number=version.version_number,
            size_bytes=version.size_bytes,
            content_type=version.content_type.value,
        ),
    )


def _commit(
    connection: Connection, *, version: UploadVersion, last_modified: datetime, actor: UUID
) -> CommitResult:
    changed = connection.execute(
        COMMIT_VERSION, {"version_id": version.id, "last_modified": sql_time(last_modified)}
    ).rowcount
    events: tuple[AuditEvent, ...] = ()
    if changed == 1:
        event = upload_committed_event(version, actor)
        write_audit_event(connection, event)
        events = (event,)
    row = connection.execute(VERSION_AFTER_COMMIT, {"version_id": version.id}).first()
    after = None if row is None else _upload_version(row)
    if events:
        return CommitResult("committed", after, events=events)
    # The version is `pending` with the reserved hash only when an earlier attempt or a
    # concurrent stream committed these bytes; any other state means a delete failed it.
    if after is not None and after.state is DocumentVersionState.PENDING:
        return CommitResult("already_pending", after)
    return CommitResult("not_uploading", after)


@request_path(
    example_params={
        "version": UploadVersion(
            id=_EXAMPLE_VERSION,
            document_id=_EXAMPLE_DOCUMENT,
            index_id=_EXAMPLE_INDEX,
            version_number=1,
            state=DocumentVersionState.UPLOADING,
            size_bytes=482_113,
            sha256=_EXAMPLE_SHA256,
            content_type=ContentType.PDF,
            scan_status=ScanStatus.NOT_REQUIRED,
            failure_code=None,
            uploaded_by=_EXAMPLE_USER,
            uploaded_by_name=None,
            project_id=_EXAMPLE_PROJECT,
            project_name="Travel",
            reserved_at=_EXAMPLE_TIME,
            committed_at=None,
            ingested_at=None,
            document_pdf_status=None,
        ),
        "last_modified": _EXAMPLE_TIME,
        "actor": _EXAMPLE_USER,
    }
)
async def commit_upload(
    engine: SqlEngine, *, version: UploadVersion, last_modified: datetime, actor: UUID
) -> CommitResult:
    """The version becomes `pending` with its commit time and the blob's `last_modified`, and
    `document.upload_committed` is written, in one transaction. The update is guarded by the
    state, so a re-run after a lost acknowledgement finds the version `pending` and writes
    nothing more. A violation of either filtered SHA-256 index rolls back and returns
    `duplicate` with its code."""
    work = partial(_commit, version=version, last_modified=last_modified, actor=actor)
    try:
        return await engine.run(work, name=COMMIT_UPLOAD, retry="transient")
    except SqlUniqueViolation as violation:
        code = DUPLICATE_CODES.get(violation.constraint or "")
        if code is None:
            raise
    return CommitResult("duplicate", code=code)


FAIL_VERSION: Final = (
    update(document_versions)
    .where(
        document_versions.c.id == bindparam("version_id"),
        document_versions.c.state == _UPLOADING,
    )
    .values(state=_FAILED, failure_code=bindparam("code"), failed_at=SYSUTCDATETIME)
)
"""After a refused commit: the version `failed` with `content_unchanged` or
`duplicate_content`."""


@request_path(example_params={"version_id": _EXAMPLE_VERSION, "code": "duplicate_content"})
async def fail_upload(engine: SqlEngine, *, version_id: UUID, code: DuplicateCode) -> bool:
    """Mark an `uploading` version `failed` with the code its commit met; `False` when it had
    left `uploading`. Guarded by the state, so a re-run changes nothing."""
    changed = await engine.execute(
        FAIL_VERSION, {"version_id": version_id, "code": code}, name=FAIL_UPLOAD, retry="transient"
    )
    return changed == 1


# --- Submit -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmittedUploads:
    """The run (when one was created), each version's outcome, and the audit rows written."""

    result: UploadRunResult
    events: tuple[AuditEvent, ...] = ()


def run_submitted_event(index_id: UUID, result: UploadRunResult, actor: UUID) -> AuditEvent:
    """`ingestion.run_submitted`: target the run, metadata `indexId`, `itemCount`,
    `notQueuedCount` (`14-ingestion.md` section 18.5)."""
    if result.run_id is None:
        raise ValueError("only a created run is audited")
    return AuditEvent(
        action="ingestion.run_submitted",
        actor_type="user",
        actor_object_id=actor,
        target_type="ingestion_run",
        target_id=str(result.run_id),
        metadata=IngestionRunSubmittedMetadata(
            index_id=index_id,
            item_count=result.item_count,
            not_queued_count=result.not_queued_count,
        ),
    )


def _submit(
    connection: Connection,
    *,
    index_id: UUID,
    version_ids: Sequence[UUID],
    actor: UUID,
    trace: TraceContext | None,
) -> SubmittedUploads:
    result = create_upload_run(
        connection, index_id=index_id, version_ids=version_ids, requested_by=actor, trace=trace
    )
    if result.run_id is None:
        return SubmittedUploads(result)
    event = run_submitted_event(index_id, result, actor)
    write_audit_event(connection, event)
    return SubmittedUploads(result, (event,))


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "version_ids": [_EXAMPLE_VERSION],
        "actor": _EXAMPLE_USER,
        "trace": _EXAMPLE_TRACE,
    }
)
async def submit_uploads(
    engine: SqlEngine,
    *,
    index_id: UUID,
    version_ids: Sequence[UUID],
    actor: UUID,
    trace: TraceContext | None,
) -> SubmittedUploads:
    """`adminSubmitUploads`' transaction (`14-ingestion.md` section 2.6 step 2) with its audit
    row. Run creation draws random IDs, so the unit is never re-run as a whole; a refusal
    (`IngestionRefusedError`, an index with no physical index to target) rolls it back."""
    work = partial(_submit, index_id=index_id, version_ids=version_ids, actor=actor, trace=trace)
    return await engine.run(work, name=SUBMIT_UPLOADS, retry="none")
