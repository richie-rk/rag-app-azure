"""T-201, the document-deletion integration test (`82-security.md` SEC-122 and acceptance
criterion 39; `00-overview.md` acceptance criterion 10; `14-ingestion.md` sections 6.12 and 7.1).

Across the API and the ingestion ledger, against SQL Server, the Cosmos DB emulator and Azurite:
`alice` opens her citation of a served document (`200`), `carol` deletes the document with
`adminDeleteDocument`, the same citation answers `410 document_changed` at once, and once the
delete run's clean-up item has run (`purge_document_artifacts`, claimed as the dispatcher claims
it) no chunk of the document is returned by search. Search is the stateful search fake shared by
the API and the ingestion worker, so a chunk the clean-up deletes is gone for every reader.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from azure.cosmos.aio import DatabaseProxy
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, insert, select, update

from docchat_api.http.queue import ingestion_queue
from docchat_core.cosmos.containers import CONVERSATIONS
from docchat_core.ingestion.methods import MethodRegistry
from docchat_core.ingestion.models import ItemSettings
from docchat_core.models import SystemClock, encode_row_version
from docchat_core.search.schema import ChunkIndexFields
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import project_indexes, project_members, projects, users
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_ingestion.activities import claim_ingestion_items, purge_document_artifacts
from docchat_ingestion.activities.documents import DocumentServices
from docchat_ingestion.isolation.pdf_schema import RENDITION_RULES_VERSION
from docchat_ingestion.isolation.runner import ParsePool
from docchat_testing.builders import project_index_row, project_member_row, project_row, user_row
from docchat_testing.builders.cosmos import stored_citation, turn_document
from docchat_testing.builders.ingestion import (
    chunk_set_row,
    document_row,
    document_version_row,
    physical_index_row,
    search_index_row,
    served_chunk_set_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fakes.chat import ChatFakes, search_result
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    BuiltApp,
    build_app,
    emulator_containers,
    ports_under_test,
    serving,
)
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import StorageClients
from docchat_testing.tokens import SigningKeys

CHUNKS: Final = 2
FIELDS: Final = ChunkIndexFields.for_version(1)
SETTINGS: Final = ItemSettings(
    activity_time_budget_seconds=1200,
    max_pages_per_document=2000,
    pagewise_pages_per_extract=40,
    max_parse_seconds_per_document=7200,
    embedding_batch_max_inputs=64,
    embedding_batch_max_tokens=60_000,
    embedding_concurrency=2,
    index_batch_max_documents=100,
    index_batch_max_bytes=8_000_000,
    table_summary_concurrency=4,
    table_summary_max_input_tokens=6000,
    table_summary_max_output_tokens=300,
)


class _NotInThisTest:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the test reached a service it doesn't arrange ({name})")


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
def built(
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    signing_keys: SigningKeys,
) -> BuiltApp:
    ports = replace(
        ports_under_test(sql_engine=api_sql_engine, chat=chat_fakes.ports()),
        container_wrapper=emulator_containers(cosmos_database),
    )
    return build_app(ports=ports, signing_keys=signing_keys)


@pytest.fixture
async def api(
    built: BuiltApp, api_sql_engine: SqlEngine, storage: StorageClients
) -> AsyncIterator[ApiUnderTest]:
    try:
        async with serving(built) as started:
            queue = storage.queue()
            started.app.dependency_overrides[ingestion_queue] = lambda: cast("Any", queue)
            yield started
    finally:
        await api_sql_engine.dispose()


@pytest.fixture
async def ingestion_sql(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    engine = SqlEngine(
        "ingestion",
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=sql_database.connection_string("docchat_ingestion"),
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def services(
    ingestion_sql: SqlEngine, storage: StorageClients, chat_fakes: ChatFakes
) -> DocumentServices:
    """The worker's document services over SQL Server, Azurite and the shared search fake."""
    return DocumentServices(
        sql=ingestion_sql,
        documents=cast("Any", storage.container("documents")),
        renditions=cast("Any", storage.container("renditions")),
        work=cast("Any", storage.container("ingestion-work")),
        search=lambda name: cast("Any", chat_fakes.search(name)),
        openai=_NotInThisTest(),
        parse_pool=cast(ParsePool, _NotInThisTest()),
        methods=MethodRegistry(),
        clock=SystemClock(),
        rendition_rules_version=RENDITION_RULES_VERSION,
        converter_version="lo-4:7.4.7-1+deb12u14/0123456789ab",
    )


