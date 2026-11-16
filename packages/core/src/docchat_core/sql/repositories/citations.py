"""The statements behind the file and citation reads: the citation's document check
(`12-chat-pipeline.md` section 16.2 step 5) and the administrator's whole-document lookup
(`11-api.md` section 3.11.10 steps 4 and 5).

- **The cited document** (`read_cited_document`): one statement joining the document, the cited
  version, the cited chunk set and the turn's project's mapping of the document's logical index,
  each joined on its primary key, so the plan is four seeks. The API decides from the row: no row
  is `410 document_changed`, an unmapped index `404 citation_not_found`, a deleted document or
  another current version `410`, and the chunk set's `pdf_sha256` against the version's PDF hash
  decides whether the renditions show the cited page.
- **The document's whole-document rendition** (`read_document_pdf`): the logical index and the
  live document of that index with its current version's number and `document_pdf_status`, in
  one round trip (seeks on `PK_search_indexes`, `PK_documents` and `PK_document_versions`). The
  API answers the index (`404 index_not_found`), then the document (`404 document_not_found`),
  then the rendition (`409 document_pdf_unavailable`).

Every value from a request is a bound parameter; the functions return rows and decide nothing.
Both carry `@request_path`, so the plan audit runs each once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, Select, and_, bindparam, case, select

from docchat_core.ingestion.models import ContentType, DocumentPdfStatus, IndexStatus
from docchat_core.sql.engine import SqlEngine, request_path
from docchat_core.sql.repositories.common import ONE, ZERO
from docchat_core.sql.tables.access import project_indexes
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    search_indexes,
)
from docchat_core.sql.tables.schema import BIT_FALSE

# Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics.
CITED_DOCUMENT: Final = "cited_document"
DOCUMENT_PDF: Final = "document_pdf"

# Fixed identifiers for the plan audit; a plan doesn't depend on whether the rows exist.
_EXAMPLE_DOCUMENT: Final = UUID("91b6cfa7-e28a-44b5-8f7d-a0bcde342156")
_EXAMPLE_VERSION: Final = UUID("a2c7d0b8-f39b-45c6-9a8e-b1cdef453267")
_EXAMPLE_CHUNK_SET: Final = UUID("b3d8e1c9-04ac-46d7-8b9f-c2def0564378")
_EXAMPLE_PROJECT: Final = UUID("c4e9f2da-15bd-47e8-9ca0-d3ef01675489")
_EXAMPLE_INDEX: Final = UUID("d5fa03eb-26ce-48f9-8db1-e4f01278659a")


# --- The cited document --------------------------------------------------------------------


def _cited_document() -> Select[Any]:
    d = documents.alias("d")
    v = document_versions.alias("v")
    cs = chunk_sets.alias("cs")
    pi = project_indexes.alias("pi")
    return (
        select(
            d.c.search_index_id,
            d.c.is_deleted,
            d.c.current_version_id,
            d.c.source_name,
            v.c.content_type,
            v.c.version_number,
            v.c.sha256,
            v.c.converted_sha256,
            v.c.document_pdf_status,
            cs.c.pdf_sha256,
            case((pi.c.project_id.is_(None), ZERO), else_=ONE).label("index_mapped"),
        )
        .select_from(
            d.join(v, and_(v.c.id == bindparam("document_version_id"), v.c.document_id == d.c.id))
            .join(
                cs, and_(cs.c.id == bindparam("chunk_set_id"), cs.c.document_version_id == v.c.id)
            )
            .outerjoin(
                pi,
                and_(
                    pi.c.project_id == bindparam("project_id"),
                    pi.c.search_index_id == d.c.search_index_id,
                ),
            )
        )
        .where(d.c.id == bindparam("document_id"))
    )


CITED_DOCUMENT_QUERY: Final = _cited_document()
"""Section 16.2 step 5, index-backed by the primary keys of all four tables."""


def _sha(value: Any) -> bytes | None:
    return None if value is None else bytes(value)


@dataclass(frozen=True, slots=True)
class CitedDocument:
    """The document, version, chunk-set and mapping facts of one citation."""

    search_index_id: UUID
    is_deleted: bool
    current_version_id: UUID | None
    source_name: str
    content_type: ContentType
    version_number: int
    sha256: bytes
    converted_sha256: bytes | None
    document_pdf_status: DocumentPdfStatus | None
    pdf_sha256: bytes | None
    index_mapped: bool

    @property
    def rendition_pdf_sha256(self) -> bytes | None:
        """The hash of the PDF the version's renditions come from: the source of a PDF version,
        the converted PDF of a DOCX or PPTX version (`None` until one is recorded)."""
        return self.sha256 if self.content_type is ContentType.PDF else self.converted_sha256


def _cited(row: Row[Any]) -> CitedDocument:
    status = row.document_pdf_status
    return CitedDocument(
        search_index_id=row.search_index_id,
        is_deleted=bool(row.is_deleted),
        current_version_id=row.current_version_id,
        source_name=row.source_name,
        content_type=ContentType(row.content_type),
        version_number=row.version_number,
        sha256=bytes(row.sha256),
        converted_sha256=_sha(row.converted_sha256),
        document_pdf_status=None if status is None else DocumentPdfStatus(status),
        pdf_sha256=_sha(row.pdf_sha256),
        index_mapped=row.index_mapped == 1,
    )


@request_path(
    example_params={
        "document_id": _EXAMPLE_DOCUMENT,
        "document_version_id": _EXAMPLE_VERSION,
        "chunk_set_id": _EXAMPLE_CHUNK_SET,
        "project_id": _EXAMPLE_PROJECT,
    }
)
async def read_cited_document(
    engine: SqlEngine,
    *,
    document_id: UUID,
    document_version_id: UUID,
    chunk_set_id: UUID,
    project_id: UUID,
) -> CitedDocument | None:
    """The cited document's row; `None` when the document, the cited version of it or the cited
    chunk set of that version no longer exists."""
    row = await engine.fetch_one(
        CITED_DOCUMENT_QUERY,
        {
            "document_id": document_id,
            "document_version_id": document_version_id,
            "chunk_set_id": chunk_set_id,
            "project_id": project_id,
        },
        name=CITED_DOCUMENT,
    )
    return None if row is None else _cited(row)


# --- The administrator's whole-document rendition ------------------------------------------


def _document_pdf() -> Select[Any]:
    s = search_indexes.alias("s")
    d = documents.alias("d")
    v = document_versions.alias("v")
    return (
        select(
            s.c.status,
            d.c.id.label("document_id"),
            d.c.source_name,
            v.c.id.label("version_id"),
            v.c.version_number,
            v.c.document_pdf_status,
        )
        .select_from(
            s.outerjoin(
                d,
                and_(
                    d.c.id == bindparam("document_id"),
                    d.c.search_index_id == s.c.id,
                    d.c.is_deleted == BIT_FALSE,
                ),
            ).outerjoin(v, v.c.id == d.c.current_version_id)
        )
        .where(s.c.id == bindparam("index_id"))
    )


DOCUMENT_PDF_QUERY: Final = _document_pdf()
"""Section 3.11.10 steps 4 and 5: the index row, and the live document of that index with its
current version, both left-joined so the one round trip answers every step."""


@dataclass(frozen=True, slots=True)
class CurrentVersion:
    """A document's current version, as the whole-document download needs it."""

    id: UUID
    version_number: int
    document_pdf_status: DocumentPdfStatus | None


