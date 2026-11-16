"""Deployed membership rows (`84-testing.md` sections 5.2 and 6.5): the membership-removal test
T-028 (`full`) and the operator-read audit test T-033 (stage acceptance, person-run).

T-028 removes `member` from the seeded project through the admin API, sends `createChatTurn`
until it answers `404 project_not_found`, and re-adds the membership in a `finally` block, then
sends again until a turn completes. Memberships are never cached (`11-api.md` section 1.10), so
the first answer after a change already shows it; the bound is the 60-second lifetime of the
API's in-process caches. Chat turns share the user's request rate, and a wait for that limiter
isn't the access cache's delay, so it is left out of the measured time.

T-033 needs a person (section 6.5; `90-build-order.md` section 1.3.1 step 4). The stage's
performer, an operator holding the just-in-time level `read`, reads a user's conversation with
`docchat-ops --env dev user read-conversation` (runbook R19 step 3), then, signed in with
`az login` as the same person and holding Log Analytics Reader on `log-docchat-dev` through
`sg-docchat-operators`, runs this function from the checkout with `.azure/dev/.env` refreshed:
`uv run pytest tests/deployed/test_membership.py --deployed --stage-acceptance -m full`. It finds
the read in `CDBDataPlaneRequests` under the caller's own `AadPrincipalId` within 30 minutes and
shows that the deployed `human-conversation-access` rule's query selects it; the performer records
the alert's notification time in the acceptance record. In a workflow job it skips, because no
job holds an operator's identity.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from azure.identity.aio import AzureCliCredential

from docchat_core.clients.tls import TLS_CONTEXT
from docchat_testing.adminapi import AdminApi
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.deployedchat import ChatTurn, send_chat_turn
from docchat_testing.logquery import Rows, kql_datetime, query_logs
from docchat_testing.seed import SeedData
from docchat_testing.tokens import unverified_claims

pytestmark = pytest.mark.deployed

type ClientFactory = Callable[[str | None], DeployedApiClient]

ACCESS_CACHE_SECONDS: Final = 60.0
RETRY_SECONDS: Final = 10.0
QUESTION: Final = "Which treatment plants does the Fernhollow Valley Water Cooperative run?"
HTTP_OK: Final = 200
HTTP_NOT_FOUND: Final = 404


async def turn_until(
    member: DeployedApiClient,
    project_id: str,
    reached: Callable[[ChatTurn], bool],
    *,
    within: float,
) -> tuple[ChatTurn, float]:
    """Send chat turns until `reached` holds, for at most `within` seconds not counting waits
    for the request rate; the last turn and the seconds it took."""
    started = time.monotonic()
    rate_waits = 0.0
    while True:
        turn = await send_chat_turn(member, project_id=project_id, question=QUESTION)
        rate_waits += turn.rate_waited_seconds
        elapsed = time.monotonic() - started - rate_waits
        if reached(turn) or elapsed >= within:
            return turn, elapsed
        await asyncio.sleep(RETRY_SECONDS)


def project_not_found(turn: ChatTurn) -> bool:
    return turn.status_code == HTTP_NOT_FOUND and turn.problem_code == "project_not_found"


def completed(turn: ChatTurn) -> bool:
    return turn.status_code == HTTP_OK and turn.done is not None


@pytest.mark.full
async def test_t028_a_removed_member_loses_the_project_and_regains_it_when_re_added(
    api_client: ClientFactory, admin_api: AdminApi, seed_data: SeedData
) -> None:
    member = api_client("member")
    me = await member.request("GET", "/v1/me")
    assert me.status_code == HTTP_OK
    member_id = str(me.json()["userId"])
    project_id = seed_data.project_id

    assert await admin_api.remove_member(project_id, member_id), "member wasn't a member"
    try:
        refused, refused_after = await turn_until(
            member, project_id, project_not_found, within=ACCESS_CACHE_SECONDS
        )
    finally:
        await admin_api.ensure_member(project_id, member_id)
    assert project_not_found(refused), f"after the removal the turn ended {refused.describe()}"
    assert refused_after <= ACCESS_CACHE_SECONDS

    restored, restored_after = await turn_until(
        member, project_id, completed, within=ACCESS_CACHE_SECONDS
    )
    assert completed(restored), f"after re-adding the turn ended {restored.describe()}"
    assert restored_after <= ACCESS_CACHE_SECONDS


# --- T-033 (person-run) ---------------------------------------------------------------------

LOGS_SCOPE: Final = "https://api.loganalytics.io/.default"
ARM: Final = "https://management.azure.com"
ARM_SCOPE: Final = "https://management.azure.com/.default"
SCHEDULED_QUERY_RULES_API: Final = "2026-03-01"
RULE: Final = "docchat-dev-human-conversation-access"
RESOURCE_GROUP: Final = "rg-docchat-dev"
APPEARANCE_LIMIT: Final = timedelta(minutes=30)
POLL_SECONDS: Final = 60.0
RULE_WINDOW: Final = "ago(15m)"
"""The rule's own window (`13-conversations-and-memory.md` section 10.3), moved back to the read
so the check doesn't depend on when the row arrived."""


