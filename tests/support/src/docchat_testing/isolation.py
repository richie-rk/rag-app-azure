"""The cross-project and cross-user isolation suite: the registry `DATA_PATHS`, its completeness
check and the runner (`84-testing.md` section 4; `11-api.md` acceptance criterion 16;
`82-security.md` SEC-14, SEC-15 and SEC-20; T-002, T-003).

Each `Entry` names one data path and one case: the shared principal who acts, the rows its setup
lays out (builder calls through `sa`), the API calls of its action and the check of what came
back. `tests/integration/isolation/` runs every entry through the in-process API against the
compose services with the permissive limiters. Every component registers its entries here under
its owner (`ia`, `chat`, `conv`, `ing`); `completeness_problems` is the registry check of section
4.1, which fails while any of the data paths that must hold a refusal has none.

The chat entries (`chat`) are the six of the chat pipeline that `createChatTurn` serves: another
project, a session of another project, another user's session, the retrieval scope, a shared
index and an unmapped override; the API runs them over the chat doubles of the `api` fixture
(`ApiUnderTest.chat`), so "the model receives no call" reads the model double's requests.

The identity-and-access entries cover the project picker, the member operations, overrides
(refused by the composite foreign key) and every administrator route; the ones whose action is a
chat turn, a session read or a citation run over the chat doubles too:
`ia.members.revoked_and_restored`, `ia.projects.archived_hidden` (both lists, then every chat
path of the archived project), `ia.mappings.mapped_only`, `ia.overrides.expired_ignored` and
`ia.overrides.other_user_unchanged`, whose checks read which physical index the search double was
asked for.

The ingestion entries cover the document and run operations: the refusal of every document,
upload and run route to a `User` token, and the absence of index Y's documents, versions and runs
from index X's document list, document reads (`adminGetDocument` and `adminGetDocumentPdf`,
`404 document_not_found`) and run list; the one operation that reads a version by its ID under an
index, `adminUploadContent`, answers Y's version under X with `404 version_not_found`.

The citation entries (`citations.*`, and `ia.mappings.unmapped_hides_citations`) need the Cosmos
DB emulator and Azurite besides SQL Server: the runner gives the context the session's emulator
database (`cosmos`, whose containers the app also uses) and Azurite's `renditions` container
client, and installs `CosmosAudit` on the app's containers (`audit`), so each entry also checks
that every partition key the app used is the caller's own. A turn and its renditions are written
by the entry's action, since the setup is synchronous.

The conversation entries (`conv.*`) cover the session reads, the memory operations and feedback:
`alice` and `bob` both reach A and B, so only the partition key keeps one's sessions, turns and
facts from the other, and `carol` (an administrator) reaches none of them either. Their actions
write the sessions, turns and memory documents into the emulator and read back what the check
compares, and each check confirms that every partition key the app used was a caller's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal, get_args

import httpx
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from sqlalchemy import Engine, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.conversations import turn_id, turn_trace_id
from docchat_core.cosmos.usage import download_lease_id, download_rate_window_id, epoch_minute
from docchat_core.ingestion.names import rendition_document_path, rendition_page_path
from docchat_core.models import encode_row_version
from docchat_core.sql.tables.access import (
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_runs,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_testing.authz_matrix import without_correlation
from docchat_testing.builders import (
    project_index_row,
    project_member_row,
    project_row,
    search_index_row,
    user_index_override_row,
    user_row,
)
from docchat_testing.builders.cosmos import (
    cosmos_now,
    fact,
    memory_document,
    session_document,
    stored_citation,
    turn_document,
)
from docchat_testing.builders.ingestion import (
    chunk_set_row,
    document_row,
    document_version_row,
    ingestion_run_row,
    physical_index_row,
    served_chunk_set_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fakes.chat import ChatFakes, search_result
from docchat_testing.fixtures.identities import PrincipalName, SharedPrincipal

type Owner = Literal["ia", "chat", "conv", "ing"]
type DataPath = Literal[
    "chat",
    "retrieval",
    "sessions",
    "memory",
    "feedback",
    "citations",
    "documents",
    "uploads",
    "ingestion_status",
    "projects",
    "members",
    "mappings",
    "overrides",
    "admin_routes",
]

OWNERS: Final[frozenset[str]] = frozenset(get_args(Owner.__value__))
DATA_PATH_NAMES: Final[frozenset[str]] = frozenset(get_args(DataPath.__value__))
PRINCIPALS: Final[frozenset[str]] = frozenset(get_args(PrincipalName.__value__))
REQUIRED_NEGATIVE_PATHS: Final = (
    "chat",
    "retrieval",
    "sessions",
    "memory",
    "feedback",
    "citations",
    "documents",
    "ingestion_status",
)
"""The data paths that must each hold at least one negative entry (section 4.1)."""

_NAME_RE: Final = re.compile(r"^[a-z_]+(\.[a-z_]+){1,2}$")
_PATH_PARAMETER: Final = re.compile(r"{([^}]+)}")
SHARED_NAME: Final = "Shared.pdf"
RENDITION: Final = b"%PDF-1.7\n% a verified rendition\n%%EOF\n"
CITATION_FILES: Final = ("", "/content", "/document")
"""The suffixes of `getCitation`, `getCitationContent` and `getCitationDocument`."""
"""The name Y's document carries in the upload isolation entry."""

INGESTION_ROUTES: Final = (
    "/v1/admin/indexes/{indexId}/documents",
    "/v1/admin/indexes/{indexId}/uploads",
    "/v1/admin/ingestion-runs",
)
"""The path prefixes of the document, upload and run routes of `14-ingestion.md`."""


@dataclass(slots=True)
class IsolationContext:
    """What an entry's steps share: the in-process API's client, `sa` on the session's database,
    the five principals, the exported OpenAPI document, the identifiers the setup laid out and
    the responses the action received."""

    client: httpx.AsyncClient
    admin: Engine
    principals: Mapping[PrincipalName, SharedPrincipal]
    document: Mapping[str, Any]
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    responses: list[httpx.Response] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    cosmos: Any = None
    """The session's emulator database (`DatabaseProxy`), whose containers the app uses."""
    renditions: Any = None
    """Azurite's `renditions` container client (`ContainerClient`), which the app reads."""
    audit: Any = None
    """The `CosmosAudit` installed on the app's containers."""
    chat: ChatFakes | None = None
    """The chat pipeline's doubles the API runs over (the chat entries)."""
    names: dict[str, str] = field(default_factory=dict)
    """Physical index names by the label of their logical index."""
    found: dict[str, Any] = field(default_factory=dict)
    """What an action read for its check (a stored trace)."""

    def headers(self, name: PrincipalName) -> dict[str, str]:
        return self.principals[name].authorization()

    def put(self, *rows: tuple[Any, Mapping[str, Any]]) -> None:
        with self.admin.begin() as connection:
            for table, row in rows:
                connection.execute(insert(table), dict(row))


type Setup = Callable[[IsolationContext], None]
type Action = Callable[[IsolationContext], Awaitable[None]]
type Check = Callable[[IsolationContext], None]


@dataclass(frozen=True, slots=True)
class Entry:
    """One registered case of section 4.1's table; `expected` checks what `action` received."""

    name: str
    owner: Owner
    data_path: DataPath
    actor: PrincipalName
    setup: Setup
    action: Action
    expected: Check
    negative: bool
    description: str


def completeness_problems(entries: Sequence[Entry]) -> list[str]:
    """The registry check (T-003): a repeated name, a name outside `<owner>.<path>.<case>`, an
    actor outside the five principals, a data path outside the list, and each data path of
    `REQUIRED_NEGATIVE_PATHS` without a negative entry."""
    problems: list[str] = []
    for name, times in Counter(entry.name for entry in entries).items():
        if times > 1:
            problems.append(f"{name}: registered {times} times")
    for entry in entries:
        if not _NAME_RE.fullmatch(entry.name):
            problems.append(f"{entry.name}: not a dotted <owner>.<data path>.<case> name")
        if entry.owner not in OWNERS:
            problems.append(f"{entry.name}: owner {entry.owner!r} isn't a component")
        if entry.actor not in PRINCIPALS:
            problems.append(f"{entry.name}: actor {entry.actor!r} isn't a shared principal")
        if entry.data_path not in DATA_PATH_NAMES:
            problems.append(f"{entry.name}: no data path")
    negative = {entry.data_path for entry in entries if entry.negative}
    problems.extend(
        f"{path}: no negative entry" for path in REQUIRED_NEGATIVE_PATHS if path not in negative
    )
    return problems