@dataclass(frozen=True, slots=True)
class LiveDocument:
    """A live document of the index and its current version (`None` when it has none)."""

    id: UUID
    source_name: str
    current_version: CurrentVersion | None


@dataclass(frozen=True, slots=True)
class DocumentPdfLookup:
    """The index exists and isn't `deleted`; `document` is `None` when the index holds no live
    document with that ID (a deleted document, one of another index, a random ID)."""

    document: LiveDocument | None


def _lookup(row: Row[Any]) -> DocumentPdfLookup:
    if row.document_id is None:
        return DocumentPdfLookup(document=None)
    version = None
    if row.version_id is not None:
        status = row.document_pdf_status
        version = CurrentVersion(
            id=row.version_id,
            version_number=row.version_number,
            document_pdf_status=None if status is None else DocumentPdfStatus(status),
        )
    return DocumentPdfLookup(
        document=LiveDocument(
            id=row.document_id, source_name=row.source_name, current_version=version
        )
    )


@request_path(example_params={"index_id": _EXAMPLE_INDEX, "document_id": _EXAMPLE_DOCUMENT})
async def read_document_pdf(
    engine: SqlEngine, *, index_id: UUID, document_id: UUID
) -> DocumentPdfLookup | None:
    """The index and its live document with the current version; `None` when the index is
    missing or `deleted`."""
    row = await engine.fetch_one(
        DOCUMENT_PDF_QUERY, {"index_id": index_id, "document_id": document_id}, name=DOCUMENT_PDF
    )
    if row is None or row.status == IndexStatus.DELETED:
        return None
    return _lookup(row)
