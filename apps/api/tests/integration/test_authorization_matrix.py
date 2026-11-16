"""The authorisation matrix test (T-001; `84-testing.md` section 3; `11-api.md` acceptance
criterion 1).

The cases come from the document the app under test exports, built at collection time outside
any event loop, so an unexported route can't hide; the app runs against the session's SQL Server
database with the permissive limiters. The seeded world grows with the operations: each stage
adds the kinds its scoped parameters name to `world`.

A mapped index refuses deletion (`409 index_mapped`, `11-api.md` section 3.10.6), so the
operations of `UNMAPPED_INDEX_OPERATIONS` name an unmapped index of the same world instead of X.

The citation operations read the caller's turn from the Cosmos DB emulator (the app's containers
are the session database's, through `EmulatorContainers`) and the renditions from Azurite: `alice`
has a turn in A citing X's ingested document (citation 1, which opens) and Y's (citation 2, whose
index A doesn't map), `bob` a turn in B; citation 16 exists nowhere. Each case starts with the
callers' download windows and leases cleared, so the cases of a minute never add up to the
download limit.

The session, feedback and memory operations read the same emulator: `alice` and `bob` each have
one session holding those two completed turns (the cited one first) and one memory fact; every
case writes the memory documents afresh, so the memory writes send `alice`'s current `etag`.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from sqlalchemy import Engine, Table, insert, select, update

from docchat_api.app import create_app
from docchat_api.chat.citations import renditions_container
from docchat_api.http.queue import ingestion_queue
from docchat_api.routes.admin_uploads import UploadStorage, upload_storage
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.conversations import StoredCitation, turn_id
from docchat_core.cosmos.usage import download_lease_id, download_rate_window_id, epoch_minute
from docchat_core.ingestion.names import rendition_document_path, rendition_page_path
from docchat_core.models import encode_row_version
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.tables.access import project_indexes, project_members, projects, users
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_runs,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_testing.authz_matrix import (
    Caller,
    MatrixCase,
    World,
    case_ids,
    check_case,
    generate,
)
from docchat_testing.builders import project_index_row, project_member_row, project_row, user_row
from docchat_testing.builders.cosmos import (
    cosmos_now,
    fact,
    memory_document,
    memory_if_match,
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
    search_index_row,
    served_chunk_set_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    BuiltApp,
    build_app,
    operation_map,
    ports_under_test,
    raise_chat_limits,
    serving,
)
from docchat_testing.fixtures.cosmos import EmulatorContainers
from docchat_testing.fixtures.identities import PrincipalName, SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import StorageClients
from docchat_testing.tokens import SigningKeys

DOCUMENT = create_app().openapi()
CASES = generate(DOCUMENT)
UNMAPPED_INDEX_OPERATIONS: Final = frozenset({"adminDeleteIndex"})
RENDITION: Final = b"%PDF-1.7\n% a verified rendition\n%%EOF\n"
DOWNLOADERS: Final[tuple[PrincipalName, ...]] = ("alice", "carol")


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def if_match(engine: Engine, table: Table, row_id: Any) -> str:
    """The `If-Match` value naming the row's current `row_version`."""
    with engine.connect() as connection:
        row_version = connection.execute(
            select(table.c.row_version).where(table.c.id == row_id)
        ).scalar_one()
    return f'"{encode_row_version(bytes(row_version))}"'


def ready_index(engine: Engine) -> tuple[UUID, UUID]:
    """A ready index with an active physical index, mapped to no project; both IDs."""
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    put(engine, (search_indexes, index), (physical_indexes, physical))
    with engine.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return UUID(str(index["id"])), UUID(str(physical["id"]))


@dataclass(frozen=True)
class MappedIndex:
    """An index's identifiers: its own, its `pending` version's (the upload operations' version
    kind), its ingested document's (the document operations' kind) with its current version and
    its served chunk set (what a citation names), and its completed run's."""

    index: str
    version: str
    document: str
    run: str
    current_version: str
    chunk_set: str