async def run_entry(entry: Entry, context: IsolationContext) -> None:
    """Lay out the entry's rows, make its calls, check what came back. The setup and the check
    are synchronous (`sa` statements among them), so they run in a worker thread: on the loop,
    a setup's inserts plus an action that never suspends (every call refused before any I/O)
    can hold it past the loop guard's 100 ms."""
    await asyncio.to_thread(entry.setup, context)
    await entry.action(context)
    await asyncio.to_thread(entry.expected, context)


# --- Identity and access (`ia`) -------------------------------------------------------------


def _two_projects(context: IsolationContext) -> None:
    """Projects A and B, `alice` a member of A and `bob` of B (section 4.2)."""
    alice, bob = context.principals["alice"], context.principals["bob"]
    project_a, project_b = project_row(), project_row()
    context.put(
        (users, user_row(object_id=alice.oid)),
        (users, user_row(object_id=bob.oid)),
        (projects, project_a),
        (projects, project_b),
        (project_members, project_member_row(alice.oid, project_a["id"])),
        (project_members, project_member_row(bob.oid, project_b["id"])),
    )
    context.ids |= {"A": project_a["id"], "B": project_b["id"]}


def _archive_a(context: IsolationContext) -> None:
    with context.admin.begin() as connection:
        connection.execute(
            update(projects)
            .where(projects.c.id == context.ids["A"])
            .values(status="archived", archived_at=sql_now(), archived_by=uuid.uuid4())
        )


def _listing(*callers: PrincipalName) -> Action:
    async def action(context: IsolationContext) -> None:
        for caller in callers:
            context.responses.append(
                await context.client.get(
                    "/v1/projects", params={"limit": 100}, headers=context.headers(caller)
                )
            )

    return action


def _listed(response: httpx.Response) -> set[uuid.UUID]:
    assert response.status_code == 200, response.status_code
    return {uuid.UUID(item["projectId"]) for item in response.json()["items"]}


def _lists_only_a(context: IsolationContext) -> None:
    (response,) = context.responses
    listed = _listed(response)
    assert context.ids["A"] in listed
    assert context.ids["B"] not in listed


def _lists_a_and_b(context: IsolationContext) -> None:
    (response,) = context.responses
    assert {context.ids["A"], context.ids["B"]} <= _listed(response)


def _lists_neither_archived(responses: Sequence[httpx.Response], project: uuid.UUID) -> None:
    for response in responses:
        assert project not in _listed(response)


def _admin_required(response: httpx.Response) -> None:
    assert response.status_code == 403, response.status_code
    assert response.json()["code"] == "admin_required"


async def _member_operations(context: IsolationContext) -> None:
    """`alice` on every member operation, with A and `bob` (real IDs) and with random ones."""
    bob = context.principals["bob"].oid
    for project_id, user_id in ((context.ids["A"], bob), (uuid.uuid4(), uuid.uuid4())):
        base = f"/v1/admin/projects/{project_id}/members"
        headers = context.headers("alice") | {"If-Match": '"AAAAAAAAB9E="'}
        context.responses.append(await context.client.get(base, headers=headers))
        context.responses.append(await context.client.put(f"{base}/{user_id}", headers=headers))
        context.responses.append(await context.client.delete(f"{base}/{user_id}", headers=headers))


def _all_admin_required(context: IsolationContext) -> None:
    assert context.responses
    for response in context.responses:
        _admin_required(response)


def _filled_path(path: str) -> str:
    for name in _PATH_PARAMETER.findall(path):
        # Every administrator path parameter is an identifier, and the refusal comes before any
        # lookup, so a random one shows that nothing about the resource leaks.
        path = path.replace("{" + name + "}", str(uuid.uuid4()))
    return path


async def _every_admin_operation(context: IsolationContext) -> None:
    """`alice` on every `/v1/admin` operation of the exported document, with random IDs and an
    invalid body."""
    for path, item in context.document["paths"].items():
        if not path.startswith("/v1/admin"):
            continue
        for method in item:
            context.responses.append(
                await context.client.request(
                    method.upper(),
                    _filled_path(path),
                    content=b"{not json",
                    headers=context.headers("alice")
                    | {"Content-Type": "application/json", "If-Match": "*"},
                )
            )


def _none_reveals_anything(context: IsolationContext) -> None:
    _all_admin_required(context)
    admin_operations = sum(
        len(item)
        for path, item in context.document["paths"].items()
        if path.startswith("/v1/admin")
    )
    assert len(context.responses) == admin_operations


def _unmapped_override_setup(context: IsolationContext) -> None:
    _two_projects(context)
    index_b = search_index_row(status="ready")
    context.put(
        (search_indexes, index_b),
        (project_indexes, project_index_row(context.ids["B"], index_b["id"], is_default=True)),
    )
    context.ids["Y"] = index_b["id"]


async def _set_unmapped_override(context: IsolationContext) -> None:
    # An operator sets overrides (`docchat-ops override`); the composite foreign key refuses one
    # whose index the project doesn't map, whoever writes it.
    row = user_index_override_row(
        context.principals["alice"].oid, context.ids["A"], context.ids["Y"]
    )
    try:
        context.put((user_index_overrides, row))
    except IntegrityError as error:
        context.refused.append(type(error).__name__)


def _override_refused(context: IsolationContext) -> None:
    assert context.refused == ["IntegrityError"]


IDENTITY_AND_ACCESS: Final[tuple[Entry, ...]] = (
    Entry(
        name="ia.projects.member_lists_own",
        owner="ia",
        data_path="projects",
        actor="alice",
        setup=_two_projects,
        action=_listing("alice"),
        expected=_lists_only_a,
        negative=True,
        description="alice lists A and never B in listMyProjects",
    ),
    Entry(
        name="ia.projects.admin_lists_all",
        owner="ia",
        data_path="projects",
        actor="carol",
        setup=_two_projects,
        action=_listing("carol"),
        expected=_lists_a_and_b,
        negative=False,
        description="carol lists A and B without membership rows",
    ),
    Entry(
        name="ia.members.user_token_refused",
        owner="ia",
        data_path="members",
        actor="alice",
        setup=_two_projects,
        action=_member_operations,
        expected=_all_admin_required,
        negative=True,
        description="a User token on any member operation gets 403 admin_required with real "
        "and random IDs",
    ),
    Entry(
        name="ia.overrides.unmapped_refused",
        owner="ia",
        data_path="overrides",
        actor="alice",
        setup=_unmapped_override_setup,
        action=_set_unmapped_override,
        expected=_override_refused,
        negative=True,
        description="an override for alice in A to an index not mapped to A is refused by "
        "FK_user_index_overrides_mapping",
    ),
    Entry(
        name="ia.admin_routes.user_token_refused",
        owner="ia",
        data_path="admin_routes",
        actor="alice",
        setup=_two_projects,
        action=_every_admin_operation,
        expected=_none_reveals_anything,
        negative=True,
        description="every /v1/admin operation with a User token gets 403 admin_required, "
        "never 404, 412, 422 or 428",
    ),
)

# --- Ingestion (`ing`) ----------------------------------------------------------------------


def _two_indexes(context: IsolationContext) -> None:
    """`_two_projects`, then index X mapped to A and Y to B as their defaults, each ready with an
    active physical index, one live document whose ingested version 1 is current and one
    completed run; their IDs are `X`, `DX`, `VX`, `RX` and `Y`, `DY`, `VY`, `RY`."""
    _two_projects(context)
    for project, name in (("A", "X"), ("B", "Y")):
        index = search_index_row(status="ready")
        physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
        document = document_row(index["id"])
        version = document_version_row(
            document["id"],
            index["id"],
            context.ids[project],
            state="ingested",
            committed_at=sql_now(),
            ingested_at=sql_now(),
        )
        run = ingestion_run_row(
            index["id"], state="completed", started_at=sql_now(), completed_at=sql_now()
        )
        context.put(
            (search_indexes, index),
            (physical_indexes, physical),
            (
                project_indexes,
                project_index_row(context.ids[project], index["id"], is_default=True),
            ),
            (documents, document),
            (document_versions, version),
            (ingestion_runs, run),
        )
        with context.admin.begin() as connection:
            connection.execute(
                update(search_indexes)
                .where(search_indexes.c.id == index["id"])
                .values(active_physical_index_id=physical["id"])
            )
            connection.execute(
                update(documents)
                .where(documents.c.id == document["id"])
                .values(current_version_id=version["id"])
            )
        context.ids |= {
            name: index["id"],
            f"D{name}": document["id"],
            f"V{name}": version["id"],
            f"R{name}": run["id"],
        }


