"""The administrator ingestion-run operations (`11-api.md` sections 1.8, 3.12 and 4.7).

`adminListIngestionRuns` and `adminGetIngestionRun`, both `admin` and read-only. `indexId` on the
list is a filter, never an existence check: an unknown index gives an empty page, so the list has
no scoped parameter. A run's items page with their own parameters (`itemsLimit`, `itemsCursor`,
`itemState`) in queue order, and an `itemsCursor` is valid only for the run it was issued for.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from fastapi import Path as PathParam
from pydantic import Field

from docchat_api.http.access import access
from docchat_api.http.models import QueryModel, ResponseModel
from docchat_api.http.paging import CursorModel, CursorText, decode_cursor
from docchat_api.http.routing import DocChatRoute
from docchat_api.http.state import ApiProcess, current_process
from docchat_api.routes.admin_common import PageQuery, keyset_page
from docchat_api.routes.admin_models import (
    ItemStateFilter,
    RunItem,
    RunKindFilter,
    RunStateFilter,
    RunSummary,
    RunSummaryPage,
    run_item,
    run_summary,
)
from docchat_api.routes.ingestion_problems import RunNotFound
from docchat_core.access import MAX_PAGE_LIMIT
from docchat_core.ingestion.models import ItemState, RunKind, RunState
from docchat_core.models import CanonicalUuid, UtcDateTime
from docchat_core.sql.repositories import runs
from docchat_core.sql.repositories.runs import RunFilters, RunPosition

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "admin-runs"
DEFAULT_ITEMS_LIMIT: Final = 100
ITEMS_CURSOR_PARAMETER: Final = "query.itemsCursor"

Process = Annotated[ApiProcess, Depends(current_process)]


# --- adminListIngestionRuns -----------------------------------------------------------------


class ListRunsQuery(PageQuery):
    index_id: CanonicalUuid | None = None
    state: RunStateFilter | None = None
    kind: RunKindFilter | None = None


class RunsCursor(CursorModel):
    """`{"v":1,"c":"<createdAt>","r":"<runId>","x":"<indexId>" or null,"s":"<state>" or null,
    "k":"<kind>" or null}`; `x`, `s` and `k` are the filters it was issued for."""

    c: UtcDateTime
    r: CanonicalUuid
    x: CanonicalUuid | None
    s: str | None
    k: str | None


@router.get(
    "/admin/ingestion-runs",
    operation_id="adminListIngestionRuns",
    tags=[TAG],
    response_model=RunSummaryPage,
)
@access("admin")
async def admin_list_ingestion_runs(
    query: Annotated[ListRunsQuery, Query()], process: Process
) -> RunSummaryPage:
    state, kind = query.state, query.kind
    after = None
    if query.cursor is not None:
        cursor = decode_cursor(
            query.cursor, RunsCursor, filters={"x": query.index_id, "s": state, "k": kind}
        )
        after = RunPosition(created_at=cursor.c, run_id=cursor.r)
    rows = await runs.read_runs(
        process.sql,
        filters=RunFilters(
            index_id=query.index_id,
            state=None if state is None else RunState(state),
            kind=None if kind is None else RunKind(kind),
        ),
        after=after,
        limit_plus_one=query.limit + 1,
    )
    page, cursor_text = keyset_page(
        rows,
        query.limit,
        lambda row: RunsCursor(c=row.created_at, r=row.id, x=query.index_id, s=state, k=kind),
    )
    return RunSummaryPage(items=[run_summary(row) for row in page], next_cursor=cursor_text)


# --- adminGetIngestionRun -------------------------------------------------------------------


class GetRunQuery(QueryModel):
    items_limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_ITEMS_LIMIT
    items_cursor: CursorText | None = None
    item_state: ItemStateFilter | None = None


class RunItemsCursor(CursorModel):
    """`{"v":1,"r":"<runId>","a":<queue sequence of the last item>,"s":"<itemState>" or null}`;
    `r` and `s` are the run and the filter it was issued for."""

    r: CanonicalUuid
    a: Annotated[int, Field(ge=1)]
    s: str | None


class IngestionRunDetail(ResponseModel):
    run: RunSummary
    items: Annotated[list[RunItem], Field(max_length=MAX_PAGE_LIMIT)]
    next_items_cursor: CursorText | None


@router.get(
    "/admin/ingestion-runs/{runId}",
    operation_id="adminGetIngestionRun",
    tags=[TAG],
    response_model=IngestionRunDetail,
)
@access("admin", scoped_params=["runId"], error_codes=["run_not_found"])
async def admin_get_ingestion_run(
    run_id: Annotated[CanonicalUuid, PathParam(alias="runId")],
    query: Annotated[GetRunQuery, Query()],
    process: Process,
) -> IngestionRunDetail:
    item_state = query.item_state
    after = None
    if query.items_cursor is not None:
        cursor = decode_cursor(
            query.items_cursor,
            RunItemsCursor,
            filters={"r": run_id, "s": item_state},
            parameter=ITEMS_CURSOR_PARAMETER,
        )
        after = cursor.a
    detail = await runs.read_run(
        process.sql,
        run_id=run_id,
        item_state=None if item_state is None else ItemState(item_state),
        after_queue_seq=after,
        limit_plus_one=query.items_limit + 1,
    )
    if detail is None:
        raise RunNotFound()
    items, cursor_text = keyset_page(
        detail.items,
        query.items_limit,
        lambda item: RunItemsCursor(r=run_id, a=item.queue_seq, s=item_state),
    )
    return IngestionRunDetail(
        run=run_summary(detail.run),
        items=[run_item(item) for item in items],
        next_items_cursor=cursor_text,
    )
