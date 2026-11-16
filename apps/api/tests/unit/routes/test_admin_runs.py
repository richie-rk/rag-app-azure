"""Unit tests of the administrator run routes (`11-api.md` section 3.12) on the in-process app over
the fake data-access layer, with the repository's reads replaced by recorders; the statements are
`packages/core/tests/unit/sql/repositories/test_runs.py`'s and the reads against SQL Server
`apps/api/tests/integration/routes/test_admin_runs.py`'s."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from docchat_api.http.paging import encode_cursor
from docchat_api.routes.admin_runs import RunItemsCursor, RunsCursor
from docchat_core.ingestion.models import (
    ItemOperation,
    ItemState,
    RunKind,
    RunPriority,
    RunState,
)
from docchat_core.sql.repositories import runs
from docchat_core.sql.repositories.runs import (
    RunCounts,
    RunDetailRow,
    RunItemRow,
    RunPosition,
    RunRow,
)
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

AT = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC)
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
RUN = UUID("9c4d5e6f-7a8b-4c9d-8e1f-3a4b5c6d7e63")
RUNS = "/v1/admin/ingestion-runs"


@dataclass
class Fake:
    result: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, engine: object, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


def run_row(run_id: UUID = RUN, created_at: datetime = AT) -> RunRow:
    return RunRow(
        id=run_id,
        kind=RunKind.UPLOAD,
        state=RunState.COMPLETED,
        index_id=INDEX,
        index_display_name="Policy library",
        priority=RunPriority.INTERACTIVE,
        requested_by=None,
        requested_by_name=None,
        created_at=created_at,
        started_at=created_at,
        completed_at=created_at,
        counts=RunCounts(total=1, succeeded=1),
    )


def item_row(queue_seq: int) -> RunItemRow:
    return RunItemRow(
        id=uuid4(),
        queue_seq=queue_seq,
        operation=ItemOperation.CLEANUP_DOCUMENT,
        document_id=uuid4(),
        document_name="Notes.pdf",
        version_number=None,
        target=None,
        state=ItemState.SUCCEEDED,
        stage=None,
        attempts=1,
        outcome_code=None,
        outcome_message=None,
        warnings=(),
        embedding_tokens=None,
        summary_input_tokens=None,
        summary_output_tokens=None,
        docintel_pages=None,
        created_at=AT,
        started_at=AT,
        completed_at=AT,
    )


async def test_run_pages_follow_the_cursor_newest_first(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [run_row(uuid4(), AT - timedelta(minutes=minutes)) for minutes in range(3)]
    fake = Fake(rows)
    monkeypatch.setattr(runs, "read_runs", fake)
    params: dict[str, str | int] = {
        "limit": 2,
        "indexId": str(INDEX).upper(),
        "state": "completed",
        "kind": "upload",
    }

    first = (await started.client.get(RUNS, params=params, headers=carol.authorization())).json()

    assert [item["runId"] for item in first["items"]] == [str(row.id) for row in rows[:2]]
    assert first["nextCursor"] == encode_cursor(
        RunsCursor(c=rows[1].created_at, r=rows[1].id, x=INDEX, s="completed", k="upload")
    )
    filters = fake.calls[0]["filters"]
    assert (filters.index_id, filters.state, filters.kind) == (
        INDEX,
        RunState.COMPLETED,
        RunKind.UPLOAD,
    )

    fake.result = rows[2:]
    second = await started.client.get(
        RUNS, params=params | {"cursor": first["nextCursor"]}, headers=carol.authorization()
    )

    assert second.json()["nextCursor"] is None
    assert fake.calls[1]["after"] == RunPosition(created_at=rows[1].created_at, run_id=rows[1].id)


async def test_an_unknown_index_filter_gives_an_empty_page(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "read_runs", Fake([]))

    response = await started.client.get(
        RUNS, params={"indexId": str(uuid4())}, headers=carol.authorization()
    )

    assert response.json() == {"items": [], "nextCursor": None}


async def test_a_run_list_cursor_is_bound_to_its_filters(
    started: ApiUnderTest, carol: SharedPrincipal
) -> None:
    cursor = encode_cursor(RunsCursor(c=AT, r=RUN, x=None, s=None, k="upload"))

    response = await started.client.get(
        RUNS, params={"cursor": cursor}, headers=carol.authorization()
    )

    assert response.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]


async def test_run_parameters_are_validated(started: ApiUnderTest, carol: SharedPrincipal) -> None:
    response = await started.client.get(
        RUNS,
        params={"kind": "nightly", "state": "done", "indexId": "x"},
        headers=carol.authorization(),
    )

    assert response.json()["errors"] == [
        {"path": "query.indexId", "reason": "invalid_uuid"},
        {"path": "query.state", "reason": "not_allowed"},
        {"path": "query.kind", "reason": "not_allowed"},
    ]


async def test_a_run_pages_its_items_in_queue_order(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Fake(RunDetailRow(run=run_row(), items=(item_row(3), item_row(5), item_row(9))))
    monkeypatch.setattr(runs, "read_run", fake)

    first = (
        await started.client.get(
            f"{RUNS}/{RUN}",
            params={"itemsLimit": 2, "itemState": "succeeded"},
            headers=carol.authorization(),
        )
    ).json()

    assert first["run"]["runId"] == str(RUN)
    assert first["run"]["priority"] == "interactive"
    assert len(first["items"]) == 2
    assert first["nextItemsCursor"] == encode_cursor(RunItemsCursor(r=RUN, a=5, s="succeeded"))
    assert fake.calls[0] == {
        "run_id": RUN,
        "item_state": ItemState.SUCCEEDED,
        "after_queue_seq": None,
        "limit_plus_one": 3,
    }

    fake.result = RunDetailRow(run=run_row(), items=(item_row(9),))
    second = (
        await started.client.get(
            f"{RUNS}/{RUN}",
            params={
                "itemsLimit": 2,
                "itemState": "succeeded",
                "itemsCursor": first["nextItemsCursor"],
            },
            headers=carol.authorization(),
        )
    ).json()

    assert second["nextItemsCursor"] is None
    assert fake.calls[1]["after_queue_seq"] == 5


async def test_an_items_cursor_of_another_run_is_invalid(
    started: ApiUnderTest, carol: SharedPrincipal
) -> None:
    cursor = encode_cursor(RunItemsCursor(r=uuid4(), a=5, s=None))

    response = await started.client.get(
        f"{RUNS}/{RUN}", params={"itemsCursor": cursor}, headers=carol.authorization()
    )

    assert response.json()["errors"] == [{"path": "query.itemsCursor", "reason": "invalid_cursor"}]


async def test_an_unknown_run_is_run_not_found(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "read_run", Fake(None))

    response = await started.client.get(f"{RUNS}/{uuid4()}", headers=carol.authorization())

    assert (response.status_code, response.json()["code"]) == (404, "run_not_found")


async def test_a_user_gets_admin_required(started: ApiUnderTest, alice: SharedPrincipal) -> None:
    response = await started.client.get(f"{RUNS}/{uuid4()}", headers=alice.authorization())

    assert (response.status_code, response.json()["code"]) == (403, "admin_required")
