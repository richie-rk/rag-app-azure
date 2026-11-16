"""Chat turns sent by the Python side of the load test: the seed's 500 sessions, the alert probe's
25 turns and the new turn of S2-e (`84-testing.md` sections 7.4 to 7.6).

Each turn goes through `DeployedApiClient` (the bearer token, a fresh `traceparent` and the one
retry of a rate-limited request) and its stream is validated by the streaming harness, so a turn
counts as done only when its NDJSON stream follows the protocol and ends with `done`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import ValidationError

from docchat_api.chat.stream import StreamMetaEvent
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.load.common import PROJECT_NAME, LoadError
from docchat_testing.streaming import StreamProtocolError, collect_stream

TURN_PATH: Final = "/v1/chat/turns"
NDJSON: Final = "application/x-ndjson"
HTTP_OK: Final = 200
STREAM_TIMEOUT: Final = httpx.Timeout(240.0, connect=5.0)
PAGE_LIMIT: Final = 100


@dataclass(frozen=True, slots=True)
class TurnResult:
    """How one turn ended: its status, the session its `meta` named and whether it ended with
    `done`; `reason` says why it didn't."""

    status: int
    session_id: str | None
    done: bool
    reason: str | None = None


async def find_project(client: DeployedApiClient, name: str = PROJECT_NAME) -> str:
    """The ID of the project `name` among the caller's projects (`listMyProjects`)."""
    params: dict[str, str | int] = {"limit": PAGE_LIMIT}
    while True:
        response = await client.request("GET", "/v1/projects", params=params)
        if response.status_code != HTTP_OK:
            raise LoadError(
                "project_unavailable", f"listMyProjects answered {response.status_code}"
            )
        page: dict[str, Any] = response.json()
        for item in page["items"]:
            if item["name"] == name:
                return str(item["projectId"])
        cursor = page.get("nextCursor")
        if not cursor:
            raise LoadError("project_missing", f"project {name} is missing: run docchat-load seed")
        params["cursor"] = cursor


async def send_turn(
    client: DeployedApiClient, *, project_id: str, question: str, session_id: str | None = None
) -> TurnResult:
    """One `createChatTurn` with a new `clientTurnId`, read to its end."""
    body = {
        "projectId": project_id,
        "sessionId": session_id,
        "clientTurnId": str(uuid.uuid4()),
        "question": question,
    }
    try:
        async with client.stream(
            "POST", TURN_PATH, json=body, headers={"Accept": NDJSON}, timeout=STREAM_TIMEOUT
        ) as response:
            if response.status_code != HTTP_OK:
                await response.aread()
                return TurnResult(
                    response.status_code, None, False, f"status_{response.status_code}"
                )
            try:
                events = await collect_stream(response)
            except (StreamProtocolError, ValidationError):
                return TurnResult(HTTP_OK, None, False, "invalid_stream")
    except httpx.HTTPError as error:
        return TurnResult(0, None, False, f"transport_{type(error).__name__}")
    meta = events[0]
    session = str(meta.session_id) if isinstance(meta, StreamMetaEvent) else None
    if events[-1].type != "done":
        return TurnResult(HTTP_OK, session, False, "error_event")
    return TurnResult(HTTP_OK, session, True)