def _ingestion_operations(document: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        (method, path)
        for path, item in document["paths"].items()
        if path.startswith(INGESTION_ROUTES)
        for method in item
    ]


def _path_with(path: str, values: Mapping[str, uuid.UUID]) -> str:
    for name in _PATH_PARAMETER.findall(path):
        path = path.replace("{" + name + "}", str(values.get(name, uuid.uuid4())))
    return path


async def _every_ingestion_operation(context: IsolationContext) -> None:
    """`alice` on every document, upload and run operation, with X's real IDs and with random
    ones, an invalid body and `If-Match: *`."""
    real = {
        "indexId": context.ids["X"],
        "documentId": context.ids["DX"],
        "versionId": context.ids["VX"],
        "runId": context.ids["RX"],
    }
    for values in (real, {}):
        for method, path in _ingestion_operations(context.document):
            context.responses.append(
                await context.client.request(
                    method.upper(),
                    _path_with(path, values),
                    content=b"{not json",
                    headers=context.headers("alice")
                    | {"Content-Type": "application/json", "If-Match": "*"},
                )
            )


def _every_ingestion_operation_refused(context: IsolationContext) -> None:
    operations = _ingestion_operations(context.document)
    assert operations
    assert len(context.responses) == 2 * len(operations)
    _all_admin_required(context)


async def _x_documents(context: IsolationContext) -> None:
    """`carol` lists X's documents, reads X's document under X and Y's document under X, asks for
    Y's document's PDF under X, and sends content for Y's version under X."""
    index = context.ids["X"]
    base = f"/v1/admin/indexes/{index}/documents"
    headers = context.headers("carol")
    context.responses.append(await context.client.get(base, params={"limit": 100}, headers=headers))
    for document in ("DX", "DY"):
        context.responses.append(
            await context.client.get(f"{base}/{context.ids[document]}", headers=headers)
        )
    context.responses.append(
        await context.client.get(f"{base}/{context.ids['DY']}/pdf", headers=headers)
    )
    context.responses.append(
        await context.client.put(
            f"/v1/admin/indexes/{index}/uploads/{context.ids['VY']}/content",
            content=b"%PDF-1.7\n",
            headers=headers | {"Content-Type": "application/octet-stream"},
        )
    )


def _version_ids(document: Mapping[str, Any]) -> set[uuid.UUID]:
    versions = [document.get("currentVersion"), document.get("latestVersion")]
    versions.extend(document.get("versions", []))
    return {uuid.UUID(version["versionId"]) for version in versions if version is not None}


def _no_document_of_y(context: IsolationContext) -> None:
    listing, own, other, other_pdf, other_version = context.responses
    assert listing.status_code == 200, listing.status_code
    items = listing.json()["items"]
    assert {uuid.UUID(item["documentId"]) for item in items} == {context.ids["DX"]}
    assert set().union(*(_version_ids(item) for item in items)) == {context.ids["VX"]}
    assert own.status_code == 200, own.status_code
    assert _version_ids(own.json()) == {context.ids["VX"]}
    assert other.status_code == 404, other.status_code
    assert other.json()["code"] == "document_not_found"
    assert other_pdf.status_code == 404, other_pdf.status_code
    assert other_pdf.json()["code"] == "document_not_found"
    assert other_version.status_code == 404, other_version.status_code
    assert other_version.json()["code"] == "version_not_found"


async def _x_runs(context: IsolationContext) -> None:
    """`carol` lists the runs filtered to X."""
    context.responses.append(
        await context.client.get(
            "/v1/admin/ingestion-runs",
            params={"indexId": str(context.ids["X"]), "limit": 100},
            headers=context.headers("carol"),
        )
    )


def _no_run_of_y(context: IsolationContext) -> None:
    (response,) = context.responses
    assert response.status_code == 200, response.status_code
    items = response.json()["items"]
    assert {uuid.UUID(item["runId"]) for item in items} == {context.ids["RX"]}
    assert {uuid.UUID(item["index"]["indexId"]) for item in items} == {context.ids["X"]}


def _y_pending_version(context: IsolationContext) -> None:
    """`_two_indexes`, then Y's document renamed `Shared.pdf` and a committed version 2 of it
    that no run has picked up (`PY`)."""
    _two_indexes(context)
    version = document_version_row(
        context.ids["DY"],
        context.ids["Y"],
        context.ids["B"],
        version_number=2,
        state="pending",
        committed_at=sql_now(),
    )
    context.put((document_versions, version))
    with context.admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == context.ids["DY"])
            .values(source_name=SHARED_NAME, normalized_name=SHARED_NAME.casefold())
        )
    context.ids["PY"] = version["id"]


async def _uploads_under_x(context: IsolationContext) -> None:
    """`carol` submits Y's pending version under X, then reserves a file of Y's document's name
    under X through project A."""
    base = f"/v1/admin/indexes/{context.ids['X']}/uploads"
    headers = context.headers("carol")
    context.responses.append(
        await context.client.post(
            f"{base}/submit", json={"versionIds": [str(context.ids["PY"])]}, headers=headers
        )
    )
    context.responses.append(
        await context.client.post(
            base,
            json={
                "name": SHARED_NAME,
                "sizeBytes": 1000,
                "sha256": "0" * 64,
                "projectId": str(context.ids["A"]),
            },
            headers=headers,
        )
    )


def _nothing_of_y_touched(context: IsolationContext) -> None:
    submitted, reserved = context.responses
    assert submitted.status_code == 200, submitted.status_code
    assert submitted.json() == {
        "runId": None,
        "results": [{"versionId": str(context.ids["PY"]), "outcome": "not_found"}],
    }
    assert reserved.status_code == 201, reserved.status_code
    assert uuid.UUID(reserved.json()["documentId"]) != context.ids["DY"]


INGESTION: Final[tuple[Entry, ...]] = (
    Entry(
        name="ing.documents.non_admin_refused",
        owner="ing",
        data_path="documents",
        actor="alice",
        setup=_two_indexes,
        action=_every_ingestion_operation,
        expected=_every_ingestion_operation_refused,
        negative=True,
        description="a User token on every document, upload and run route gets 403 "
        "admin_required with real and random IDs",
    ),
    Entry(
        name="ing.documents.other_index_absent",
        owner="ing",
        data_path="documents",
        actor="carol",
        setup=_two_indexes,
        action=_x_documents,
        expected=_no_document_of_y,
        negative=True,
        description="index X's document list and document reads never return a document or "
        "version of index Y (404 document_not_found, 404 version_not_found)",
    ),
    Entry(
        name="ing.ingestion_status.other_index_absent",
        owner="ing",
        data_path="ingestion_status",
        actor="carol",
        setup=_two_indexes,
        action=_x_runs,
        expected=_no_run_of_y,
        negative=True,
        description="a run of index Y never appears in a run list filtered to X",
    ),
    Entry(
        name="ing.uploads.other_index_refused",
        owner="ing",
        data_path="uploads",
        actor="carol",
        setup=_y_pending_version,
        action=_uploads_under_x,
        expected=_nothing_of_y_touched,
        negative=True,
        description="upload calls for index X never touch a document or version of index Y: "
        "submitting Y's version under X is not_found, and a reservation under X never reuses "
        "Y's document",
    ),
)

# --- Chat and retrieval (`chat`) ------------------------------------------------------------

CHAT_QUESTION: Final = "How many days do I have to submit an expense claim?"
CHAT_PATH: Final = "/v1/chat/turns"


