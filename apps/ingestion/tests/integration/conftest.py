"""An ingestion worker process over the `compose.yaml` services, for the integration tests.

`IngestionProcess` as `runtime.build_process` builds it, with the data-access layer as the
`docchat_ingestion` role's login of the session's SQL Server database, the ops-config store over
it, and the session's Azurite containers. The search service isn't a compose service, so the
search port refuses every call; the tests that use this process don't reach it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from docchat_core.models import Clock, SystemClock
from docchat_core.opsconfig import INGESTION_AREAS, OpsConfigStore
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_ingestion.activities.runtime import (
    ContainerPort,
    IngestionProcess,
    QueuePeekPort,
    SearchIndexPort,
)
from docchat_ingestion.isolation.pdf_schema import RENDITION_RULES_VERSION
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import POISON_QUEUE, StorageClients

WORKLOAD_PRINCIPAL_ID = UUID("5f1c0de0-0000-4000-8000-0000000000b2")

type ProcessFactory = Callable[..., IngestionProcess]


class _NoSearchService:
    """The search service isn't a compose service."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the test reached the search service ({name})")


@pytest.fixture
async def ingestion_sql(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    """The data-access layer as the `docchat_ingestion` role."""
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
def make_process(ingestion_sql: SqlEngine, storage: StorageClients) -> ProcessFactory:
    """`make_process(scanning_enabled=..., clock=...)`: the worker's resources over compose."""

    def build(*, scanning_enabled: bool = False, clock: Clock | None = None) -> IngestionProcess:
        return IngestionProcess(
            sql=ingestion_sql,
            ops=OpsConfigStore(ingestion_sql, INGESTION_AREAS, report_invalid=False),
            documents=cast(ContainerPort, storage.container("documents")),
            renditions=cast(ContainerPort, storage.container("renditions")),
            work=cast(ContainerPort, storage.container("ingestion-work")),
            poison_queue=cast(QueuePeekPort, storage.queue(POISON_QUEUE)),
            search_indexes=cast(SearchIndexPort, _NoSearchService()),
            scanning_enabled=scanning_enabled,
            workload_principal_id=WORKLOAD_PRINCIPAL_ID,
            rendition_rules_version=RENDITION_RULES_VERSION,
            clock=SystemClock() if clock is None else clock,
        )

    return build
