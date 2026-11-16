from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from docchat_testing import logquery
from docchat_testing.logquery import (
    CHAT_TELEMETRY_ITEMS,
    LogQueryError,
    chat_telemetry_query,
    check_chat_telemetry,
    found_items,
    parse_since,
    query_logs,
    rows_of,
    timespan_of,
)

WORKSPACE = "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
URL = f"https://api.loganalytics.io/v1/workspaces/{WORKSPACE}/query"
SINCE = datetime(2026, 10, 30, 9, 15, tzinfo=UTC)


def answer(rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "PrimaryResult",
                "columns": [{"name": "item", "type": "string"}, {"name": "found", "type": "long"}],
                "rows": rows,
            }
        ]
    }


async def bearer() -> str:
    return "token-1"


@respx.mock
async def test_query_logs_posts_the_query_and_returns_rows() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=answer([["a", 2]])))
    rows = await query_logs(WORKSPACE, "AppTraces | take 1", timedelta(minutes=5), token=bearer)
    assert rows == [{"item": "a", "found": 2}]
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer token-1"
    assert json.loads(request.content) == {"query": "AppTraces | take 1", "timespan": "PT300S"}


@respx.mock
async def test_query_logs_reports_refusals_without_their_text() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            403, json={"error": {"code": "InsufficientAccessError", "message": "x"}}
        )
    )
    with pytest.raises(LogQueryError, match="403 InsufficientAccessError"):
        await query_logs(WORKSPACE, "q", "P1D", token=bearer)
    respx.post(URL).mock(return_value=httpx.Response(500, text="oops"))
    with pytest.raises(LogQueryError, match=r"500$"):
        await query_logs(WORKSPACE, "q", "P1D", token=bearer)
    with pytest.raises(ValueError, match="badly formed"):
        await query_logs("../other", "q", "P1D", token=bearer)


@respx.mock
async def test_query_logs_takes_the_cli_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    scopes: list[str] = []

    class Token:
        token = "cli-token"

    class Credential:
        async def __aenter__(self) -> Credential:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get_token(self, scope: str) -> Token:
            scopes.append(scope)
            return Token()

    monkeypatch.setattr(logquery, "AzureCliCredential", Credential)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=answer([])))
    assert await query_logs(WORKSPACE, "q", "PT1H") == []
    assert scopes == ["https://api.loganalytics.io/.default"]
    assert route.calls.last.request.headers["Authorization"] == "Bearer cli-token"


def test_rows_and_timespans() -> None:
    with pytest.raises(LogQueryError):
        rows_of({"tables": []})
    assert timespan_of("P1D") == "P1D"
    assert timespan_of(timedelta(seconds=0)) == "PT1S"


def test_chat_telemetry_query_names_the_three_items() -> None:
    kql = chat_telemetry_query(SINCE)
    assert "let since = datetime(2026-10-30T09:15:00.000000Z);" in kql
    assert 'AppRoleName == "docchat-api" and Name == "docchat.chat.turn"' in kql
    assert 'Properties["docchat.ttft_ms"]' in kql
    assert '"docchat.openai.tokens", "docchat.openai.estimated_cost"' in kql
    assert kql.count("ingestion_time() - TimeGenerated <= 15m") == 2


def test_found_items_ignores_zero_counts_and_strangers() -> None:
    rows = [
        {"item": "docchat.chat.turn", "found": 0},
        {"item": "docchat.openai.tokens", "found": 3},
        {"item": "other", "found": 5},
    ]
    assert found_items(rows) == {"docchat.openai.tokens"}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


async def test_t282_check_polls_until_every_item_is_found() -> None:
    answers = [
        [{"item": "docchat.chat.turn", "found": 1}],
        [{"item": "docchat.openai.tokens", "found": 1}],
        [{"item": "docchat.openai.estimated_cost", "found": 4}],
    ]
    seen: list[tuple[str, str, str]] = []

    async def run(workspace: str, kql: str, timespan: str) -> list[dict[str, Any]]:
        seen.append((workspace, kql, timespan))
        return answers.pop(0)

    clock = Clock()
    missing = await check_chat_telemetry(
        WORKSPACE,
        SINCE,
        run_query=run,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: SINCE + timedelta(minutes=20),
    )
    assert missing == []
    assert clock.waits == [60.0, 60.0]
    assert seen[0][2] == "2026-10-30T09:15:00+00:00/2026-10-30T09:35:00+00:00"


async def test_t282_check_gives_up_after_15_minutes() -> None:
    async def run(workspace: str, kql: str, timespan: str) -> list[dict[str, Any]]:
        del workspace, kql, timespan
        return [{"item": "docchat.openai.tokens", "found": 1}]

    clock = Clock()
    missing = await check_chat_telemetry(
        WORKSPACE, SINCE, run_query=run, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert missing == ["docchat.chat.turn", "docchat.openai.estimated_cost"]
    assert len(clock.waits) == 15


def test_parse_since_reads_utc() -> None:
    assert parse_since("2026-10-30T09:15:00Z") == SINCE
    assert parse_since("2026-10-30T09:15:00") == SINCE
    assert parse_since("2026-10-30T11:15:00+02:00") == SINCE


def test_main_exits_0_when_found(capsys: pytest.CaptureFixture[str]) -> None:
    async def run(workspace: str, kql: str, timespan: str) -> list[dict[str, Any]]:
        del kql, timespan
        assert workspace == WORKSPACE
        return [{"item": item, "found": 1} for item in CHAT_TELEMETRY_ITEMS]

    argv = ["chat-telemetry", "--workspace-id", WORKSPACE, "--since", "2026-10-30T09:15:00Z"]
    assert logquery.main(argv, run_query=run) == 0
    assert json.loads(capsys.readouterr().out) == {"found": list(CHAT_TELEMETRY_ITEMS)}


def test_main_exits_1_naming_missing_items(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(workspace: str, kql: str, timespan: str) -> list[dict[str, Any]]:
        del workspace, kql, timespan
        return [{"item": "docchat.chat.turn", "found": 1}]

    monkeypatch.setattr(logquery, "POLL_LIMIT", timedelta(0))
    argv = ["chat-telemetry", "--workspace-id", WORKSPACE, "--since", "2026-10-30T09:15:00Z"]
    assert logquery.main(argv, run_query=run) == 1
    line = json.loads(capsys.readouterr().err)
    assert line["code"] == "telemetry_missing"
    assert line["detail"] == ["docchat.openai.tokens", "docchat.openai.estimated_cost"]


def test_main_refuses_a_workspace_that_is_not_a_uuid() -> None:
    with pytest.raises(SystemExit) as caught:
        logquery.main(["chat-telemetry", "--workspace-id", "x", "--since", "2026-10-30T09:15:00Z"])
    assert caught.value.code == 2