def seed_project(context: IsolationContext, label: str, *members: PrincipalName) -> None:
    """One more active project `label` with memberships for `members`, each member's
    `app.users` row first when they have none yet (`FK_project_members_users`)."""
    project = project_row()
    oids = [context.principals[member].oid for member in members]
    with context.admin.begin() as connection:
        known = set(
            connection.scalars(select(users.c.object_id).where(users.c.object_id.in_(oids)))
            if oids
            else ()
        )
        for oid in dict.fromkeys(oids):
            if oid not in known:
                connection.execute(insert(users), user_row(object_id=oid))
        connection.execute(insert(projects), project)
        for oid in oids:
            connection.execute(insert(project_members), project_member_row(oid, project["id"]))
    context.ids[label] = project["id"]


def seed_served_index(
    context: IsolationContext, label: str, *project_labels: str, default: bool = True
) -> None:
    """A ready index `label` with an active physical index, mapped to each project of
    `project_labels` (as its default unless `default` is false), holding one live document whose
    current version's chunk set it serves; its IDs are `label`, `D<label>`, `V<label>`,
    `C<label>`, and its physical index's name is in `names`. The search double answers with that
    document's chunk."""
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    document = document_row(index["id"])
    version = document_version_row(
        document["id"],
        index["id"],
        context.ids[project_labels[0]],
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
    )
    chunk_set = chunk_set_row(
        document["id"], version["id"], physical["id"], state="served", served_at=sql_now()
    )
    context.put(
        (search_indexes, index),
        (physical_indexes, physical),
        (documents, document),
        (document_versions, version),
        (chunk_sets, chunk_set),
        (
            served_chunk_sets,
            served_chunk_set_row(document["id"], physical["id"], chunk_set["id"], version["id"]),
        ),
        *(
            (
                project_indexes,
                project_index_row(context.ids[project], index["id"], is_default=default),
            )
            for project in project_labels
        ),
    )
    with context.admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == document["id"])
            .values(current_version_id=version["id"])
        )
    context.ids |= {
        label: index["id"],
        f"D{label}": document["id"],
        f"V{label}": version["id"],
        f"C{label}": chunk_set["id"],
    }
    context.names[label] = str(physical["name"])
    _chat(context).search.results.append(
        search_result(
            document_id=document["id"], chunk_set_id=chunk_set["id"], version_id=version["id"]
        )
    )


def _chat(context: IsolationContext) -> ChatFakes:
    if context.chat is None:
        raise AssertionError("a chat entry runs over the API's chat doubles")
    return context.chat


async def _ask(
    context: IsolationContext, caller: PrincipalName, project: str, session: str | None = None
) -> httpx.Response:
    """One chat turn as `caller` in project `project` (a label), read to its end."""
    response = await context.client.post(
        CHAT_PATH,
        json={
            "projectId": str(context.ids[project]),
            "sessionId": session,
            "clientTurnId": str(uuid.uuid4()),
            "question": CHAT_QUESTION,
        },
        headers=context.headers(caller),
    )
    context.responses.append(response)
    return response


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.status_code
    return [json.loads(line) for line in response.text.splitlines() if line]


def _session_of(response: httpx.Response) -> str:
    meta = _events(response)[0]
    assert meta["type"] == "meta"
    return str(meta["sessionId"])


def _not_found(response: httpx.Response, code: str) -> None:
    assert response.status_code == 404, response.status_code
    assert response.json()["code"] == code


def _searched(context: IsolationContext) -> list[str]:
    return [str(request["index"]) for request in _chat(context).search.requests]


def _b_with_its_index(context: IsolationContext) -> None:
    _two_projects(context)
    seed_served_index(context, "Y", "B")


async def _alice_in_b(context: IsolationContext) -> None:
    await _ask(context, "alice", "B")


def _project_not_found_and_no_model_call(context: IsolationContext) -> None:
    (response,) = context.responses
    _not_found(response, "project_not_found")
    assert _chat(context).model.requests == []


def _a_and_c_for_alice(context: IsolationContext) -> None:
    """`_two_projects`, then project C with `alice`, and `bob` a member of A too; X is A's
    index, Z C's."""
    _two_projects(context)
    seed_project(context, "C", "alice")
    context.put(
        (project_members, project_member_row(context.principals["bob"].oid, context.ids["A"]))
    )
    seed_served_index(context, "X", "A")
    seed_served_index(context, "Z", "C")


async def _own_session_of_c_in_a(context: IsolationContext) -> None:
    session = _session_of(await _ask(context, "alice", "C"))
    await _ask(context, "alice", "A", session)


def _session_not_found(context: IsolationContext) -> None:
    _not_found(context.responses[-1], "session_not_found")


async def _bobs_session_in_a(context: IsolationContext) -> None:
    bobs = _session_of(await _ask(context, "bob", "A"))
    own_of_c = _session_of(await _ask(context, "alice", "C"))
    await _ask(context, "alice", "A", bobs)
    await _ask(context, "alice", "A", own_of_c)


def _foreign_session_like_another_projects(context: IsolationContext) -> None:
    foreign, other_project = context.responses[-2:]
    _not_found(foreign, "session_not_found")
    _not_found(other_project, "session_not_found")
    assert without_correlation(foreign) == without_correlation(other_project)


def _a_and_b_with_indexes(context: IsolationContext) -> None:
    _two_projects(context)
    seed_served_index(context, "X", "A")
    seed_served_index(context, "Y", "B")


async def _alice_in_a_and_her_trace(context: IsolationContext) -> None:
    response = await _ask(context, "alice", "A")
    meta = _events(response)[0]
    if context.cosmos is None:
        raise AssertionError("the retrieval-scope entry reads the stored trace")
    container = context.cosmos.get_container_client(CONVERSATIONS.name)
    context.found["trace"] = await container.read_item(
        str(turn_trace_id(uuid.UUID(meta["turnId"]))),
        [str(context.principals["alice"].oid), str(meta["sessionId"])],
    )


def _retrieval_in_as_index_only(context: IsolationContext) -> None:
    retrieval = context.found["trace"]["retrieval"]
    assert retrieval["searchIndexId"] == str(context.ids["X"])
    assert retrieval["kept"], "the served document of X is kept"
    assert {entry["documentId"] for entry in retrieval["kept"]} <= {str(context.ids["DX"])}
    assert _searched(context) == [context.names["X"]]


def _x_shared_by_a_and_b(context: IsolationContext) -> None:
    _two_projects(context)
    seed_served_index(context, "X", "A", "B")


async def _alice_in_a_and_bob_in_b(context: IsolationContext) -> None:
    await _ask(context, "alice", "A")
    await _ask(context, "bob", "B")


def _both_receive_sources_from_x(context: IsolationContext) -> None:
    for response in context.responses:
        events = _events(response)
        citations = next(event for event in events if event["type"] == "citations")
        assert citations["citations"], "a source of X was cited"
    assert _searched(context) == [context.names["X"], context.names["X"]]


def _unmapped_override_with_a_default(context: IsolationContext) -> None:
    _unmapped_override_setup(context)
    seed_served_index(context, "X", "A")


async def _override_then_alice_asks(context: IsolationContext) -> None:
    await _set_unmapped_override(context)
    await _ask(context, "alice", "A")


def _override_refused_and_a_default_used(context: IsolationContext) -> None:
    assert context.refused == ["IntegrityError"]
    (response,) = context.responses
    assert _events(response)[-1]["type"] == "done"
    assert _searched(context) == [context.names["X"]]


CHAT: Final[tuple[Entry, ...]] = (
    Entry(
        name="chat.other_project",
        owner="chat",
        data_path="chat",
        actor="alice",
        setup=_b_with_its_index,
        action=_alice_in_b,
        expected=_project_not_found_and_no_model_call,
        negative=True,
        description="alice's turn in project B gets 404 project_not_found and makes no model call",
    ),
    Entry(
        name="chat.session_other_project",
        owner="chat",
        data_path="chat",
        actor="alice",
        setup=_a_and_c_for_alice,
        action=_own_session_of_c_in_a,
        expected=_session_not_found,
        negative=True,
        description="alice's own session of project C sent with project A gets 404 "
        "session_not_found",
    ),
    Entry(
        name="chat.foreign_session",
        owner="chat",
        data_path="chat",
        actor="alice",
        setup=_a_and_c_for_alice,
        action=_bobs_session_in_a,
        expected=_foreign_session_like_another_projects,
        negative=True,
        description="bob's session in A sent by alice gets 404 session_not_found, its body "
        "identical to that of her own session of another project",
    ),
    Entry(
        name="chat.retrieval_scope",
        owner="chat",
        data_path="chat",
        actor="alice",
        setup=_a_and_b_with_indexes,
        action=_alice_in_a_and_her_trace,
        expected=_retrieval_in_as_index_only,
        negative=False,
        description="alice's turn in A searches A's index, and every kept source is a document "
        "of that index",
    ),
    Entry(
        name="retrieval.shared_index",
        owner="chat",
        data_path="retrieval",
        actor="alice",
        setup=_x_shared_by_a_and_b,
        action=_alice_in_a_and_bob_in_b,
        expected=_both_receive_sources_from_x,
        negative=False,
        description="with index X the default of A and of B, alice in A and bob in B both get "
        "sources from X",
    ),
    Entry(
        name="retrieval.override_unmapped",
        owner="chat",
        data_path="retrieval",
        actor="alice",
        setup=_unmapped_override_with_a_default,
        action=_override_then_alice_asks,
        expected=_override_refused_and_a_default_used,
        negative=True,
        description="an override naming project B's index is refused by the composite foreign "
        "key, and alice's next turn in A searches A's default",
    ),
)


