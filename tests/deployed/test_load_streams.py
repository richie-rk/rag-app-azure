"""The load run's long chat streams (`84-testing.md` sections 5.14, 6.5 and 7.4 job 5).

T-597, the 200-second stream test, runs in the smoke suite of the load run only (load-test.yaml
job `smoke`): there `chat.max_turn_seconds` is 220 and the model stub answers, so a question
carrying `[[stub:first_ms=40000@stream]] [[stub:tokens=400]] [[stub:delay_ms=400]]` streams for
more than 200 s through platform authentication and the App Service front end. A second such
stream, closed by the client after 60 s, must read `cancelled` in `getSession`, and its
`POST /v1/chat/turns` row in `AppRequests` must end within 2 s of the close.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest

from docchat_testing.apiclient import DeployedApiClient, DeployedEnvironment
from docchat_testing.logquery import kql_datetime, query_logs
from docchat_testing.seed import SeedData

pytestmark = pytest.mark.deployed

type ClientFactory = Callable[[str | None], DeployedApiClient]
type TimedEvent = tuple[float, dict[str, Any]]

DIRECTIVES: Final = "[[stub:first_ms=40000@stream]] [[stub:tokens=400]] [[stub:delay_ms=400]]"
QUESTION: Final = "What does the water report say about deliveries?"
NDJSON: Final = "application/x-ndjson"
STREAM_TIMEOUT: Final = httpx.Timeout(300.0, connect=5.0)
LONGEST_STREAM_SECONDS: Final = 200.0
HEARTBEAT_WINDOW_SECONDS: Final = 40.0
CLOSE_AFTER_SECONDS: Final = 60.0
CLOSE_TOLERANCE: Final = timedelta(seconds=2)
SESSION_POLL_SECONDS: Final = 2.0
SESSION_POLL_LIMIT_SECONDS: Final = 60.0
LOG_POLL_SECONDS: Final = 60.0
LOG_POLL_LIMIT_SECONDS: Final = 600.0
WORKSPACE_VARIABLE: Final = "DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"
HTTP_OK: Final = 200
LOAD_ONLY: Final = (
    "runs only in load, where chat.max_turn_seconds is 220 and the model stub answers "
    "(84-testing.md section 7.3)"
)


def turn_body(project_id: str) -> dict[str, Any]:
    return {
        "projectId": project_id,
        "sessionId": None,
        "clientTurnId": str(uuid.uuid4()),
        "question": f"{QUESTION} {DIRECTIVES}",
    }


async def read_events(
    response: httpx.Response, *, close_after: float | None = None
) -> list[TimedEvent]:
    """Every event with the seconds since the stream started; with `close_after`, reading stops
    at the first line after that many seconds."""
    started = time.monotonic()
    events: list[TimedEvent] = []
    async for line in response.aiter_lines():
        if line:
            events.append((time.monotonic() - started, json.loads(line)))
        if close_after is not None and time.monotonic() - started >= close_after:
            break
    return events


@pytest.mark.smoke
@pytest.mark.full
async def test_t597_a_200_second_stream_reaches_done_uncut(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory, seed_data: SeedData
) -> None:
    if deployed_environment.name != "load":
        pytest.skip(LOAD_ONLY)
    async with api_client("member").stream(
        "POST",
        "/v1/chat/turns",
        json=turn_body(seed_data.project_id),
        headers={"Accept": NDJSON},
        timeout=STREAM_TIMEOUT,
    ) as response:
        assert response.status_code == HTTP_OK
        events = await read_events(response)

    types = [event["type"] for _, event in events]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert "error" not in types
    assert events[-1][0] >= LONGEST_STREAM_SECONDS
    early_heartbeats = [
        seconds
        for seconds, event in events
        if event["type"] == "heartbeat" and seconds <= HEARTBEAT_WINDOW_SECONDS
    ]
    assert len(early_heartbeats) >= 2


async def turn_status(client: DeployedApiClient, session_id: str, turn_id: str) -> str | None:
    response = await client.request("GET", f"/v1/sessions/{session_id}")
    if response.status_code != HTTP_OK:
        return None
    for turn in response.json()["turns"]:
        if turn["turnId"] == turn_id:
            return str(turn["status"])
    return None


async def request_end(trace_id: str, since: datetime) -> datetime | None:
    kql = (
        f"AppRequests | where TimeGenerated > {kql_datetime(since)} "
        f'and OperationId == "{trace_id}" and Name == "POST /v1/chat/turns" '
        "| project ended = TimeGenerated + DurationMs * 1ms"
    )
    rows = await query_logs(os.environ[WORKSPACE_VARIABLE], kql, "PT1H")
    if not rows:
        return None
    return datetime.fromisoformat(str(rows[0]["ended"]))


@pytest.mark.smoke
@pytest.mark.full
async def test_t597_a_stream_closed_after_60_seconds_reads_cancelled(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory, seed_data: SeedData
) -> None:
    if deployed_environment.name != "load":
        pytest.skip(LOAD_ONLY)
    client = api_client("member")
    trace_id = secrets.token_hex(16)
    started_at = datetime.now(UTC)
    async with client.stream(
        "POST",
        "/v1/chat/turns",
        json=turn_body(seed_data.project_id),
        headers={"Accept": NDJSON, "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"},
        timeout=STREAM_TIMEOUT,
    ) as response:
        assert response.status_code == HTTP_OK
        events = await read_events(response, close_after=CLOSE_AFTER_SECONDS)
    closed_at = datetime.now(UTC)
    assert events[0][1]["type"] == "meta"
    assert "done" not in [event["type"] for _, event in events]
    session_id, turn_id = str(events[0][1]["sessionId"]), str(events[0][1]["turnId"])

    status = None
    deadline = time.monotonic() + SESSION_POLL_LIMIT_SECONDS
    while time.monotonic() < deadline:
        status = await turn_status(client, session_id, turn_id)
        if status == "cancelled":
            break
        await asyncio.sleep(SESSION_POLL_SECONDS)
    assert status == "cancelled"

    ended = None
    deadline = time.monotonic() + LOG_POLL_LIMIT_SECONDS
    while ended is None and time.monotonic() < deadline:
        ended = await request_end(trace_id, started_at - timedelta(minutes=1))
        if ended is None:
            await asyncio.sleep(LOG_POLL_SECONDS)
    assert ended is not None, "the turn's AppRequests row didn't arrive within 10 minutes"
    assert abs(ended - closed_at) <= CLOSE_TOLERANCE
