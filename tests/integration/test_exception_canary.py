"""T-488, the exception-text canary test (`85-observability.md` sections 3.6 and 3.7 item 8;
`82-security.md` SEC-31 and SEC-81, statement 28).

A log record and a span carrying a fake JWT, a fake connection string and an e-mail address
reach the session's in-memory exporters redacted. An exception whose message is
`cnry-question-<8 hex>`, raised in an unmapped API route (answered `500`) and in an ingestion
activity, leaves no exported span event, log record or stack trace holding `cnry-`, while the
failure is still recorded with its type and frames. The session-wide canary audit reads the same
exporters at session end.

The third place the row names, a post-turn background task, arrives with the post-turn work of
`13-conversations-and-memory.md` (stage 4); this suite gains its case then.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import APIRouter
from opentelemetry import trace

from docchat_api.http.access import access
from docchat_api.http.routing import DocChatRoute
from docchat_core.ingestion.models import ItemEnvelope, ItemSettings
from docchat_core.sql.engine import SqlEngine
from docchat_ingestion.activities import validate_item
from docchat_ingestion.activities.documents import DocumentServices
from docchat_testing.builders import canary
from docchat_testing.fixtures.api import ApiUnderTest, ports_under_test, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.telemetry_capture import TelemetryCapture, TelemetryItem
from docchat_testing.tokens import SigningKeys

SETTINGS = ItemSettings(
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


def _secrets() -> tuple[str, str, str]:
    """A JWT-shaped value, a connection-string secret and an e-mail address, built at run time
    so no source line a stack trace quotes holds them."""
    jwt = "eyJ" + secrets.token_hex(8) + ".eyJ" + secrets.token_hex(8) + "." + secrets.token_hex(8)
    connection = "AccountKey=" + secrets.token_hex(12) + ";EndpointSuffix=core.windows.net"
    email = "operator" + "@" + "example.com"
    return jwt, connection, email


def _texts(items: list[TelemetryItem]) -> list[str]:
    return [text for item in items for _, text in item.strings]


def _holding(capture: TelemetryCapture, marker: str) -> list[TelemetryItem]:
    return [item for item in capture.items() if any(marker in text for _, text in item.strings)]


def _exception_events(capture: TelemetryCapture, type_name: str) -> list[dict[str, Any]]:
    return [
        dict(event.attributes or {})
        for span in capture.spans()
        for event in span.events
        if event.name == "exception" and (event.attributes or {}).get("exception.type") == type_name
    ]


# T-488


def test_t488_records_with_secrets_reach_the_exporters_redacted(
    telemetry_capture: TelemetryCapture,
) -> None:
    jwt, connection, email = _secrets()
    marker = f"t488-{secrets.token_hex(4)}"

    with trace.get_tracer("docchat.tests").start_as_current_span(f"docchat.tests.{marker}") as span:
        span.set_attribute("note", f"{jwt} {connection} {email}")
    logging.getLogger("docchat").warning("%s %s %s %s", marker, jwt, connection, email)

    exported = _holding(telemetry_capture, marker)
    texts = _texts(exported)
    assert len(exported) >= 2
    assert [text for text in texts if jwt in text or connection in text or email in text] == []
    assert any("[redacted:jwt]" in text for text in texts)
    assert any("AccountKey=[redacted]" in text for text in texts)
    assert any("[redacted:email]" in text for text in texts)


def _boom_router(message: str) -> APIRouter:
    router = APIRouter(prefix="/v1", route_class=DocChatRoute)

    @router.get("/t488/boom", operation_id="t488Boom")
    @access("user")
    async def boom() -> None:
        raise LookupError(message)

    return router


@pytest.fixture
async def boom_api(
    api_sql_engine: SqlEngine, signing_keys: SigningKeys
) -> AsyncIterator[tuple[ApiUnderTest, str]]:
    message = canary("question")
    try:
        async with running_api(
            ports=ports_under_test(sql_engine=api_sql_engine),
            signing_keys=signing_keys,
            routers=[_boom_router(message)],
        ) as started:
            yield started, message
    finally:
        await api_sql_engine.dispose()


async def test_t488_an_exception_in_an_unmapped_api_route_exports_no_canary(
    boom_api: tuple[ApiUnderTest, str],
    alice: SharedPrincipal,
    telemetry_capture: TelemetryCapture,
) -> None:
    api, message = boom_api

    response = await api.client.get("/v1/t488/boom", headers=alice.authorization())

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert message not in response.text
    # Reading the whole session's telemetry is blocking work: off the loop (84-testing.md 2.8).
    assert await asyncio.to_thread(_holding, telemetry_capture, message) == []
    assert await asyncio.to_thread(_holding, telemetry_capture, "cnry-") == []
    recorded = await asyncio.to_thread(_exception_events, telemetry_capture, "builtins.LookupError")
    assert recorded
    assert all("in boom" in str(event.get("exception.stacktrace")) for event in recorded)


async def test_t488_an_exception_in_an_ingestion_activity_exports_no_canary(
    telemetry_capture: TelemetryCapture,
) -> None:
    message = canary("question")
    envelope = ItemEnvelope(
        item_id=uuid4(), claim_number=1, run_id=uuid4(), traceparent=None, settings=SETTINGS
    )

    async def failing_services() -> DocumentServices:
        raise LookupError(message)

    output = await validate_item.run(
        envelope.model_dump(mode="json", by_alias=True), failing_services
    )

    assert output["ok"] is False
    assert output["errorCode"] == "internal_error"
    # Reading the whole session's telemetry is blocking work: off the loop (84-testing.md 2.8).
    assert await asyncio.to_thread(_holding, telemetry_capture, message) == []
    assert await asyncio.to_thread(_holding, telemetry_capture, "cnry-") == []
    recorded = await asyncio.to_thread(_exception_events, telemetry_capture, "builtins.LookupError")
    assert any(
        "in failing_services" in str(event.get("exception.stacktrace")) for event in recorded
    )