# --- Citations (`chat`, and the mapping entry of `ia`) --------------------------------------


def _cited_index(context: IsolationContext) -> None:
    """`_two_projects` with `bob` a member of A too, then index X mapped to A as its default,
    ready with an active physical index, holding one live document whose ingested version 1 is
    current, rendered whole and served by a chunk set that parsed that version's PDF; their IDs
    are `X`, `DX`, `VX` and `CX`. The turns `TA` (`alice`'s, session `SA`) and `TB` (`bob`'s,
    session `SB`), both in A and citing that document as citation 1, are named here and written
    by the action."""
    _two_projects(context)
    bob = context.principals["bob"]
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    document = document_row(index["id"])
    version = document_version_row(
        document["id"],
        index["id"],
        context.ids["A"],
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
        page_count=1,
        document_pdf_status="built",
    )
    chunk_set = chunk_set_row(
        document["id"],
        version["id"],
        physical["id"],
        state="served",
        pdf_sha256=version["sha256"],
        chunk_count=1,
    )
    context.put(
        (project_members, project_member_row(bob.oid, context.ids["A"])),
        (search_indexes, index),
        (physical_indexes, physical),
        (project_indexes, project_index_row(context.ids["A"], index["id"], is_default=True)),
        (documents, document),
        (document_versions, version),
        (chunk_sets, chunk_set),
        (
            served_chunk_sets,
            served_chunk_set_row(document["id"], physical["id"], chunk_set["id"], version["id"]),
        ),
    )
    with context.admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == document["id"])
            .values(current_version_id=version["id"])
        )
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    client_a, client_b = uuid.uuid4(), uuid.uuid4()
    context.ids |= {
        "X": index["id"],
        "DX": document["id"],
        "VX": version["id"],
        "CX": chunk_set["id"],
        "SA": session_a,
        "SB": session_b,
        "TA": turn_id(session_a, client_a),
        "TB": turn_id(session_b, client_b),
        "CA": client_a,
        "CB": client_b,
    }


async def _write_turns_and_renditions(context: IsolationContext) -> None:
    """The turns of `_cited_index` in the emulator, X's renditions in Azurite, and `alice`'s
    download windows and leases cleared, so the entries of a minute never meet the limit."""
    conversations = context.cosmos.get_container_client(CONVERSATIONS.name)
    citation = stored_citation(
        number=1,
        document_id=context.ids["DX"],
        document_version_id=context.ids["VX"],
        chunk_set_id=context.ids["CX"],
        search_index_id=context.ids["X"],
    )
    turns: tuple[tuple[PrincipalName, str, str], ...] = (("alice", "SA", "CA"), ("bob", "SB", "CB"))
    for owner, session, client in turns:
        document = turn_document(
            user_id=context.principals[owner].oid,
            session_id=context.ids[session],
            client_turn_id=context.ids[client],
            project_id=context.ids["A"],
            citations=[citation],
        )
        await conversations.upsert_item(document.to_item())
    for path in (
        rendition_page_path(context.ids["X"], context.ids["DX"], 1, 1),
        rendition_document_path(context.ids["X"], context.ids["DX"], 1),
    ):
        await context.renditions.upload_blob(path, RENDITION, overwrite=True)
    usage = context.cosmos.get_container_client(USAGE.name)
    minute = epoch_minute(cosmos_now())
    for item in (
        download_rate_window_id(minute - 1),
        download_rate_window_id(minute),
        download_lease_id(1),
        download_lease_id(2),
    ):
        with contextlib.suppress(CosmosResourceNotFoundError):
            await usage.delete_item(item, str(context.principals["alice"].oid))


async def _open_citation(context: IsolationContext, session: str, turn: str) -> None:
    """`alice` on the three citation operations for citation 1 of the turn `turn`."""
    base = f"/v1/sessions/{context.ids[session]}/turns/{context.ids[turn]}/citations/1"
    for suffix in CITATION_FILES:
        context.responses.append(
            await context.client.get(base + suffix, headers=context.headers("alice"))
        )


async def _bobs_turn(context: IsolationContext) -> None:
    await _write_turns_and_renditions(context)
    await _open_citation(context, "SB", "TB")


def _set_membership(context: IsolationContext, *, member: bool) -> None:
    alice = context.principals["alice"]
    with context.admin.begin() as connection:
        if member:
            connection.execute(
                insert(project_members), project_member_row(alice.oid, context.ids["A"])
            )
        else:
            connection.execute(
                delete(project_members).where(
                    project_members.c.user_object_id == alice.oid,
                    project_members.c.project_id == context.ids["A"],
                )
            )


async def _revoked_then_restored(context: IsolationContext) -> None:
    await _write_turns_and_renditions(context)
    _set_membership(context, member=False)
    await _open_citation(context, "SA", "TA")
    _set_membership(context, member=True)
    await _open_citation(context, "SA", "TA")


async def _superseded(context: IsolationContext) -> None:
    """A version 2 of the cited document is ingested and becomes current; the cited version
    is superseded first, because a document holds at most one ingested version
    (`UX_document_versions_one_ingested`)."""
    await _write_turns_and_renditions(context)
    version = document_version_row(
        context.ids["DX"],
        context.ids["X"],
        context.ids["A"],
        version_number=2,
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
        page_count=1,
        document_pdf_status="built",
    )
    with context.admin.begin() as connection:
        connection.execute(
            update(document_versions)
            .where(document_versions.c.id == context.ids["VX"])
            .values(state="superseded", superseded_at=sql_now())
        )
    context.put((document_versions, version))
    with context.admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == context.ids["DX"])
            .values(current_version_id=version["id"])
        )
    await _open_citation(context, "SA", "TA")


async def _unmapped_in_sql(context: IsolationContext) -> None:
    await _write_turns_and_renditions(context)
    with context.admin.begin() as connection:
        connection.execute(
            delete(project_indexes).where(
                project_indexes.c.project_id == context.ids["A"],
                project_indexes.c.search_index_id == context.ids["X"],
            )
        )
    await _open_citation(context, "SA", "TA")


async def _unmapped_by_carol(context: IsolationContext) -> None:
    """`carol` unmaps X from A with `adminUnmapProjectIndex`; then `alice` opens her citation."""
    await _write_turns_and_renditions(context)
    with context.admin.connect() as connection:
        row_version = connection.execute(
            select(projects.c.row_version).where(projects.c.id == context.ids["A"])
        ).scalar_one()
    unmapped = await context.client.delete(
        f"/v1/admin/projects/{context.ids['A']}/indexes/{context.ids['X']}",
        headers=context.headers("carol")
        | {"If-Match": f'"{encode_row_version(bytes(row_version))}"'},
    )
    assert unmapped.status_code == 200, unmapped.status_code
    assert unmapped.json()["removed"] is True
    await _open_citation(context, "SA", "TA")


def _answers(responses: Sequence[httpx.Response], status: int, code: str) -> None:
    assert len(responses) == len(CITATION_FILES)
    for response in responses:
        assert response.status_code == status, response.status_code
        assert response.json()["code"] == code


def _only_alices_partition(context: IsolationContext) -> None:
    if context.audit is not None:
        assert context.audit.user_ids() <= {str(context.principals["alice"].oid)}
        context.audit.check()


