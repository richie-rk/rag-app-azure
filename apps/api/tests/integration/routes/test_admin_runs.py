"""The administrator ingestion-run operations against SQL Server (`11-api.md` sections 1.8 and
3.12; `14-ingestion.md` section 17; T-054's run lists).

The app runs as the `docchat_api` role with the permissive limiters; runs and items are
arranged as `sa` with the creation times each test needs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, insert

from docchat_core.sql.tables.ingestion import (
    documents,
    ingestion_run_items,
    ingestion_runs,
    search_indexes,
)
from docchat_testing.builders.ingestion import (
    document_row,
    ingestion_run_item_row,
    ingestion_run_row,
    search_index_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

RUNS = "/v1/admin/ingestion-runs"


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def new_index(engine: Engine) -> UUID:
    index = search_index_row(status="ready")
    put(engine, (search_indexes, index))
    return UUID(str(index["id"]))


def new_run(engine: Engine, index_id: UUID, minutes_ago: int, **overrides: Any) -> UUID:
    """A run of the index created `minutes_ago` minutes before now."""
    run = ingestion_run_row(
        index_id, created_at=sql_now() - timedelta(minutes=minutes_ago), **overrides
    )
    put(engine, (ingestion_runs, run))
    return UUID(str(run["id"]))


def cleanup_items(engine: Engine, index_id: UUID, run_id: UUID, states: list[str]) -> list[UUID]:
    """One `cleanup_document` item per state, each for a document of its own, in queue order."""
    created: list[UUID] = []
    for state in states:
        document = document_row(index_id)
        failed = state == "failed"
        item = ingestion_run_item_row(
            run_id,
            index_id,
            document_id=document["id"],
            state=state,
            outcome_code="document_deleted" if failed else None,
            outcome_message="Not processed." if failed else None,
        )
        put(engine, (documents, document), (ingestion_run_items, item))
        created.append(UUID(str(item["id"])))
    return created


async def list_runs(api: ApiUnderTest, caller: SharedPrincipal, **params: Any) -> list[str]:
    response = await api.client.get(
        RUNS, params={"limit": 100, **params}, headers=caller.authorization()
    )
    assert response.status_code == 200, response.text
    return [item["runId"] for item in response.json()["items"]]


# --- adminListIngestionRuns -----------------------------------------------------------------


async def test_t054_run_pages_stay_complete_while_runs_are_added(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = new_index(admin)
    # Newest first: created 10, 20, 30, 40 and 50 minutes ago.
    expected = [str(new_run(admin, index_id, minutes)) for minutes in (10, 20, 30, 40, 50)]
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get(RUNS, params=params, headers=carol.authorization())
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["runId"] for item in body["items"])
        pages += 1
        if pages == 1:
            # A newer run sorts before the first page's cursor, an older one after it.
            new_run(admin, index_id, 0)
            expected.append(str(new_run(admin, index_id, 60)))
        cursor = body["nextCursor"]
        if cursor is None:
            break

    assert seen == expected
    assert pages == 3


async def test_the_run_list_filters_by_index_state_and_kind(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    x, y = new_index(admin), new_index(admin)
    queued_upload = new_run(admin, x, 1)
    completed_upload = new_run(admin, x, 2, state="completed")
    delete = new_run(admin, x, 3, kind="document_delete", priority=1)
    other = new_run(admin, y, 4)

    assert await list_runs(api, carol, indexId=str(x)) == [
        str(queued_upload),
        str(completed_upload),
        str(delete),
    ]
    assert await list_runs(api, carol, state="completed") == [str(completed_upload)]
    assert await list_runs(api, carol, kind="upload", state="queued") == [
        str(queued_upload),
        str(other),
    ]
    assert await list_runs(api, carol, indexId=str(uuid4())) == []


async def test_a_run_summary_counts_its_items_by_state(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = new_index(admin)
    run_id = new_run(admin, index_id, 1, state="running")
    cleanup_items(admin, index_id, run_id, ["succeeded", "failed", "queued"])

    response = await api.client.get(
        RUNS, params={"indexId": str(index_id)}, headers=carol.authorization()
    )

    (summary,) = response.json()["items"]
    assert summary["counts"]["total"] == 3
    assert (summary["counts"]["succeeded"], summary["counts"]["failed"]) == (1, 1)
    assert summary["index"]["indexId"] == str(index_id)


# --- adminGetIngestionRun -------------------------------------------------------------------


async def test_a_runs_items_page_in_queue_order(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = new_index(admin)
    run_id = new_run(admin, index_id, 1, state="running")
    items = cleanup_items(admin, index_id, run_id, ["succeeded", "failed", "queued"])
    path = f"{RUNS}/{run_id}"

    first = await api.client.get(path, params={"itemsLimit": 2}, headers=carol.authorization())
    second = await api.client.get(
        path,
        params={"itemsLimit": 2, "itemsCursor": first.json()["nextItemsCursor"]},
        headers=carol.authorization(),
    )
    failed = await api.client.get(
        path, params={"itemState": "failed"}, headers=carol.authorization()
    )

    assert first.status_code == 200
    assert first.json()["run"]["runId"] == str(run_id)
    assert [item["itemId"] for item in first.json()["items"]] == [str(item) for item in items[:2]]
    assert [item["itemId"] for item in second.json()["items"]] == [str(items[2])]
    assert second.json()["nextItemsCursor"] is None
    (only,) = failed.json()["items"]
    assert (only["itemId"], only["outcomeCode"]) == (str(items[1]), "document_deleted")


async def test_an_items_cursor_belongs_to_its_run(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = new_index(admin)
    one, other = new_run(admin, index_id, 1), new_run(admin, index_id, 2)
    cleanup_items(admin, index_id, one, ["queued", "queued"])
    first = await api.client.get(
        f"{RUNS}/{one}", params={"itemsLimit": 1}, headers=carol.authorization()
    )

    response = await api.client.get(
        f"{RUNS}/{other}",
        params={"itemsCursor": first.json()["nextItemsCursor"]},
        headers=carol.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "query.itemsCursor", "reason": "invalid_cursor"}]


async def test_an_unknown_run_is_not_found(api: ApiUnderTest, carol: SharedPrincipal) -> None:
    response = await api.client.get(f"{RUNS}/{uuid4()}", headers=carol.authorization())

    assert (response.status_code, response.json()["code"]) == (404, "run_not_found")
