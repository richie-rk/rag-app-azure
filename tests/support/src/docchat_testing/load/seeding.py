"""`docchat-load seed` (`84-testing.md` section 7.4 job 4).

As `docchat-load-admin`, through the admin API: the project `Load test` flagged default (so every
load user reaches it), the index `load-chat` (the page-wise method at its default parameters)
mapped as its default and holding the four fixture documents, ingested and `completed`. Then one
`getCurrentUser` call per load user, which provisions each user, and, from 70 s after `seed-config`
applied the load ops config (the ops-config cache lifetime of 60 s plus margin), 500 single-turn
sessions for `docchat-load-01`, which S1's `sessions` scenario lists. Those sessions run 20 at a
time; one that doesn't end with `done` is sent once more, and the seed fails if it fails again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from docchat_testing.adminapi import (
    HTTP_CONFLICT,
    HTTP_CREATED,
    AdminApi,
    AdminApiError,
    UnusableResourceError,
    problem_code,
)
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.load.chat import send_turn
from docchat_testing.load.common import (
    ADMIN_ALIAS,
    LOAD_USERS,
    PROJECT_NAME,
    SESSION_USER,
    LoadError,
    token_of,
    utc_now,
)
from docchat_testing.seed import (
    DOCUMENTS_DIR,
    FIXTURE_DOCUMENTS,
    INDEX_METHOD,
    MappedIndex,
    ensure_documents,
    fixture_bytes,
    upload_file,
)

INDEX_NAME: Final = "load-chat"
PROJECT_DESCRIPTION: Final = "Created and used by the load test"
SESSIONS: Final = 500
SESSIONS_IN_FLIGHT: Final = 20
CONFIG_SETTLE: Final = timedelta(seconds=70)
INGEST_WITHIN_SECONDS: Final = 3600.0
HTTP_OK: Final = 200


@dataclass(frozen=True, slots=True)
class SeedResult:
    project_id: str
    index_id: str
    sessions: int


def static_token(token: str) -> Callable[[], Any]:
    async def bearer() -> str:
        return token

    return bearer


async def ensure_default_project(api: AdminApi) -> dict[str, Any]:
    """The active project `Load test` flagged default, created when absent."""
    found = await api.find_project(PROJECT_NAME)
    if found is None:
        response = await api.call(
            "adminCreateProject",
            "POST",
            "/v1/admin/projects",
            expected=(HTTP_CREATED, HTTP_CONFLICT),
            json={"name": PROJECT_NAME, "description": PROJECT_DESCRIPTION, "isDefault": True},
        )
        if response.status_code == HTTP_CREATED:
            created: dict[str, Any] = response.json()
            return created
        if problem_code(response) != "project_name_taken":
            raise AdminApiError("adminCreateProject", response)
        found = await api.find_project(PROJECT_NAME)
        if found is None:
            raise AdminApiError("adminCreateProject", response)
    if found["status"] != "active" or not found.get("isDefault"):
        raise UnusableResourceError(f"project {PROJECT_NAME} isn't an active default project")
    return found


async def provision_users(api_base_url: str, tokens: Mapping[str, str], transport: Any) -> None:
    """One `getCurrentUser` per load user."""
    for alias in LOAD_USERS:
        async with DeployedApiClient(
            api_base_url, static_token(token_of(tokens, alias)), transport=transport
        ) as client:
            response = await client.request("GET", "/v1/me")
        if response.status_code != HTTP_OK:
            raise LoadError(
                "seed_failed", f"getCurrentUser as {alias} answered {response.status_code}"
            )


async def seed_sessions(
    client: DeployedApiClient, project_id: str, questions: Sequence[str], count: int = SESSIONS
) -> int:
    """`count` single-turn sessions, 20 at a time; the number that ended with `done`. Twenty
    workers take the next session number in turn, so no more than 20 tasks ever exist."""
    numbers = iter(range(count))
    done = 0

    async def worker() -> None:
        nonlocal done
        for number in numbers:
            question = questions[number % len(questions)]
            for _ in range(2):
                result = await send_turn(client, project_id=project_id, question=question)
                if result.done:
                    done += 1
                    break

    await asyncio.gather(*(worker() for _ in range(min(SESSIONS_IN_FLIGHT, count))))
    return done


async def seed(
    api_base_url: str,
    tokens: Mapping[str, str],
    questions: Sequence[str],
    applied_at: datetime,
    *,
    transport: Any = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    now: Callable[[], datetime] = utc_now,
    poll_seconds: float = 15.0,
) -> SeedResult:
    async with DeployedApiClient(
        api_base_url, static_token(token_of(tokens, ADMIN_ALIAS)), transport=transport
    ) as admin_client:
        api = AdminApi(admin_client, sleep=sleep, poll_seconds=poll_seconds)
        project = await ensure_default_project(api)
        project_id = str(project["projectId"])
        index = await api.ensure_index(INDEX_NAME, method_key=INDEX_METHOD)
        index_id = str(index["indexId"])
        await api.ensure_mapping(project_id, index_id, is_default=True)
        files = [
            upload_file(name, fixture_bytes(name, DOCUMENTS_DIR)) for name in FIXTURE_DOCUMENTS
        ]
        await ensure_documents(
            api, MappedIndex(project_id, index_id), files, within=INGEST_WITHIN_SECONDS
        )
    await provision_users(api_base_url, tokens, transport)
    remaining = (applied_at + CONFIG_SETTLE - now()).total_seconds()
    if remaining > 0:
        await sleep(remaining)
    async with DeployedApiClient(
        api_base_url, static_token(token_of(tokens, SESSION_USER)), transport=transport
    ) as client:
        done = await seed_sessions(client, project_id, questions)
    if done < SESSIONS:
        raise LoadError("seed_failed", f"{SESSIONS - done} of the {SESSIONS} sessions failed twice")
    return SeedResult(project_id=project_id, index_id=index_id, sessions=done)