def _all_citation_not_found(context: IsolationContext) -> None:
    _answers(context.responses, 404, "citation_not_found")
    _only_alices_partition(context)


def _hidden_then_shown(context: IsolationContext) -> None:
    revoked, restored = context.responses[:3], context.responses[3:]
    _answers(revoked, 404, "citation_not_found")
    detail, page, whole = restored
    assert detail.status_code == 200, detail.status_code
    assert detail.json()["number"] == 1
    for response in (page, whole):
        assert response.status_code == 200, response.status_code
        assert response.headers["content-type"] == "application/pdf"
        assert response.content == RENDITION
    _only_alices_partition(context)


def _all_document_changed(context: IsolationContext) -> None:
    _answers(context.responses, 410, "document_changed")
    _only_alices_partition(context)


CITATIONS: Final[tuple[Entry, ...]] = (
    Entry(
        name="citations.foreign_turn",
        owner="chat",
        data_path="citations",
        actor="alice",
        setup=_cited_index,
        action=_bobs_turn,
        expected=_all_citation_not_found,
        negative=True,
        description="alice calls getCitation, getCitationContent and getCitationDocument with "
        "bob's session and turn IDs in A: 404 citation_not_found on all three",
    ),
    Entry(
        name="citations.revoked",
        owner="chat",
        data_path="citations",
        actor="alice",
        setup=_cited_index,
        action=_revoked_then_restored,
        expected=_hidden_then_shown,
        negative=True,
        description="after alice's membership in A is removed, the three operations on her own "
        "turn in A answer 404 citation_not_found; after it is restored, 200",
    ),
    Entry(
        name="citations.superseded",
        owner="chat",
        data_path="citations",
        actor="alice",
        setup=_cited_index,
        action=_superseded,
        expected=_all_document_changed,
        negative=True,
        description="after a new version of the cited document is ingested, the three "
        "operations answer 410 document_changed",
    ),
    Entry(
        name="citations.unmapped_index",
        owner="chat",
        data_path="citations",
        actor="alice",
        setup=_cited_index,
        action=_unmapped_in_sql,
        expected=_all_citation_not_found,
        negative=True,
        description="after the cited index is unmapped from A, the three operations answer "
        "404 citation_not_found",
    ),
    Entry(
        name="ia.mappings.unmapped_hides_citations",
        owner="ia",
        data_path="mappings",
        actor="alice",
        setup=_cited_index,
        action=_unmapped_by_carol,
        expected=_all_citation_not_found,
        negative=True,
        description="unmapping an index (adminUnmapProjectIndex) hides A's citations of that "
        "index: 404 citation_not_found",
    ),
)

# --- Conversations, memory and feedback (`conv`) --------------------------------------------

ISOLATION_FACT: Final = {"text": "Works on supplier contract renewals", "category": "topic"}
"""The `updateMemoryFact` body the memory entries send: a text every validator accepts."""


def _both_in_both(context: IsolationContext) -> None:
    """`_two_projects`, then `alice` a member of B and `bob` of A too: each reaches both
    projects, so only the partition key keeps their sessions, turns and facts apart."""
    _two_projects(context)
    context.put(
        (project_members, project_member_row(context.principals["alice"].oid, context.ids["B"])),
        (project_members, project_member_row(context.principals["bob"].oid, context.ids["A"])),
    )


def _mismatch_world(context: IsolationContext) -> None:
    """`_both_in_both` with a served index the default of B, so a turn in B passes the project
    and index checks and reaches the session check."""
    _both_in_both(context)
    seed_served_index(context, "Y", "B")


def _emulator(context: IsolationContext, name: str) -> Any:
    if context.cosmos is None:
        raise AssertionError("a conversation entry reads and writes the emulator")
    return context.cosmos.get_container_client(name)


async def _seed_session(
    context: IsolationContext, owner: PrincipalName, label: str, project: str, turns: int = 2
) -> None:
    """A session `label` of `owner` in project `project` holding `turns` completed turns, whose
    IDs are `<label>T1`, `<label>T2` and so on."""
    conversations = _emulator(context, CONVERSATIONS.name)
    oid, session_id = context.principals[owner].oid, uuid.uuid4()
    session = session_document(
        user_id=oid, session_id=session_id, project_id=context.ids[project], turn_count=turns
    )
    await conversations.upsert_item(session.to_item())
    for seq in range(1, turns + 1):
        turn = turn_document(
            user_id=oid, session_id=session_id, project_id=context.ids[project], seq=seq
        )
        await conversations.upsert_item(turn.to_item())
        context.ids[f"{label}T{seq}"] = turn.id
    context.ids[label] = session_id


async def _read_stored(context: IsolationContext, key: str, name: str, *partition: str) -> None:
    """The stored item `name` of the partition, kept in `found[key]` for the check."""
    memory = len(partition) == 1
    container = _emulator(context, MEMORIES.name if memory else CONVERSATIONS.name)
    try:
        context.found[key] = await container.read_item(
            name, partition[0] if memory else list(partition)
        )
    except CosmosResourceNotFoundError:
        context.found[key] = None


async def _seed_memory(context: IsolationContext, owner: PrincipalName, facts: int) -> None:
    """`owner`'s memory with `facts` facts, `F<owner><n>`; the stored item in `found`."""
    oid = context.principals[owner].oid
    document = memory_document(user_id=oid, facts=[fact() for _ in range(facts)])
    context.found[f"memory:{owner}"] = await _emulator(context, MEMORIES.name).upsert_item(
        document.to_item()
    )
    for index, item in enumerate(document.facts):
        context.ids[f"F{owner}{index}"] = item.id


async def _get(context: IsolationContext, caller: PrincipalName, path: str, **params: Any) -> None:
    context.responses.append(
        await context.client.get(path, params=params, headers=context.headers(caller))
    )


def _session_ids(response: httpx.Response) -> list[uuid.UUID]:
    assert response.status_code == 200, response.status_code
    return [uuid.UUID(item["sessionId"]) for item in response.json()["items"]]


def _only_partitions_of(context: IsolationContext, *callers: PrincipalName) -> None:
    """Every partition key the app used is one of the callers' own."""
    if context.audit is not None:
        own = {str(context.principals[caller].oid) for caller in callers}
        assert context.audit.user_ids() <= own
        context.audit.check()


async def _alice_lists_beside_bob(context: IsolationContext) -> None:
    await _seed_session(context, "bob", "SB1", "A")
    await _seed_session(context, "bob", "SB2", "A")
    await _seed_session(context, "alice", "SA", "A")
    await _get(context, "alice", "/v1/sessions", limit=50)
    await _get(context, "alice", "/v1/sessions", limit=50, projectId=str(context.ids["A"]))


def _only_alices_session(context: IsolationContext) -> None:
    assert len(context.responses) == 2
    for response in context.responses:
        assert _session_ids(response) == [context.ids["SA"]]
    _only_partitions_of(context, "alice")


async def _bobs_session_read_by_others(context: IsolationContext) -> None:
    await _seed_session(context, "bob", "S", "A")
    for caller in ("alice", "carol"):
        await _get(context, caller, f"/v1/sessions/{context.ids['S']}")
        await _get(context, caller, f"/v1/sessions/{context.ids['S']}/turns")


def _all_session_not_found(context: IsolationContext) -> None:
    assert len(context.responses) == 4
    for response in context.responses:
        _not_found(response, "session_not_found")
    _only_partitions_of(context, "alice", "carol")


async def _session_reads(context: IsolationContext) -> None:
    session = context.ids["S"]
    await _get(context, "alice", "/v1/sessions", limit=50)
    await _get(context, "alice", f"/v1/sessions/{session}")
    await _get(context, "alice", f"/v1/sessions/{session}/turns")


async def _sessions_revoked_then_restored(context: IsolationContext) -> None:
    await _seed_session(context, "alice", "S", "A")
    _set_membership(context, member=False)
    await _session_reads(context)
    _set_membership(context, member=True)
    await _session_reads(context)


