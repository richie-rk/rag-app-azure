"""Deployed chat rows (`84-testing.md` sections 5.6 and 6.5): the chat test T-281 and the stream
compression test T-271 (`smoke`, so also `full`), and against the real `dev` services the
screening test T-240 and the content-filter test T-248 (`full`, `dev`).

Every turn goes through platform authentication as the suite's `member` on the seeded project
`Automated tests`, whose default index holds the four fixture documents (section 6.6). The
questions are answerable from `tables.pdf`. Chat turns share the user's request rate (3 a minute
by default), so `send_chat_turn` waits out a `429 rate_limited` between the suite's turns.

The stream carries no screening record (`12-chat-pipeline.md` section 14.2), so T-240 reads a
flagged source from what the turn leaves out: the turn ends `done`, which needs Prompt Shields to
accept the empty `userPrompt` of every screening call (section 8.4 ends the turn with
`source_screening_unavailable` on a `400`), and the uploaded document, the only one about
supplier onboarding, is neither cited nor the source of an allowed link.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from docchat_api.chat.stream import StreamCitationsEvent
from docchat_testing.adminapi import AdminApi
from docchat_testing.apiclient import DeployedApiClient, DeployedEnvironment
from docchat_testing.deployedchat import ChatTurn, send_chat_turn
from docchat_testing.seed import (
    SeedData,
    byte_unique,
    delete_documents,
    fixture_bytes,
    upload_file,
)

pytestmark = pytest.mark.deployed

type ClientFactory = Callable[[str | None], DeployedApiClient]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
JAILBREAK_FILE: Final = REPOSITORY_ROOT / "tests" / "fixtures" / "questions" / "jailbreak.json"
INDIRECT_ATTACK: Final = "indirect-attack.pdf"
INDIRECT_ATTACK_LINK: Final = "https://example.net/unlock"
DOCUMENT_WAIT_SECONDS: Final = 360
"""The upload's ingestion and its deletion each, so the test fits its 900 s (section 1.2)."""

META_LIMIT_SECONDS: Final = 3.0
"""SP-9: the `meta` line within 3 s, so platform authentication doesn't buffer the stream."""

PLANTS_QUESTION: Final = (
    "Which treatment plants does the Fernhollow Valley Water Cooperative run, and which of them "
    "treat groundwater?"
)
ONBOARDING_QUESTION: Final = (
    "What does the Fernhollow Valley supplier onboarding checklist say about confirming a "
    "supplier's bank details?"
)
HTTP_OK: Final = 200
PDF_MAGIC: Final = b"%PDF-"


def allowed_links(turn: ChatTurn) -> list[str]:
    for event in turn.events:
        if isinstance(event, StreamCitationsEvent):
            return [str(link) for link in event.allowed_links]
    return []


@pytest.mark.smoke
@pytest.mark.full
async def test_t281_a_chat_turn_streams_with_citations_that_open_as_sandboxed_pdfs(
    api_client: ClientFactory, seed_data: SeedData
) -> None:
    member = api_client("member")
    turn = await send_chat_turn(member, project_id=seed_data.project_id, question=PLANTS_QUESTION)
    assert turn.status_code == HTTP_OK, turn.describe()
    assert turn.first_line_seconds is not None
    assert turn.first_line_seconds <= META_LIMIT_SECONDS, (
        f"the meta line arrived after {turn.first_line_seconds:.1f} s"
    )
    turn.require_done()
    assert turn.citations, "the answer cites no source"

    meta = turn.meta
    number = turn.citations[0].number
    path = f"/v1/sessions/{meta.session_id}/turns/{meta.turn_id}/citations/{number}/content"
    content = await member.request("GET", path)
    assert content.status_code == HTTP_OK, f"getCitationContent answered {content.status_code}"
    assert content.headers["content-type"] == "application/pdf"
    assert content.headers.get("content-security-policy") == "sandbox"
    assert content.headers.get("content-disposition", "").startswith("attachment")
    assert content.content.startswith(PDF_MAGIC)


@pytest.mark.smoke
@pytest.mark.full
async def test_t271_the_chat_stream_is_never_compressed(
    api_client: ClientFactory, seed_data: SeedData
) -> None:
    turn = await send_chat_turn(
        api_client("member"),
        project_id=seed_data.project_id,
        question=PLANTS_QUESTION,
        headers={"Accept-Encoding": "gzip, br"},
    )
    assert turn.status_code == HTTP_OK, turn.describe()
    assert turn.headers["content-type"].startswith("application/x-ndjson")
    assert "content-encoding" not in turn.headers, turn.headers.get("content-encoding")
    turn.require_done()


def dev_only(environment: DeployedEnvironment, what: str) -> None:
    if environment.name != "dev":
        pytest.skip(f"{what} runs against the real dev services only (84-testing.md section 5.6)")


@pytest.mark.full
async def test_t240_a_source_holding_an_indirect_attack_is_dropped_as_flagged(
    deployed_environment: DeployedEnvironment,
    api_client: ClientFactory,
    admin_api: AdminApi,
    seed_data: SeedData,
    run_label: str,
) -> None:
    dev_only(deployed_environment, "the screening test")
    stem = f"indirect-attack-{run_label}"
    data = byte_unique(fixture_bytes(INDIRECT_ATTACK), INDIRECT_ATTACK, stem)
    reservation = await admin_api.upload(
        seed_data.index_id, upload_file(f"{stem}.pdf", data), seed_data.project_id
    )
    try:
        run_id = (await admin_api.submit(seed_data.index_id, [reservation.version_id]))["runId"]
        run = await admin_api.wait_for_run(str(run_id), within=DOCUMENT_WAIT_SECONDS)
        assert run["run"]["state"] == "completed", f"its ingestion ended {run['run']['state']}"
        turn = await send_chat_turn(
            api_client("member"), project_id=seed_data.project_id, question=ONBOARDING_QUESTION
        )
    finally:
        await delete_documents(
            admin_api, seed_data.index_id, [reservation.document_id], within=DOCUMENT_WAIT_SECONDS
        )
    turn.require_done()
    cited = [citation.title for citation in turn.citations]
    assert not [title for title in cited if title.startswith(stem)], "the flagged source is cited"
    assert INDIRECT_ATTACK_LINK not in allowed_links(turn)


@pytest.mark.full
async def test_t248_a_jailbreak_question_ends_with_question_filtered(
    deployed_environment: DeployedEnvironment, api_client: ClientFactory, seed_data: SeedData
) -> None:
    dev_only(deployed_environment, "the content-filter test")
    question = str(json.loads(JAILBREAK_FILE.read_text(encoding="utf-8"))["question"])
    turn = await send_chat_turn(
        api_client("member"), project_id=seed_data.project_id, question=question
    )
    assert turn.status_code == HTTP_OK, turn.describe()
    assert turn.error is not None, f"the turn ended with {turn.describe()}"
    assert turn.error.code == "question_filtered"
    assert turn.error.retryable is False
