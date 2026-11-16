"""The cross-project and cross-user isolation suite (T-002, T-003; `84-testing.md` section 4).

Every entry of `docchat_testing.isolation.DATA_PATHS` runs through the in-process API against the
session's SQL Server database, the Cosmos DB emulator and Azurite, with the permissive limiters.
The app's Cosmos DB containers are the session database's (`EmulatorContainers`), each wrapped in
`CosmosAudit`, and its `renditions` container is Azurite's. The OpenAPI document an entry reads
is exported at collection time, outside any event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from typing import Any, cast

import pytest
from azure.cosmos.aio import DatabaseProxy
from sqlalchemy import Engine

from docchat_api.app import create_app
from docchat_api.chat.citations import renditions_container
from docchat_core.sql.engine import SqlEngine
from docchat_testing.data_audit import CosmosAudit
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
from docchat_testing.isolation import (
    DATA_PATHS,
    Entry,
    IsolationContext,
    completeness_problems,
    run_entry,
)
from docchat_testing.tokens import SigningKeys

DOCUMENT = create_app().openapi()


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
def audit() -> CosmosAudit:
    return CosmosAudit(operations=operation_map())


@pytest.fixture
def emulator(audit: CosmosAudit) -> EmulatorContainers:
    return EmulatorContainers(audit=audit)


@pytest.fixture
def api_app(
    api_sql_engine: SqlEngine,
    sql_database: SqlTestDatabase,
    signing_keys: SigningKeys,
    emulator: EmulatorContainers,
    chat_fakes: ChatFakes,
) -> BuiltApp:
    # The chat entries run turns over the chat doubles, with the chat path's limits raised.
    raise_chat_limits(sql_database)
    ports = replace(
        ports_under_test(sql_engine=api_sql_engine, chat=chat_fakes.ports()),
        container_wrapper=emulator,
    )
    return build_app(ports=ports, signing_keys=signing_keys)


@pytest.fixture
async def api(  # noqa: PLR0917 - fixtures
    api_app: BuiltApp,
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    emulator: EmulatorContainers,
    storage: StorageClients,
    chat_fakes: ChatFakes,
) -> AsyncIterator[ApiUnderTest]:
    emulator.database = cosmos_database
    try:
        async with serving(api_app) as started:
            started.operations = operation_map()
            started.chat = chat_fakes
            renditions = cast("Any", storage.container("renditions"))
            started.app.dependency_overrides[renditions_container] = lambda: renditions
            yield started
    finally:
        await api_sql_engine.dispose()


@pytest.mark.parametrize("entry", DATA_PATHS, ids=[entry.name for entry in DATA_PATHS])
async def test_t002_isolation_entry(
    *,
    entry: Entry,
    api: ApiUnderTest,
    admin: Engine,
    principals: dict[PrincipalName, SharedPrincipal],
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    audit: CosmosAudit,
) -> None:
    context = IsolationContext(
        client=api.client,
        admin=admin,
        principals=principals,
        document=DOCUMENT,
        cosmos=cosmos_database,
        renditions=storage.container("renditions"),
        audit=audit,
        chat=api.chat,
    )

    await run_entry(entry, context)
    audit.check()


def test_t003_the_registry_is_complete() -> None:
    assert completeness_problems(DATA_PATHS) == []