def _sessions_hidden_then_shown(context: IsolationContext) -> None:
    listed, read, page, *restored = context.responses
    assert context.ids["S"] not in _session_ids(listed)
    _not_found(read, "session_not_found")
    _not_found(page, "session_not_found")
    listed, read, page = restored
    assert context.ids["S"] in _session_ids(listed)
    turns = [context.ids["ST1"], context.ids["ST2"]]
    assert read.status_code == 200, read.status_code
    assert [uuid.UUID(turn["turnId"]) for turn in read.json()["turns"]] == turns
    assert page.status_code == 200, page.status_code
    assert [uuid.UUID(turn["turnId"]) for turn in page.json()["items"]] == turns
    _only_partitions_of(context, "alice")


async def _own_session_sent_with_b(context: IsolationContext) -> None:
    await _seed_session(context, "alice", "S", "A", turns=1)
    await _ask(context, "alice", "B", str(context.ids["S"]))
    oid = str(context.principals["alice"].oid)
    await _read_stored(context, "session", str(context.ids["S"]), oid, str(context.ids["S"]))


def _session_not_found_and_no_turn(context: IsolationContext) -> None:
    (response,) = context.responses
    _not_found(response, "session_not_found")
    assert context.found["session"]["turnCount"] == 1
    assert context.found["session"]["activeTurn"] is None
    assert _chat(context).model.requests == []


async def _bobs_fact_edited_by_others(context: IsolationContext) -> None:
    await _seed_memory(context, "bob", 1)
    fact_path = f"/v1/memory/facts/{context.ids['Fbob0']}"
    for caller in ("alice", "carol"):
        own = await context.client.get("/v1/memory", headers=context.headers(caller))
        assert own.status_code == 200, own.status_code
        headers = context.headers(caller) | {"If-Match": own.json()["etag"]}
        context.responses.append(
            await context.client.put(fact_path, json=ISOLATION_FACT, headers=headers)
        )
        context.responses.append(await context.client.delete(fact_path, headers=headers))
    bob = str(context.principals["bob"].oid)
    await _read_stored(context, "memory:bob:after", bob, bob)


def _bobs_memory_unchanged(context: IsolationContext) -> None:
    before, after = context.found["memory:bob"], context.found["memory:bob:after"]
    assert after is not None
    assert after["_etag"] == before["_etag"]
    assert after["facts"] == before["facts"]


def _fact_not_found_and_bob_unchanged(context: IsolationContext) -> None:
    assert len(context.responses) == 4
    for response in context.responses:
        _not_found(response, "memory_fact_not_found")
    _bobs_memory_unchanged(context)
    _only_partitions_of(context, "alice", "carol")


async def _memories_read_and_cleared(context: IsolationContext) -> None:
    await _seed_memory(context, "alice", 2)
    await _seed_memory(context, "bob", 2)
    # The shared principals keep their partitions for the whole session: carol starts with none.
    carol = str(context.principals["carol"].oid)
    with contextlib.suppress(CosmosResourceNotFoundError):
        await _emulator(context, MEMORIES.name).delete_item(carol, carol)
    for caller in ("alice", "carol"):
        read = await context.client.get("/v1/memory", headers=context.headers(caller))
        context.responses.append(read)
        assert read.status_code == 200, read.status_code
        headers = context.headers(caller) | {"If-Match": read.json()["etag"]}
        context.responses.append(await context.client.delete("/v1/memory/facts", headers=headers))
    bob = str(context.principals["bob"].oid)
    await _read_stored(context, "memory:bob:after", bob, bob)


def _fact_ids(response: httpx.Response) -> list[uuid.UUID]:
    assert response.status_code == 200, response.status_code
    return [uuid.UUID(item["factId"]) for item in response.json()["facts"]]


def _each_reads_and_clears_their_own(context: IsolationContext) -> None:
    alice_read, alice_clear, carol_read, carol_clear = context.responses
    assert set(_fact_ids(alice_read)) == {context.ids["Falice0"], context.ids["Falice1"]}
    assert _fact_ids(carol_read) == []
    assert _fact_ids(alice_clear) == []
    assert _fact_ids(carol_clear) == []
    _bobs_memory_unchanged(context)
    _only_partitions_of(context, "alice", "carol")


def _feedback_path(context: IsolationContext, session: str) -> str:
    return f"/v1/sessions/{context.ids[session]}/turns/{context.ids[session + 'T1']}/feedback"


async def _put_feedback(context: IsolationContext, caller: PrincipalName, session: str) -> None:
    context.responses.append(
        await context.client.put(
            _feedback_path(context, session),
            json={"rating": "up"},
            headers=context.headers(caller),
        )
    )


async def _bobs_turn_rated_by_others(context: IsolationContext) -> None:
    await _seed_session(context, "bob", "S", "A", turns=1)
    for caller in ("alice", "carol"):
        await _put_feedback(context, caller, "S")
    bob, session = str(context.principals["bob"].oid), str(context.ids["S"])
    await _read_stored(context, "turn", str(context.ids["ST1"]), bob, session)


def _turn_not_found_and_feedback_unchanged(context: IsolationContext) -> None:
    assert len(context.responses) == 2
    for response in context.responses:
        _not_found(response, "turn_not_found")
    assert context.found["turn"]["feedback"] is None
    _only_partitions_of(context, "alice", "carol")


async def _feedback_revoked_then_restored(context: IsolationContext) -> None:
    await _seed_session(context, "alice", "S", "A", turns=1)
    _set_membership(context, member=False)
    await _put_feedback(context, "alice", "S")
    _set_membership(context, member=True)
    await _put_feedback(context, "alice", "S")


def _feedback_refused_then_saved(context: IsolationContext) -> None:
    revoked, restored = context.responses
    _not_found(revoked, "turn_not_found")
    assert restored.status_code == 200, restored.status_code
    assert restored.json()["rating"] == "up"
    _only_partitions_of(context, "alice")


CONVERSATIONS_AND_MEMORY: Final[tuple[Entry, ...]] = (
    Entry(
        name="conv.sessions.list_own_only",
        owner="conv",
        data_path="sessions",
        actor="alice",
        setup=_both_in_both,
        action=_alice_lists_beside_bob,
        expected=_only_alices_session,
        negative=True,
        description="with bob's 2 sessions and alice's 1 in A, alice's listSessions, filtered to "
        "A or not, holds only her session",
    ),
    Entry(
        name="conv.sessions.other_user",
        owner="conv",
        data_path="sessions",
        actor="alice",
        setup=_both_in_both,
        action=_bobs_session_read_by_others,
        expected=_all_session_not_found,
        negative=True,
        description="alice and carol get 404 session_not_found from getSession and "
        "listSessionTurns on bob's session",
    ),
    Entry(
        name="conv.sessions.revoked_and_restored",
        owner="conv",
        data_path="sessions",
        actor="alice",
        setup=_both_in_both,
        action=_sessions_revoked_then_restored,
        expected=_sessions_hidden_then_shown,
        negative=True,
        description="removed from A, alice's session in A is absent from her list and its reads "
        "answer 404 session_not_found; restored, it is listed and readable with both turns",
    ),
    Entry(
        name="conv.sessions.project_mismatch",
        owner="conv",
        data_path="sessions",
        actor="alice",
        setup=_mismatch_world,
        action=_own_session_sent_with_b,
        expected=_session_not_found_and_no_turn,
        negative=True,
        description="alice's session of A sent to createChatTurn with project B answers 404 "
        "session_not_found and creates no turn",
    ),
    Entry(
        name="conv.memory.other_user_fact",
        owner="conv",
        data_path="memory",
        actor="alice",
        setup=_both_in_both,
        action=_bobs_fact_edited_by_others,
        expected=_fact_not_found_and_bob_unchanged,
        negative=True,
        description="alice and carol, each with their own etag, get 404 memory_fact_not_found "
        "from updateMemoryFact and deleteMemoryFact on bob's fact; bob's document is unchanged",
    ),
    Entry(
        name="conv.memory.own_document_only",
        owner="conv",
        data_path="memory",
        actor="alice",
        setup=_both_in_both,
        action=_memories_read_and_cleared,
        expected=_each_reads_and_clears_their_own,
        negative=True,
        description="alice and carol read and clear only their own memory; bob's facts are "
        "unchanged",
    ),
    Entry(
        name="conv.feedback.other_user",
        owner="conv",
        data_path="feedback",
        actor="alice",
        setup=_both_in_both,
        action=_bobs_turn_rated_by_others,
        expected=_turn_not_found_and_feedback_unchanged,
        negative=True,
        description="alice and carol get 404 turn_not_found from putTurnFeedback on bob's turn, "
        "whose feedback stays unset",
    ),
    Entry(
        name="conv.feedback.revoked_and_restored",
        owner="conv",
        data_path="feedback",
        actor="alice",
        setup=_both_in_both,
        action=_feedback_revoked_then_restored,
        expected=_feedback_refused_then_saved,
        negative=True,
        description="removed from A, alice's feedback on her turn in A answers 404 "
        "turn_not_found; restored, 200",
    ),
)