def mapped_index(engine: Engine, project_id: Any) -> MappedIndex:
    """A ready index, the project's default, holding one document with a `pending` version, one
    live document whose ingested version 1 is current, rendered whole and served by a chunk set
    that parsed that version's PDF, and one completed upload run."""
    index_id, physical_id = ready_index(engine)
    document = document_row(index_id)
    version = document_version_row(
        document["id"], index_id, project_id, state="pending", committed_at=sql_now()
    )
    ingested = document_row(index_id)
    current = document_version_row(
        ingested["id"],
        index_id,
        project_id,
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
        page_count=1,
        document_pdf_status="built",
    )
    chunk_set = chunk_set_row(
        ingested["id"],
        current["id"],
        physical_id,
        state="served",
        pdf_sha256=current["sha256"],
        chunk_count=1,
    )
    run = ingestion_run_row(
        index_id,
        state="completed",
        started_at=sql_now(),
        completed_at=sql_now(),
        total_items=0,
        succeeded_items=0,
        skipped_items=0,
        failed_items=0,
        cancelled_items=0,
    )
    put(
        engine,
        (project_indexes, project_index_row(project_id, index_id, is_default=True)),
        (documents, document),
        (document_versions, version),
        (documents, ingested),
        (document_versions, current),
        (chunk_sets, chunk_set),
        (
            served_chunk_sets,
            served_chunk_set_row(ingested["id"], physical_id, chunk_set["id"], current["id"]),
        ),
        (ingestion_runs, run),
    )
    with engine.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == ingested["id"])
            .values(current_version_id=current["id"])
        )
    return MappedIndex(
        str(index_id),
        str(version["id"]),
        str(ingested["id"]),
        str(run["id"]),
        str(current["id"]),
        str(chunk_set["id"]),
    )


@dataclass(frozen=True)
class Turn:
    """A seeded turn: its owner's name, its session, its client turn ID, its project and its
    owner's memory fact."""

    owner: PrincipalName
    session: UUID
    client_turn: UUID
    project: str
    fact: UUID


@dataclass(frozen=True)
class Seeded:
    """The world, the unmapped index the operations of `UNMAPPED_INDEX_OPERATIONS` name, the two
    mapped indexes and the turns the citation operations read."""

    world: World
    unmapped_index: str
    unmapped_index_version: str
    x: MappedIndex
    y: MappedIndex
    turns: tuple[Turn, Turn]

    def for_operation(self, operation_id: str) -> World:
        if operation_id not in UNMAPPED_INDEX_OPERATIONS:
            return self.world
        return replace(
            self.world,
            reachable={**self.world.reachable, "index": self.unmapped_index},
            versions={**self.world.versions, "index": self.unmapped_index_version},
        )


@pytest.fixture
def seeded(
    sql_database: SqlTestDatabase, principals: dict[PrincipalName, SharedPrincipal]
) -> Iterator[Seeded]:
    """Projects A and B, `alice` a member of A and `bob` of B, index X mapped to A and Y to B as
    their defaults, each with a `pending` version, an ingested document and a completed run
    (section 3.2), and an unmapped index; the `If-Match` operations send the current
    `rowVersion` of A, X or X's ingested document."""
    engine = sql_database.engine()
    try:
        project_a = project_row()
        project_b = project_row()
        alice, bob = principals["alice"], principals["bob"]
        put(
            engine,
            (users, user_row(object_id=alice.oid)),
            (users, user_row(object_id=bob.oid)),
            (projects, project_a),
            (projects, project_b),
            (project_members, project_member_row(alice.oid, project_a["id"])),
            (project_members, project_member_row(bob.oid, project_b["id"])),
        )
        x = mapped_index(engine, project_a["id"])
        y = mapped_index(engine, project_b["id"])
        unmapped, _ = ready_index(engine)
        alices = Turn("alice", uuid4(), uuid4(), str(project_a["id"]), uuid4())
        bobs = Turn("bob", uuid4(), uuid4(), str(project_b["id"]), uuid4())
        world = World(
            reachable={
                "project": str(project_a["id"]),
                "user": str(alice.oid),
                "index": x.index,
                "version": x.version,
                "document": x.document,
                "run": x.run,
                "session": str(alices.session),
                "turn": str(turn_id(alices.session, alices.client_turn)),
                "citation": "1",
                "fact": str(alices.fact),
            },
            unreachable={
                "project": str(project_b["id"]),
                "user": str(bob.oid),
                "index": y.index,
                "version": y.version,
                "document": y.document,
                "run": y.run,
                "session": str(bobs.session),
                "turn": str(turn_id(bobs.session, bobs.client_turn)),
                "citation": "2",
                "fact": str(bobs.fact),
            },
            absent={"citation": "16"},
            versions={
                "project": if_match(engine, projects, project_a["id"]),
                "index": if_match(engine, search_indexes, x.index),
                "document": if_match(engine, documents, x.document),
            },
        )
        yield Seeded(
            world,
            str(unmapped),
            if_match(engine, search_indexes, unmapped),
            x,
            y,
            (alices, bobs),
        )
    finally:
        engine.dispose()


def citation_of(number: int, index: MappedIndex) -> StoredCitation:
    return stored_citation(
        number=number,
        document_id=UUID(index.document),
        document_version_id=UUID(index.current_version),
        chunk_set_id=UUID(index.chunk_set),
        search_index_id=UUID(index.index),
    )


@pytest.fixture
def emulator() -> EmulatorContainers:
    return EmulatorContainers()