def read_query(operator: str, since: datetime) -> str:
    """The caller's data-plane requests on `docchat` after `since`, with how late each arrived."""
    return f"""CDBDataPlaneRequests
| where TimeGenerated > {kql_datetime(since)} and DatabaseName == "docchat"
| where AadPrincipalId == "{operator}"
| summarize requests = count(), firstSeen = min(TimeGenerated),
    delaySeconds = min(ingestion_time() - TimeGenerated) / 1s"""


async def rule_definition(values: dict[str, str]) -> dict[str, Any]:
    path = (
        f"/subscriptions/{values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Insights/scheduledQueryRules/{RULE}"
    )
    async with AzureCliCredential() as credential:
        bearer = (await credential.get_token(ARM_SCOPE)).token
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=False,
        headers={"Authorization": f"Bearer {bearer}"},
        verify=TLS_CONTEXT,
    ) as client:
        response = await client.get(
            f"{ARM}{path}", params={"api-version": SCHEDULED_QUERY_RULES_API}
        )
    assert response.status_code == HTTP_OK, f"GET {RULE} answered {response.status_code}"
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.full
@pytest.mark.timeout(int(APPEARANCE_LIMIT.total_seconds()) + 600)
async def test_t033_an_operators_read_reaches_the_audit_table_and_the_alert_rule(
    azd_environment: dict[str, str],
) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip(
            "person-run: the stage's performer runs it after their own just-in-time read "
            "(84-testing.md section 6.5)"
        )
    values = azd_environment
    workspace = values["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"]
    async with AzureCliCredential() as credential:
        token = (await credential.get_token(LOGS_SCOPE)).token
    operator = str(unverified_claims(token)["oid"])
    started = datetime.now(UTC)
    since = started - APPEARANCE_LIMIT

    rows: Rows = []
    deadline = time.monotonic() + APPEARANCE_LIMIT.total_seconds()
    while time.monotonic() < deadline:
        rows = await query_logs(workspace, read_query(operator, since), "PT2H")
        if rows and rows[0]["requests"]:
            break
        await asyncio.sleep(POLL_SECONDS)
    assert rows, "the CDBDataPlaneRequests query answered no row"
    assert rows[0]["requests"], "no data-plane request of yours on docchat in 30 minutes"
    assert float(rows[0]["delaySeconds"]) <= APPEARANCE_LIMIT.total_seconds()
    first_seen = datetime.fromisoformat(str(rows[0]["firstSeen"]).replace("Z", "+00:00"))

    rule = await rule_definition(values)
    properties = rule["properties"]
    assert properties["enabled"] is True
    assert properties["severity"] == 3
    assert properties["evaluationFrequency"] == properties["windowSize"] == "PT15M"
    scopes = [str(scope).casefold() for scope in properties["scopes"]]
    assert values["DOCCHAT_LOG_ANALYTICS_RESOURCE_ID"].casefold() in scopes
    (criterion,) = properties["criteria"]["allOf"]
    query = str(criterion["query"])
    api_principal = values["DOCCHAT_API_IDENTITY_PRINCIPAL_ID"]
    assert f'AadPrincipalId != "{api_principal}"' in query
    assert RULE_WINDOW in query, "the rule's query no longer reads its 15-minute window"
    window_start = first_seen - timedelta(minutes=1)
    selected = await query_logs(
        workspace, query.replace(RULE_WINDOW, kql_datetime(window_start)), "PT2H"
    )
    assert operator in {str(row["AadPrincipalId"]) for row in selected}, (
        "the alert's query doesn't select your read"
    )