def _a_with_its_index(context: IsolationContext) -> None:
    _two_projects(context)
    seed_served_index(context, "X", "A")


async def _asks_while_revoked_and_restored(context: IsolationContext) -> None:
    """`alice` asks in A; removed from A she asks again and lists her sessions; added back she
    lists them once more."""
    session = _session_of(await _ask(context, "alice", "A"))
    context.ids["S"] = uuid.UUID(session)
    _set_membership(context, member=False)
    await _ask(context, "alice", "A")
    await _get(context, "alice", "/v1/sessions", limit=50)
    _set_membership(context, member=True)
    await _get(context, "alice", "/v1/sessions", limit=50)


def _refused_and_hidden_then_shown(context: IsolationContext) -> None:
    _, refused, hidden, shown = context.responses
    _not_found(refused, "project_not_found")
    assert context.ids["S"] not in _session_ids(hidden)
    assert context.ids["S"] in _session_ids(shown)


async def _asks_then_archived(context: IsolationContext) -> None:
    """`alice` asks in A; A is archived; then `alice` and `carol` list their projects and ask in
    A, and `alice` reads her session and rates and opens citation 1 of her turn."""
    first = _events(await _ask(context, "alice", "A"))
    session, turn = str(first[0]["sessionId"]), str(first[0]["turnId"])
    _archive_a(context)
    context.responses.clear()
    await _listing("alice", "carol")(context)
    await _ask(context, "alice", "A")
    await _ask(context, "carol", "A")
    await _get(context, "alice", f"/v1/sessions/{session}")
    context.responses.append(
        await context.client.put(
            f"/v1/sessions/{session}/turns/{turn}/feedback",
            json={"rating": "up"},
            headers=context.headers("alice"),
        )
    )
    base = f"/v1/sessions/{session}/turns/{turn}/citations/1"
    for suffix in CITATION_FILES:
        context.responses.append(
            await context.client.get(base + suffix, headers=context.headers("alice"))
        )


def _archived_hidden_on_every_chat_path(context: IsolationContext) -> None:
    listed, (alice_turn, carol_turn), (session, feedback), citations = (
        context.responses[:2],
        context.responses[2:4],
        context.responses[4:6],
        context.responses[6:],
    )
    _lists_neither_archived(listed, context.ids["A"])
    _not_found(alice_turn, "project_not_found")
    _not_found(carol_turn, "project_not_found")
    # The operations that name a session, a turn or a citation answer their own not-found code,
    # with the body of a session, turn or citation that doesn't exist.
    _not_found(session, "session_not_found")
    _not_found(feedback, "turn_not_found")
    _answers(citations, 404, "citation_not_found")


def _a_and_b_with_their_indexes(context: IsolationContext) -> None:
    """`_two_projects`, X the default of A and Y of B, and W mapped to A beside X."""
    _a_and_b_with_indexes(context)
    seed_served_index(context, "W", "A", default=False)


async def _alice_in_a_bob_in_b_with_alices_trace(context: IsolationContext) -> None:
    await _alice_in_a_and_her_trace(context)
    await _ask(context, "bob", "B")


def _a_resolves_only_its_own_index(context: IsolationContext) -> None:
    assert context.found["trace"]["retrieval"]["searchIndexId"] == str(context.ids["X"])
    alice, bob = context.responses
    assert _events(alice)[-1]["type"] == "done"
    assert _events(bob)[-1]["type"] == "done"
    # A's turn searched only A's default; B's index served B's turn and never A's.
    assert _searched(context) == [context.names["X"], context.names["Y"]]


def _expired_override(context: IsolationContext) -> None:
    """A with X as its default and W mapped beside it; `alice`'s override to W has expired."""
    _a_with_its_index(context)
    seed_served_index(context, "W", "A", default=False)
    expired = sql_now() - OVERRIDE_EXPIRED_FOR
    context.put(
        (
            user_index_overrides,
            user_index_override_row(
                context.principals["alice"].oid,
                context.ids["A"],
                context.ids["W"],
                set_at=expired - OVERRIDE_SET_FOR,
                expires_at=expired,
            ),
        )
    )


async def _alice_asks_in_a(context: IsolationContext) -> None:
    await _ask(context, "alice", "A")


def _default_used(context: IsolationContext) -> None:
    (response,) = context.responses
    assert _events(response)[-1]["type"] == "done"
    assert _searched(context) == [context.names["X"]]


def _alices_override_beside_bob(context: IsolationContext) -> None:
    """A with `alice` and `bob`, X its default and W mapped beside it; `alice` has an unexpired
    override to W."""
    _a_with_its_index(context)
    context.put(
        (project_members, project_member_row(context.principals["bob"].oid, context.ids["A"]))
    )
    seed_served_index(context, "W", "A", default=False)
    context.put(
        (
            user_index_overrides,
            user_index_override_row(
                context.principals["alice"].oid, context.ids["A"], context.ids["W"]
            ),
        )
    )


async def _alice_then_bob_in_a(context: IsolationContext) -> None:
    await _ask(context, "alice", "A")
    await _ask(context, "bob", "A")


def _bob_keeps_the_default(context: IsolationContext) -> None:
    for response in context.responses:
        assert _events(response)[-1]["type"] == "done"
    assert _searched(context) == [context.names["W"], context.names["X"]]


OVERRIDE_SET_FOR: Final = timedelta(days=7)
OVERRIDE_EXPIRED_FOR: Final = timedelta(hours=1)
"""An expired override: set 7 days before it expired, an hour ago."""


IDENTITY_AND_ACCESS_SESSIONS: Final[tuple[Entry, ...]] = (
    Entry(
        name="ia.members.revoked_and_restored",
        owner="ia",
        data_path="members",
        actor="alice",
        setup=_a_with_its_index,
        action=_asks_while_revoked_and_restored,
        expected=_refused_and_hidden_then_shown,
        negative=True,
        description="removed from A, alice's next createChatTurn in A answers 404 "
        "project_not_found and her sessions of A are hidden; added back, they show again",
    ),
    Entry(
        name="ia.projects.archived_hidden",
        owner="ia",
        data_path="projects",
        actor="alice",
        setup=_a_with_its_index,
        action=_asks_then_archived,
        expected=_archived_hidden_on_every_chat_path,
        negative=True,
        description="an archived project disappears from both lists and answers 404 "
        "project_not_found to createChatTurn, for carol and alice alike, while its session, "
        "feedback and citations answer their own 404",
    ),
    Entry(
        name="ia.mappings.mapped_only",
        owner="ia",
        data_path="mappings",
        actor="alice",
        setup=_a_and_b_with_their_indexes,
        action=_alice_in_a_bob_in_b_with_alices_trace,
        expected=_a_resolves_only_its_own_index,
        negative=True,
        description="chat in A resolves only an index mapped to A; the index mapped only to B "
        "is never used for A",
    ),
    Entry(
        name="ia.overrides.expired_ignored",
        owner="ia",
        data_path="overrides",
        actor="alice",
        setup=_expired_override,
        action=_alice_asks_in_a,
        expected=_default_used,
        negative=True,
        description="an expired override is ignored and the project default is used",
    ),
    Entry(
        name="ia.overrides.other_user_unchanged",
        owner="ia",
        data_path="overrides",
        actor="bob",
        setup=_alices_override_beside_bob,
        action=_alice_then_bob_in_a,
        expected=_bob_keeps_the_default,
        negative=True,
        description="alice's override never changes bob's resolution",
    ),
)
"""The identity-and-access entries whose action is a chat turn, a session read or a citation."""

DATA_PATHS: Final[tuple[Entry, ...]] = (
    *IDENTITY_AND_ACCESS,
    *INGESTION,
    *CHAT,
    *CITATIONS,
    *CONVERSATIONS_AND_MEMORY,
    *IDENTITY_AND_ACCESS_SESSIONS,
)
"""Every registered entry, owner by owner."""