def _served_document(admin: Engine, alice: SharedPrincipal) -> dict[str, Any]:
    """Project A with `alice`, its default index X ready with an active physical index, and one
    live document whose ingested version 1 is current and served by a chunk set of 2 chunks."""
    project = project_row()
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    document = document_row(index["id"])
    version = document_version_row(
        document["id"],
        index["id"],
        project["id"],
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
        served_at=sql_now(),
        pdf_sha256=version["sha256"],
        chunk_count=CHUNKS,
    )
    with admin.begin() as connection:
        for table, row in (
            (users, user_row(object_id=alice.oid)),
            (projects, project),
            (project_members, project_member_row(alice.oid, project["id"])),
            (search_indexes, index),
            (physical_indexes, physical),
            (project_indexes, project_index_row(project["id"], index["id"], is_default=True)),
            (documents, document),
            (document_versions, version),
            (chunk_sets, chunk_set),
            (
                served_chunk_sets,
                served_chunk_set_row(
                    document["id"], physical["id"], chunk_set["id"], version["id"]
                ),
            ),
        ):
            connection.execute(insert(table), row)
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
    return {
        "project": project["id"],
        "index": index["id"],
        "physical": physical,
        "document": document["id"],
        "version": version["id"],
        "chunk_set": chunk_set["id"],
    }


def _row_version(admin: Engine, document_id: UUID) -> str:
    with admin.connect() as connection:
        value = connection.execute(
            select(documents.c.row_version).where(documents.c.id == document_id)
        ).scalar_one()
    return encode_row_version(bytes(value))


async def _chunks_of(fakes: ChatFakes, physical_name: str, document_id: UUID) -> list[str]:
    client = fakes.search(physical_name)
    results = await client.search(
        search_text="*", filter=f"search.in(documentId, '{document_id}', ',')", top=50
    )
    return [str(row[FIELDS.key]) async for row in results]


async def test_t201_a_deleted_document_leaves_citations_at_once_and_search_after_clean_up(  # noqa: PLR0917 - fixtures
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    carol: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    chat_fakes: ChatFakes,
    services: DocumentServices,
) -> None:
    world = await asyncio.to_thread(_served_document, admin, alice)
    physical_name = str(world["physical"]["name"])
    rows = [
        search_result(
            document_id=world["document"],
            chunk_set_id=world["chunk_set"],
            version_id=world["version"],
            ordinal=ordinal,
        )
        for ordinal in range(CHUNKS)
    ]
    other = search_result(document_id=uuid4(), chunk_set_id=uuid4(), version_id=uuid4())
    await chat_fakes.search(physical_name).upload_documents([*rows, other])
    session_id, client_turn_id = uuid4(), uuid4()
    turn = turn_document(
        user_id=alice.oid,
        session_id=session_id,
        client_turn_id=client_turn_id,
        project_id=world["project"],
        citations=[
            stored_citation(
                document_id=world["document"],
                document_version_id=world["version"],
                chunk_set_id=world["chunk_set"],
                search_index_id=world["index"],
                physical_index_id=world["physical"]["id"],
            )
        ],
    )
    await cosmos_database.get_container_client(CONVERSATIONS.name).upsert_item(turn.to_item())
    citation = f"/v1/sessions/{session_id}/turns/{turn.id}/citations/1"

    before = await api.client.get(citation, headers=alice.authorization())
    deleted = await api.client.delete(
        f"/v1/admin/indexes/{world['index']}/documents/{world['document']}",
        headers=carol.authorization()
        | {"If-Match": f'"{await asyncio.to_thread(_row_version, admin, world["document"])}"'},
    )
    after = await api.client.get(citation, headers=alice.authorization())
    still_indexed = await _chunks_of(chat_fakes, physical_name, world["document"])

    def claim(connection: Connection) -> list[Any]:
        claimed, _ = claim_ingestion_items.claim(connection, 8)
        return list(claimed)

    claimed = await services.sql.run(claim, name="claim_items", retry="none")
    (item,) = [entry for entry in claimed if entry.run_id == UUID(deleted.json()["runId"])]
    purged = await purge_document_artifacts.run(
        {
            "itemId": str(item.item_id),
            "claimNumber": item.claim_number,
            "runId": str(item.run_id),
            "settings": SETTINGS.model_dump(mode="json"),
            "documentId": str(world["document"]),
        },
        lambda: _provide(services),
    )

    assert before.status_code == 200, before.text
    assert deleted.status_code == 202, deleted.text
    assert (after.status_code, after.json()["code"]) == (410, "document_changed")
    # The served rows went with the delete, so readers drop the chunks; the index still holds
    # them until the clean-up deletes them by key.
    assert sorted(still_indexed) == sorted(str(row[FIELDS.key]) for row in rows)
    assert purged["ok"] is True, purged
    assert purged["deletedChunks"] == CHUNKS
    assert await _chunks_of(chat_fakes, physical_name, world["document"]) == []
    with admin.connect() as connection:
        operation = connection.execute(
            select(ingestion_run_items.c.operation).where(ingestion_run_items.c.id == item.item_id)
        ).scalar_one()
    assert operation == "cleanup_document"
    assert other[FIELDS.key] in chat_fakes.search.indexes[physical_name]


async def _provide(services: DocumentServices) -> DocumentServices:
    return services
