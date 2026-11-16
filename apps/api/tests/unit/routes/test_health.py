from __future__ import annotations

from typing import Any, Protocol

from opentelemetry.sdk.trace import ReadableSpan

from docchat_testing.fixtures.api import ApiUnderTest


class Captured(Protocol):
    collector: Any

    def server_spans(self) -> list[ReadableSpan]: ...


class FakeSql(Protocol):
    calls: list[Any]


async def test_t060_get_health_answers_200_with_an_empty_body(
    started: ApiUnderTest, fake_sql: FakeSql, captured: Captured
) -> None:
    calls = len(fake_sql.calls)
    records = len(captured.collector.records)

    response = await started.client.get("/healthz")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert len(fake_sql.calls) == calls
    assert len(captured.collector.records) == records
    burst: Any = started.ports.burst_limiter
    assert burst.checked == []
    (server,) = captured.server_spans()
    assert "enduser.id" not in (server.attributes or {})


async def test_get_health_runs_no_in_app_check(started: ApiUnderTest) -> None:
    response = await started.client.get("/healthz", headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 200


async def test_get_health_answers_only_get(started: ApiUnderTest) -> None:
    response = await started.client.post("/healthz")

    assert (response.status_code, response.json()["code"]) == (405, "method_not_allowed")
    assert response.headers["allow"] == "GET"
