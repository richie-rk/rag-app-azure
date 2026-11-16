"""The deployed memory and feedback test T-586 (`84-testing.md` sections 5.13 and 6.5), `full`:
for `member`, feedback saves and memory edits round-trip through the API
(`13-conversations-and-memory.md` sections 7.3 to 7.8 and 8.1).

Feedback is saved on a turn of the test's own and read back through `listSessionTurns`. Memory
edits are the pause switch and, once extraction has written a fact from a turn of this test, an
edit and a delete of that fact. Users can't create facts, so the fact test clears the memory
first (a fact a failed run left behind would make the new one a duplicate) and waits for the
after-response extraction of its own turn, whose fact names that turn's session. Every memory
write carries the `etag` of the view it changes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Final

import pytest

from docchat_testing.adminapi import problem_code
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.builders import canary
from docchat_testing.deployedchat import send_chat_turn
from docchat_testing.seed import SeedData

pytestmark = pytest.mark.deployed

type ClientFactory = Callable[[str | None], DeployedApiClient]

QUESTION: Final = "Which treatment plants does the Fernhollow Valley Water Cooperative run?"
SELF_STATEMENT_QUESTION: Final = (
    "I work as a procurement analyst in the finance team. Which treatment plants does the "
    "Fernhollow Valley Water Cooperative run?"
)
EDITED_FACT: Final = "Works on supplier contract renewals"
EDITED_CATEGORY: Final = "topic"
EXTRACTION_WAIT_SECONDS: Final = 300.0
POLL_SECONDS: Final = 10.0
HTTP_OK: Final = 200
MEMORY: Final = "/v1/memory"


def if_match(view: dict[str, Any]) -> dict[str, str]:
    """The view's `etag`, sent back verbatim (section 7.3)."""
    return {"If-Match": str(view["etag"])}


async def get_memory(member: DeployedApiClient) -> dict[str, Any]:
    response = await member.request("GET", MEMORY)
    assert response.status_code == HTTP_OK, f"getMemory answered {response.status_code}"
    view: dict[str, Any] = response.json()
    return view


async def memory_write(
    member: DeployedApiClient, operation: str, method: str, path: str, view: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:  # fmt: skip
    response = await member.request(method, path, headers=if_match(view), **kwargs)
    assert response.status_code == HTTP_OK, (
        f"{operation} answered {response.status_code} {problem_code(response) or ''}"
    )
    changed: dict[str, Any] = response.json()
    return changed


async def set_paused(
    member: DeployedApiClient, view: dict[str, Any], paused: bool
) -> dict[str, Any]:
    return await memory_write(
        member, "updateMemorySettings", "PATCH", MEMORY, view, json={"paused": paused}
    )


@pytest.mark.full
async def test_t586_feedback_saves_round_trip(
    api_client: ClientFactory, seed_data: SeedData
) -> None:
    member = api_client("member")
    me = await member.request("GET", "/v1/me")
    assert me.status_code == HTTP_OK
    comments = bool(me.json()["features"]["feedbackComments"])
    turn = await send_chat_turn(member, project_id=seed_data.project_id, question=QUESTION)
    turn.require_done()
    session_id, turn_id = turn.meta.session_id, turn.meta.turn_id
    feedback = f"/v1/sessions/{session_id}/turns/{turn_id}/feedback"
    comment = canary("comment") if comments else None

    async def stored() -> dict[str, Any] | None:
        response = await member.request("GET", f"/v1/sessions/{session_id}/turns")
        assert response.status_code == HTTP_OK, f"listSessionTurns answered {response.status_code}"
        (view,) = [item for item in response.json()["items"] if item["turnId"] == str(turn_id)]
        value: dict[str, Any] | None = view["feedback"]
        return value

    down = {"rating": "down", "category": "incomplete", "comment": comment}
    saved = await member.request("PUT", feedback, json=down)
    assert saved.status_code == HTTP_OK, f"putTurnFeedback answered {saved.status_code}"
    assert {key: saved.json()[key] for key in down} == down
    read = await stored()
    assert read is not None
    assert {key: read[key] for key in down} == down

    up = {"rating": "up", "category": None, "comment": None}
    changed = await member.request("PUT", feedback, json=up)
    assert changed.status_code == HTTP_OK, f"putTurnFeedback answered {changed.status_code}"
    read = await stored()
    assert read is not None
    assert {key: read[key] for key in up} == up
    assert read["updatedAt"] == changed.json()["updatedAt"]


@pytest.mark.full
async def test_t586_the_memory_pause_switch_round_trips(api_client: ClientFactory) -> None:
    member = api_client("member")
    view = await get_memory(member)
    original = bool(view["paused"])
    switched = await set_paused(member, view, not original)
    assert switched["paused"] is (not original)
    assert switched["etag"] != view["etag"]
    assert (await get_memory(member))["paused"] is (not original)
    restored = await set_paused(member, switched, original)
    assert restored["paused"] is original
    assert (await get_memory(member))["paused"] is original


@pytest.mark.full
async def test_t586_a_fact_edit_and_delete_round_trip(
    api_client: ClientFactory, seed_data: SeedData
) -> None:
    member = api_client("member")
    view = await get_memory(member)
    if not view["enabled"]:
        pytest.skip("memory.enabled is false in this environment's ops config, so no fact is made")
    if view["paused"]:
        view = await set_paused(member, view, False)
    if view["facts"]:
        view = await memory_write(member, "clearMemory", "DELETE", f"{MEMORY}/facts", view)

    turn = await send_chat_turn(
        member, project_id=seed_data.project_id, question=SELF_STATEMENT_QUESTION
    )
    turn.require_done()
    session_id = str(turn.meta.session_id)

    deadline = time.monotonic() + EXTRACTION_WAIT_SECONDS
    extracted: list[dict[str, Any]] = []
    while not extracted and time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        view = await get_memory(member)
        extracted = [fact for fact in view["facts"] if fact["source"]["sessionId"] == session_id]
    assert extracted, f"no fact was extracted from the turn within {EXTRACTION_WAIT_SECONDS:.0f} s"
    fact_id = str(extracted[0]["factId"])
    path = f"{MEMORY}/facts/{fact_id}"

    edited = await memory_write(
        member, "updateMemoryFact", "PUT", path, view,
        json={"text": EDITED_FACT, "category": EDITED_CATEGORY},
    )  # fmt: skip
    (fact,) = [item for item in edited["facts"] if item["factId"] == fact_id]
    assert (fact["text"], fact["category"]) == (EDITED_FACT, EDITED_CATEGORY)
    assert fact["source"]["kind"] == "user"
    (read,) = [item for item in (await get_memory(member))["facts"] if item["factId"] == fact_id]
    assert (read["text"], read["category"], read["source"]["kind"]) == (
        EDITED_FACT,
        EDITED_CATEGORY,
        "user",
    )

    deleted = await memory_write(member, "deleteMemoryFact", "DELETE", path, edited)
    assert fact_id not in {item["factId"] for item in deleted["facts"]}
    assert fact_id not in {item["factId"] for item in (await get_memory(member))["facts"]}
