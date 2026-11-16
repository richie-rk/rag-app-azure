"""What the activities share (`14-ingestion.md` sections 4.4, 4.8 and 4.9): the camelCase wire
models, the item activity's span parented to its run's trace, the fenced lease update every item
activity starts with, span links to the runs a dispatcher activity touches, and the closed-set
literals its statements keep in their text.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Final, Self
from uuid import UUID

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, Span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import Connection, func, literal_column, update
from sqlalchemy.sql.elements import ColumnClause

from docchat_core.errors import ITEM_CODES
from docchat_core.ingestion.models import LEASE_LOST, ActivityResult, ItemEnvelope, ItemStage
from docchat_core.sql.tables.ingestion import ingestion_run_items
from docchat_core.sql.tables.schema import SYSUTCDATETIME, nvarchar_literal
from docchat_core.telemetry import set_attributes, start_span
from docchat_ingestion.boundary import INTERNAL_ERROR, guard, run_activity
from docchat_ingestion.methods.retry import retry_after_seconds as headers_retry_after

ACTIVITY_SPAN: Final = "docchat.ingestion.activity"
LEASE_MARGIN_SECONDS: Final = 600
"""Each item activity extends its lease to its deadline plus 10 minutes (section 4.8)."""

_PROPAGATOR: Final = TraceContextTextMapPropagator()


class WireModel(BaseModel):
    """An activity's input or output: camelCase on the wire, unknown fields refused."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    @classmethod
    def from_wire(cls, payload: object) -> Self:
        """The model of an input that crossed Durable history, read by its camelCase names."""
        return cls.model_validate(payload, by_alias=True, by_name=False)


def wire(result: BaseModel) -> dict[str, Any]:
    """An output as the JSON the activity returns."""
    return result.model_dump(mode="json")


def literal(value: str) -> ColumnClause[str]:
    """A closed-set value kept in the statement text, so a predicate matches its filtered
    index."""
    return nvarchar_literal(value)


def literals(values: Iterable[str]) -> list[ColumnClause[str]]:
    return [nvarchar_literal(value) for value in values]


def dateadd(part: str, amount: Any, start: Any = SYSUTCDATETIME) -> Any:
    """`DATEADD(<part>, <amount>, <start>)`; the part is a keyword, never a parameter."""
    if part not in {"second", "minute", "hour", "day"}:
        raise ValueError("not a date part this app uses")
    return func.dateadd(literal_column(part), amount, start)


def catalogue_message(code: str) -> str:
    """An item code's safe message, stored in `outcome_message`."""
    return ITEM_CODES[code].message


def fence_lease(
    connection: Connection, item_id: UUID, claim: int, stage: ItemStage, deadline_seconds: int
) -> bool:
    """The fenced lease update of section 4.8 rule 2; `False` means the claim was lost."""
    changed = connection.execute(
        update(ingestion_run_items)
        .where(
            ingestion_run_items.c.id == item_id,
            ingestion_run_items.c.claim_count == claim,
            ingestion_run_items.c.state == literal("running"),
        )
        .values(
            stage=stage.value,
            lease_expires_at=dateadd("second", deadline_seconds + LEASE_MARGIN_SECONDS),
        )
    ).rowcount
    return bool(changed == 1)


def remote_context(traceparent: str | None, tracestate: str | None) -> otel_context.Context | None:
    """The run's stored trace context as a parent, or `None` for a run without one."""
    if traceparent is None:
        return None
    carrier = {"traceparent": traceparent}
    if tracestate:
        carrier["tracestate"] = tracestate
    extracted = _PROPAGATOR.extract(carrier)
    if not trace.get_current_span(extracted).get_span_context().is_valid:
        return None
    return extracted


def run_links(traceparents: Iterable[str | None]) -> list[Link]:
    """One span link per distinct stored `traceparent` (section 4.9 item 4)."""
    links: list[Link] = []
    seen: set[str] = set()
    for traceparent in traceparents:
        if traceparent is None or traceparent in seen:
            continue
        seen.add(traceparent)
        parent = remote_context(traceparent, None)
        if parent is not None:
            links.append(Link(trace.get_current_span(parent).get_span_context()))
    return links


@contextmanager
def item_span(activity_name: str, envelope: ItemEnvelope, **ids: UUID | None) -> Iterator[Span]:
    """The span `docchat.ingestion.activity` of one item activity, a child of the run's trace
    context, or the root of a new trace for a run without one (section 4.9 items 3 and 5)."""
    parent = remote_context(envelope.traceparent, envelope.tracestate)
    token = None if parent is None else otel_context.attach(parent)
    try:
        attributes: dict[str, object] = {
            "docchat.activity_name": activity_name,
            "docchat.run_id": str(envelope.run_id),
            "docchat.item_id": str(envelope.item_id),
            "docchat.ingestion.claim": envelope.claim_number,
        }
        attributes |= {key: str(value) for key, value in ids.items() if value is not None}
        with start_span(ACTIVITY_SPAN, **attributes) as span:
            yield span
    finally:
        if token is not None:
            otel_context.detach(token)


def http_status(error: BaseException) -> int | None:
    """The status code of an Azure SDK error's response, when it had one."""
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


THROTTLED_STATUSES: Final = frozenset({429, 503})
FIRST_SERVER_ERROR: Final = 500


def service_unavailable(error: BaseException) -> bool:
    """A refusal the service will lift: throttled (`429`, `503`) or any other `5xx`."""
    status = http_status(error)
    return status is not None and (status in THROTTLED_STATUSES or status >= FIRST_SERVER_ERROR)


def retry_after_seconds(error: BaseException) -> int | None:
    """The wait the failed call's last response asked for (section 4.7 item 5), read by the one
    rule the methods use too (`docchat_ingestion.methods.retry`); `None` without a response or
    a usable header."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    return headers_retry_after(headers)


def record_outcome(span: Span, result: ActivityResult) -> None:
    """The activity's outcome and code on its span."""
    attributes: dict[str, object] = {"docchat.outcome": "ok" if result.ok else "failed"}
    if result.error_code is not None and result.error_code != LEASE_LOST:
        attributes["docchat.error_code"] = result.error_code
    set_attributes(span, **attributes)


def parse_envelope(payload: object) -> ItemEnvelope:
    """An item activity's input, read by its camelCase names."""
    return ItemEnvelope.model_validate(payload, by_alias=True, by_name=False)


def _failure(code: str) -> ActivityResult:
    return ActivityResult.failure(code)


async def run_item_activity(
    name: str,
    deadline_seconds: int,
    payload: object,
    body: Callable[[ItemEnvelope, Span], Awaitable[ActivityResult]],
) -> dict[str, Any]:
    """An item activity inside its span, its failure boundary and its deadline: the envelope is
    read inside the guard, then the body runs in `docchat.ingestion.activity`, parented to the
    run's trace, and the outcome lands on the span."""
    envelope: ItemEnvelope | None = None
    async with guard(name):
        envelope = parse_envelope(payload)
    if envelope is None:
        return wire(_failure(INTERNAL_ERROR))
    item = envelope
    with item_span(name, item) as span:

        async def run_body() -> ActivityResult:
            return await body(item, span)

        result = await run_activity(name, deadline_seconds, run_body, _failure)
        record_outcome(span, result)
        return wire(result)