@pytest.fixture
def api_app(
    api_sql_engine: SqlEngine,
    sql_database: SqlTestDatabase,
    signing_keys: SigningKeys,
    emulator: EmulatorContainers,
    chat_fakes: ChatFakes,
) -> BuiltApp:
    """The in-process API over the session's database and the emulator's containers, with the
    chat doubles and the chat path's per-user limits raised (`createChatTurn` runs a turn for
    each of its cases)."""
    raise_chat_limits(sql_database)
    ports = replace(
        ports_under_test(sql_engine=api_sql_engine, chat=chat_fakes.ports()),
        container_wrapper=emulator,
    )
    return build_app(ports=ports, signing_keys=signing_keys)


@pytest.fixture
async def api(
    api_app: BuiltApp,
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    emulator: EmulatorContainers,
) -> AsyncIterator[ApiUnderTest]:
    emulator.database = cosmos_database
    try:
        async with serving(api_app) as started:
            started.operations = operation_map()
            yield started
    finally:
        await api_sql_engine.dispose()


@pytest.fixture
async def files(
    seeded: Seeded,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    principals: dict[PrincipalName, SharedPrincipal],
) -> Seeded:
    """The two sessions with their turns and the two memory documents in the emulator, X's page
    and whole-document renditions in Azurite, and the downloaders' windows and leases cleared;
    the world's memory version is `alice`'s fresh `etag`."""
    conversations = cosmos_database.get_container_client(CONVERSATIONS.name)
    memories = cosmos_database.get_container_client(MEMORIES.name)
    alices, bobs = seeded.turns
    memory_etag = ""
    for turn, citations in (
        (alices, [citation_of(1, seeded.x), citation_of(2, seeded.y)]),
        (bobs, [citation_of(1, seeded.y)]),
    ):
        owner = principals[turn.owner].oid
        session = session_document(
            user_id=owner, session_id=turn.session, project_id=UUID(turn.project), turn_count=2
        )
        await conversations.upsert_item(session.to_item())
        document = turn_document(
            user_id=owner,
            session_id=turn.session,
            client_turn_id=turn.client_turn,
            project_id=UUID(turn.project),
            citations=citations,
        )
        await conversations.upsert_item(document.to_item())
        later = turn_document(
            user_id=owner, session_id=turn.session, project_id=UUID(turn.project), seq=2
        )
        await conversations.upsert_item(later.to_item())
        memory = memory_document(user_id=owner, facts=[fact(id=turn.fact)])
        written = await memories.upsert_item(memory.to_item())
        if turn is alices:
            memory_etag = memory_if_match(written)
    renditions = storage.container("renditions")
    x_index, x_document = UUID(seeded.x.index), UUID(seeded.x.document)
    for path in (
        rendition_page_path(x_index, x_document, 1, 1),
        rendition_document_path(x_index, x_document, 1),
    ):
        await renditions.upload_blob(path, RENDITION, overwrite=True)
    usage = cosmos_database.get_container_client(USAGE.name)
    minute = epoch_minute(cosmos_now())
    for name in DOWNLOADERS:
        partition = str(principals[name].oid)
        for item in (
            download_rate_window_id(minute - 1),
            download_rate_window_id(minute),
            download_lease_id(1),
            download_lease_id(2),
        ):
            with contextlib.suppress(CosmosResourceNotFoundError):
                await usage.delete_item(item, partition)
    world = replace(seeded.world, versions={**seeded.world.versions, "memory": memory_etag})
    return replace(seeded, world=world)


def test_t001_every_operation_carries_the_access_extensions() -> None:
    assert not [case.id for case in CASES if case.failure is not None]


def test_t001_the_public_set_is_exactly_get_health() -> None:
    public = {
        (method, path)
        for path, item in DOCUMENT["paths"].items()
        for method, operation in item.items()
        if operation["x-docchat-access"] == "public"
    }
    assert public == {("get", "/healthz")}


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
async def test_t001_authorisation_matrix(
    case: MatrixCase,
    api: ApiUnderTest,
    files: Seeded,
    principals: dict[PrincipalName, SharedPrincipal],
    storage: StorageClients,
) -> None:
    # The upload operations write to Azurite's `documents` container and the session's queue,
    # where the index, document and run-creating operations also send their run messages.
    queue = cast("Any", storage.queue())
    api.app.dependency_overrides[upload_storage] = lambda: UploadStorage(
        documents=cast("Any", storage.container("documents")), queue=queue
    )
    api.app.dependency_overrides[ingestion_queue] = lambda: queue
    renditions = cast("Any", storage.container("renditions"))
    api.app.dependency_overrides[renditions_container] = lambda: renditions

    def token_for(caller: Caller) -> str:
        if caller == "anonymous":
            raise AssertionError("an anonymous case sends no token")
        return principals[caller].token()

    await check_case(case, api.client, files.for_operation(case.operation_id), token_for)
