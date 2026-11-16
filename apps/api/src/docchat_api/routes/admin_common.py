"""What the administrator areas share: the reference models of `11-api.md` section 4.3
(`UserRef`, `IndexRef`, `ProjectRef`, `DocumentRef`, each defined once, because two models of one
name would break the OpenAPI schema names), `VersionSummary` (section 4.7), which the upload
and document operations both return, the page query of the administrator lists and the assembly
of a keyset page with its next cursor (section 1.8).

A closed set is published inline, never as a schema of its own (section 4.8): `inline_enum` gives
the enumeration of a ledger enum, and the builders pass its members, which validate to their text.
`VersionSummary.failureMessage` carries the safe message of its code (section 2.6): the item-code
catalogue's for an ingestion code, the problem catalogue's for the two commit refusals a version
can also fail with (`content_unchanged`, `duplicate_content`).

A cursor holds the sort values of the last item, and section 1.8 caps its text at 512
characters. A display name of 256 code points, or a name and a search text of 80 code points
each, outside the Latin range can encode past that cap, so a page whose last item can't carry a
cursor ends one item earlier: the client follows `nextCursor` and loses nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum
from typing import Annotated, Final
from uuid import UUID

from pydantic import Field, WithJsonSchema

from docchat_api.http.models import QueryModel, ResponseModel
from docchat_api.http.paging import CursorModel, CursorText, encode_cursor
from docchat_core.access import MAX_PAGE_LIMIT
from docchat_core.auth.principal import DISPLAY_NAME_MAX_LENGTH
from docchat_core.errors import CODES, ITEM_CODES
from docchat_core.ingestion.models import (
    ContentType,
    DocumentPdfStatus,
    DocumentVersionState,
    ScanStatus,
)
from docchat_core.models import UtcDateTime
from docchat_core.sql.repositories.documents import VersionRow
from docchat_core.sql.repositories.projects import IndexRefRow, UserRefRow

DEFAULT_LIMIT: Final = 50
INDEX_DISPLAY_NAME_MAX_LENGTH: Final = 80
PROJECT_NAME_MAX_LENGTH: Final = 80
DOCUMENT_NAME_MAX_LENGTH: Final = 255
MESSAGE_MAX_LENGTH: Final = 400
SIZE_BYTES_MAX: Final = 1_099_511_627_776
INT_MAX: Final = 2_147_483_647


def inline_enum(enum: type[Enum]) -> WithJsonSchema:
    """The inline enumeration of `enum`'s values."""
    return WithJsonSchema({"type": "string", "enum": [member.value for member in enum]})


VersionStateValue = Annotated[str, inline_enum(DocumentVersionState)]
ScanStatusValue = Annotated[str, inline_enum(ScanStatus)]
ContentTypeValue = Annotated[str, inline_enum(ContentType)]
DocumentPdfStatusValue = Annotated[str, inline_enum(DocumentPdfStatus)]
Message = Annotated[str, Field(min_length=1, max_length=MESSAGE_MAX_LENGTH)]
Code = Annotated[str, Field(min_length=1, max_length=64)]


class PageQuery(QueryModel):
    """`limit` (1 to 100, default 50) and `cursor`, the parameters of a list without filters."""

    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_LIMIT
    cursor: CursorText | None = None


class UserRef(ResponseModel):
    """A person; `displayName` is `""` when no `app.users` row exists for the ID."""

    user_id: UUID
    display_name: Annotated[str, Field(max_length=DISPLAY_NAME_MAX_LENGTH)]


class IndexRef(ResponseModel):
    """A logical index."""

    index_id: UUID
    display_name: Annotated[str, Field(min_length=1, max_length=INDEX_DISPLAY_NAME_MAX_LENGTH)]


class ProjectRef(ResponseModel):
    """A project and its name (`11-api.md` section 4.3)."""

    project_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)]


class DocumentRef(ResponseModel):
    """A document and its source name (`11-api.md` section 4.3)."""

    document_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=DOCUMENT_NAME_MAX_LENGTH)]


class VersionSummary(ResponseModel):
    version_id: UUID
    version_number: Annotated[int, Field(ge=1, le=INT_MAX)]
    state: VersionStateValue
    size_bytes: Annotated[int, Field(ge=1, le=SIZE_BYTES_MAX)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    content_type: ContentTypeValue
    scan_status: ScanStatusValue
    failure_code: Code | None
    failure_message: Message | None
    uploaded_by: UUID
    uploaded_by_name: Annotated[str, Field(max_length=256)] | None
    uploaded_via_project: ProjectRef
    reserved_at: UtcDateTime
    committed_at: UtcDateTime | None
    ingested_at: UtcDateTime | None
    document_pdf_status: DocumentPdfStatusValue | None


def failure_message(code: str | None) -> str | None:
    """The safe message of a version's `failure_code` or an item's code: an ingestion item
    code's message, or the text of the problem code a refused commit stored
    (`content_unchanged`, `duplicate_content`)."""
    if code is None:
        return None
    item = ITEM_CODES.get(code)
    if item is not None:
        return item.message
    problem = CODES.get(code)
    return None if problem is None else problem.detail


def version_summary(row: VersionRow) -> VersionSummary:
    return VersionSummary(
        version_id=row.id,
        version_number=row.version_number,
        state=row.state,
        size_bytes=row.size_bytes,
        sha256=row.sha256.hex(),
        content_type=row.content_type,
        scan_status=row.scan_status,
        failure_code=row.failure_code,
        failure_message=failure_message(row.failure_code),
        uploaded_by=row.uploaded_by,
        uploaded_by_name=row.uploaded_by_name,
        uploaded_via_project=ProjectRef(project_id=row.project_id, name=row.project_name),
        reserved_at=row.reserved_at,
        committed_at=row.committed_at,
        ingested_at=row.ingested_at,
        document_pdf_status=row.document_pdf_status,
    )


def user_ref(row: UserRefRow) -> UserRef:
    return UserRef(user_id=row.user_id, display_name=row.display_name)


def index_ref(row: IndexRefRow) -> IndexRef:
    return IndexRef(index_id=row.index_id, display_name=row.display_name)


def keyset_page[T](
    rows: Sequence[T], limit: int, cursor_of: Callable[[T], CursorModel]
) -> tuple[list[T], str | None]:
    """The page of `rows` (read with `limit + 1`) and its next cursor (`next_cursor`)."""
    return next_cursor(rows[:limit], len(rows) > limit, cursor_of)


def next_cursor[T](
    items: Sequence[T], more: bool, cursor_of: Callable[[T], CursorModel]
) -> tuple[list[T], str | None]:
    """The page and its next cursor: `None` when no item follows; otherwise the cursor of the
    last item, the page ending earlier while that cursor doesn't fit 512 characters."""
    items = list(items)
    if not more:
        return items, None
    while items:
        cursor = cursor_of(items[-1])
        try:
            return items, encode_cursor(cursor)
        except ValueError:
            items.pop()
    raise ValueError("no item of the page can carry a cursor")
