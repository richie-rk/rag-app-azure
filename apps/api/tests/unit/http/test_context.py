from __future__ import annotations

import re

import httpx
from opentelemetry import trace
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from docchat_api.http.context import (
    OPERATION_KEY,
    SERVER_SPAN_KEY,
    CorrelationMiddleware,
    correlation_id,
    operation_of,
    server_span_of,
)
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


async def _report(request: Request) -> Response:
    return JSONResponse({"correlationId": correlation_id()})


def _app() -> CorrelationMiddleware:
    return CorrelationMiddleware(Starlette(routes=[Route("/", _report)]))


async def test_without_a_trace_each_request_gets_its_own_trace_id() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = (await client.get("/")).json()["correlationId"]
        second = (await client.get("/")).json()["correlationId"]

    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != second
    assert first != "0" * 32


async def test_an_existing_trace_is_kept(captured: object) -> None:
    transport = httpx.ASGITransport(app=_app())
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("server") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            reported = (await client.get("/")).json()["correlationId"]

    assert reported == expected


async def test_t493_a_problem_names_the_trace_of_the_traceparent(started: ApiUnderTest) -> None:
    response = await started.client.get(
        "/v1/me", headers={"traceparent": f"00-{TRACE_ID}-00f067aa0ba902b7-01"}
    )

    assert response.status_code == 401
    assert response.json()["correlationId"] == TRACE_ID


async def test_a_request_without_a_traceparent_gets_a_new_trace(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    first = await started.client.get("/v1/me")
    second = await started.client.get("/v1/me")

    assert first.json()["correlationId"] != second.json()["correlationId"]


def test_the_scope_accessors() -> None:
    span = trace.get_tracer("test").start_span("x")
    scope = {OPERATION_KEY: "getCurrentUser", SERVER_SPAN_KEY: span}

    assert operation_of(scope) == "getCurrentUser"
    assert server_span_of(scope) is span
    assert operation_of({}) is None
    assert server_span_of({}) is trace.get_current_span()
