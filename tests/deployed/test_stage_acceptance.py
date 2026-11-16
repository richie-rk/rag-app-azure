"""Stage-acceptance rows of stages 0, 1 and 6 that a function can carry (`84-testing.md` sections
5.1 item 4 and 6.5; `90-build-order.md` section 1.3).

Each runs only with `--stage-acceptance` (the plugin skips the `stage acceptance`, `stage 0`,
`deployed (stage acceptance)` and `deployed (stage 0)` rows without it), in the job or by the
person `84-testing.md` section 6.3 names, and its result goes into the stage's acceptance record:

- T-032 (person-run, `full`): a `dev` test operator who has just joined
  `sg-docchat-dev-conversation-readers` takes a fresh token (`az logout`, `az login`) and runs it
  from a machine in `dev`'s operator allowlist; it retries a point read of a stored conversation
  item every 60 s for up to 15 minutes. `200` keeps group mode; `403` fails it with the change it
  calls for (`cosmosGroupDataAccess` `false` in `infra/env/dev.json`, the outcome recorded in ADR
  0024).
- T-440's reading (`infra`): the contained user `sql_principals.py` creates from its default SID
  source signs the ingestion identity in, shown by the workload audit's successful authentications
  of `id-docchat-ingest-dev`; the source goes into ADR 0125 and the record.
- T-207's deployed half (`infra`): the ingestion image of the release `dev` runs reported
  `ingestion_worker_started`, which a worker emits only after its start-up checks, the isolation
  probe of check 7 included, have passed. The census of loopback listeners and the spike build that
  takes it are the performer's (`90-build-order.md` section 2.16).
- T-534 (person-run, `full`): the read-back half of runbook "Repository and GitHub settings"
  (R16): the ruleset `main` against `.github/rulesets/main.json` and the repository settings of
  `01-repository.md` section 2.6, read with the performer's `gh` sign-in (repository
  administration, which no workflow token holds). The test pull request touching a security path
  is the performer's, recorded with its number.
- T-547 and T-548 (`full`): the runbook file check and the runbook title cross-check, which read
  only the repository and the specification.
- T-549 (`full`, level `rehearsal`): the release stage's reading of `docs/runbooks/rehearsals.md`,
  a `passed` row for every runbook in `dev` and R11 in `load`; it skips without
  `--stage-acceptance` like the stage rows, because the log fills only during the release stage.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity.aio import AzureCliCredential

from docchat_testing.logquery import query_logs

pytestmark = pytest.mark.deployed

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
SPEC: Final = REPOSITORY_ROOT / "docs" / "spec"
RUNBOOKS: Final = REPOSITORY_ROOT / "docs" / "runbooks"
RESOURCE_GROUP: Final = "rg-docchat-dev"
AZ_TIMEOUT_SECONDS: Final = 300.0
HTTP_OK: Final = 200


def person_run(reason: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip(f"person-run: {reason}")


def write_summary(lines: list[str]) -> None:
    """What the record copies, in the job summary when there is one and on standard output."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.writelines(f"{line}\n" for line in lines)
    sys.stdout.writelines(f"{line}\n" for line in lines)


async def run_json(executable_name: str, *args: str) -> Any:
    executable = shutil.which(executable_name)
    assert executable is not None, f"{executable_name} isn't on PATH"
    process = await asyncio.create_subprocess_exec(
        executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    async with asyncio.timeout(AZ_TIMEOUT_SECONDS):
        output, errors = await process.communicate()
    last = (errors.decode(errors="replace").strip().splitlines() or ["no error output"])[-1]
    assert process.returncode == 0, f"{executable_name} {args[0]} failed: {last[:300]}"
    return json.loads(output) if output.strip() else None


# --- T-032 (person-run) --------------------------------------------------------------------------

GROUP_READ_LIMIT_SECONDS: Final = 900.0
GROUP_READ_POLL_SECONDS: Final = 60.0
STORED_ITEM: Final = "SELECT TOP 1 c.id, c.userId, c.sessionId FROM c"


async def read_stored_item(endpoint: str) -> int:
    """The status of one point read of a stored conversation item, found by a query the reader
    role also allows: 200, or the refusal's status."""
    async with (
        AzureCliCredential() as credential,
        CosmosClient(endpoint, credential) as client,
    ):
        container = client.get_database_client("docchat").get_container_client("conversations")
        try:
            items = [item async for item in container.query_items(STORED_ITEM)]
            if not items:
                pytest.fail("conversations holds no item; run the deployed full suite first")
            item = items[0]
            await container.read_item(item["id"], partition_key=[item["userId"], item["sessionId"]])
        except CosmosHttpResponseError as error:
            return int(error.status_code or 0)
    return HTTP_OK


@pytest.mark.full
@pytest.mark.timeout(int(GROUP_READ_LIMIT_SECONDS) + 600)
async def test_t032_a_conversation_reader_group_member_reads_a_stored_item(
    azd_environment: dict[str, str],
) -> None:
    person_run("a dev test operator who has just joined the conversation readers' group")
    endpoint = azd_environment["DOCCHAT_COSMOS_ENDPOINT"]
    deadline = asyncio.get_running_loop().time() + GROUP_READ_LIMIT_SECONDS
    status = await read_stored_item(endpoint)
    while status != HTTP_OK and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(GROUP_READ_POLL_SECONDS)
        status = await read_stored_item(endpoint)
    write_summary([f"Cosmos DB group-principal test: the point read answered {status}"])

    assert status == HTTP_OK, (
        f"the point read answered {status} for 15 minutes: set cosmosGroupDataAccess to false "
        "in infra/env/dev.json by pull request and record the outcome in ADR 0024"
    )


# --- T-440 and T-207 (infra-checks, the dev deploy identity) --------------------------------------

SQL_PRINCIPALS: Final = REPOSITORY_ROOT / "scripts" / "deploy" / "sql_principals.py"
INGESTION_ROLE: Final = "docchat-ingestion"
RELEASE_TAG: Final = "docchat-release-sha"


def load_sql_principals() -> ModuleType:
    # scripts/deploy holds command-line scripts, not a package; the reading names the script's
    # own default SID source rather than a copy of it.
    sys.path.insert(0, str(SQL_PRINCIPALS.parent))
    name = "docchat_stage_acceptance_sql_principals"
    spec = importlib.util.spec_from_file_location(name, SQL_PRINCIPALS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.infra
async def test_t440_the_ingestion_identity_signs_in_with_the_default_sid_source(
    azd_environment: dict[str, str],
) -> None:
    source = str(load_sql_principals().parse_arguments(["--env", "dev"]).sid_source)
    rows = await query_logs(
        azd_environment["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"],
        """SQLSecurityAuditEvents
| where DatabasePrincipalName == "id-docchat-ingest-dev"
| where ActionName == "DATABASE AUTHENTICATION SUCCEEDED"
| summarize logins = count(), lastLogin = max(TimeGenerated)""",
        "P7D",
    )
    logins = int(rows[0]["logins"]) if rows else 0
    last = rows[0]["lastLogin"] if rows else "-"
    write_summary(
        [
            (
                f"SQL contained-user SID: the working source is {source} "
                f"({logins} ingestion logins in 7 days, the last at {last})"
            )
        ]
    )

    assert logins >= 1, f"no successful ingestion login in 7 days with --sid-source {source}"


@pytest.mark.infra
async def test_t207_the_deployed_ingestion_worker_passed_its_isolation_probe(
    azd_environment: dict[str, str],
) -> None:
    values = azd_environment
    tags = await run_json(
        "az", "functionapp", "show", "--name", values["DOCCHAT_INGESTION_APP_NAME"],
        "--resource-group", RESOURCE_GROUP, "--query", "tags", "--output", "json",
    )  # fmt: skip
    commit = str((tags or {}).get(RELEASE_TAG, ""))
    assert re.fullmatch(r"[0-9a-f]{40}", commit), f"the ingestion app's {RELEASE_TAG} isn't a SHA"
    rows = await query_logs(
        values["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"],
        f"""AppEvents
| where AppRoleName == "{INGESTION_ROLE}" and Name == "ingestion_worker_started"
| where AppVersion == "{commit}"
| summarize workers = count(), first = min(TimeGenerated)""",
        "P30D",
    )
    workers = int(rows[0]["workers"]) if rows else 0
    write_summary(
        [
            (
                f"Seccomp filter spike: {workers} ingestion worker start(s) of {commit} after the "
                "start-up checks and the isolation probe; the census goes into the stage 1 record."
            )
        ]
    )

    assert workers >= 1, f"no ingestion_worker_started for {commit}: a start-up check failed"


# --- T-534 (person-run) --------------------------------------------------------------------------

RULESET_FILE: Final = REPOSITORY_ROOT / ".github" / "rulesets" / "main.json"


def subset_problems(label: str, expected: Any, actual: Any) -> list[str]:
    """Each value of `expected` that `actual` doesn't hold; lists compare as sets of items."""
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [f"{label} is {actual!r}"]
        return [
            problem
            for key, value in expected.items()
            for problem in subset_problems(f"{label}.{key}", value, actual.get(key))
        ]
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{label} is {actual!r}"]
        wanted = {json.dumps(item, sort_keys=True) for item in expected}
        found = {json.dumps(item, sort_keys=True) for item in actual}
        return [f"{label} lacks {item}" for item in sorted(wanted - found)]
    return [] if expected == actual else [f"{label} is {actual!r}, not {expected!r}"]


def ruleset_problems(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("target", "enforcement", "conditions", "bypass_actors"):
        problems += subset_problems(key, expected[key], actual.get(key))
    found = {str(rule["type"]): rule for rule in actual.get("rules") or []}
    wanted = {str(rule["type"]): rule for rule in expected["rules"]}
    problems += [f"rule {kind} is missing" for kind in sorted(set(wanted) - set(found))]
    problems += [f"rule {kind} is extra" for kind in sorted(set(found) - set(wanted))]
    for kind in sorted(set(wanted) & set(found)):
        problems += subset_problems(
            kind, wanted[kind].get("parameters") or {}, found[kind].get("parameters") or {}
        )
    return problems


def settings_problems(answers: Mapping[str, Any]) -> list[str]:
    """`01-repository.md` section 2.6's repository settings against the API's answers."""
    repository = answers["repository"]
    analysis = repository.get("security_and_analysis") or {}
    expected = {
        "secret scanning": (analysis.get("secret_scanning") or {}).get("status") == "enabled",
        "push protection": (analysis.get("secret_scanning_push_protection") or {}).get("status")
        == "enabled",
        "Dependabot security updates": (analysis.get("dependabot_security_updates") or {}).get(
            "status"
        )
        == "enabled",
        "Dependabot alerts": answers["vulnerability_alerts"] is True,
        "private vulnerability reporting": (answers["private_reporting"] or {}).get("enabled")
        is True,
        "selected actions only": (answers["actions"] or {}).get("allowed_actions") == "selected",
        "read-only workflow token": (answers["workflow"] or {}).get("default_workflow_permissions")
        == "read",
        "Actions can't approve pull requests": (answers["workflow"] or {}).get(
            "can_approve_pull_request_reviews"
        )
        is False,
    }
    return [f"{setting} doesn't read back as section 2.6 states" for setting, ok in expected.items()
            if not ok]  # fmt: skip


async def gh_succeeds(*args: str) -> bool:
    executable = shutil.which("gh")
    assert executable is not None, "the GitHub CLI isn't on PATH"
    process = await asyncio.create_subprocess_exec(
        executable, *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    async with asyncio.timeout(AZ_TIMEOUT_SECONDS):
        return await process.wait() == 0


@pytest.mark.full
async def test_t534_the_ruleset_and_repository_settings_read_back_as_designed() -> None:
    person_run("repository administration reads need the performer's gh sign-in")
    repository = str((await run_json("gh", "repo", "view", "--json", "nameWithOwner"))[
        "nameWithOwner"
    ])  # fmt: skip
    expected = json.loads(RULESET_FILE.read_text(encoding="utf-8"))
    listed = await run_json("gh", "api", f"/repos/{repository}/rulesets")
    (summary,) = [item for item in listed or [] if item.get("name") == expected["name"]]
    ruleset = await run_json("gh", "api", f"/repos/{repository}/rulesets/{summary['id']}")
    base = f"/repos/{repository}"
    answers = {
        "repository": await run_json("gh", "api", base),
        # 204 when Dependabot alerts are on, 404 when they are off.
        "vulnerability_alerts": await gh_succeeds("api", f"{base}/vulnerability-alerts"),
        "private_reporting": await run_json("gh", "api", f"{base}/private-vulnerability-reporting"),
        "actions": await run_json("gh", "api", f"{base}/actions/permissions"),
        "workflow": await run_json("gh", "api", f"{base}/actions/permissions/workflow"),
    }

    assert ruleset_problems(expected, ruleset) == []
    assert settings_problems(answers) == []


# --- T-547 and T-548 ------------------------------------------------------------------------------

INDEX_ROW: Final = re.compile(r"^\| (R\d{2}) \| ([^|]+?) \|")
TITLE_REFERENCE: Final = re.compile(r'\brunbooks? ((?:"[^"]+"(?:,? and |, | or )?)+)')
QUOTED: Final = re.compile(r'"([^"]+)"')
PLACEHOLDER: Final = re.compile(r"^<[^>]+>$")
CROSS_CHECKED: Final = (
    "00-overview.md",
    "01-repository.md",
    "13-conversations-and-memory.md",
    "14-ingestion.md",
    "81-config.md",
    "82-security.md",
    "83-infrastructure.md",
    "84-testing.md",
    "85-observability.md",
)


def runbook_index() -> dict[str, str]:
    """`86-operations.md` section 3.2: runbook ID to title."""
    text = (SPEC / "86-operations.md").read_text(encoding="utf-8")
    section = text.split("### 3.2 Runbook index", 1)[1].split("\n### ", 1)[0]
    return {
        match.group(1): match.group(2).strip()
        for line in section.splitlines()
        if (match := INDEX_ROW.match(line))
    }


def runbook_titles_named(text: str) -> list[str]:
    """Every title a `runbook "<title>"` or `runbooks "<a>" and "<b>"` reference names."""
    return [
        title
        for reference in TITLE_REFERENCE.finditer(text)
        for title in QUOTED.findall(reference.group(1))
        if not PLACEHOLDER.match(title)
    ]


@pytest.mark.full
def test_t547_every_runbook_file_opens_with_its_index_heading() -> None:
    index = runbook_index()
    files = sorted(RUNBOOKS.glob("R*.md"))
    headings = {path.name[:3]: path.read_text(encoding="utf-8").splitlines()[0] for path in files}

    # The index of 86-operations.md section 3.2, which owns the runbooks, holds 37 rows
    # (its criterion 15; 90-build-order.md A6-2); the count is the index's.
    assert len(files) == len(index)
    assert headings == {
        identifier: f"# {identifier} — {title}" for identifier, title in index.items()
    }
    assert (RUNBOOKS / "rehearsals.md").is_file()


@pytest.mark.full
def test_t548_every_runbook_title_another_file_names_is_an_index_row() -> None:
    titles = set(runbook_index().values())
    unknown = [
        f"{name}: {title!r}"
        for name in CROSS_CHECKED
        for title in runbook_titles_named((SPEC / name).read_text(encoding="utf-8"))
        if title not in titles
    ]

    assert unknown == []


# --- T-549 (the release stage's reading of the rehearsal log) -------------------------------------

LOG_HEADER: Final = (
    "| Date (UTC) | Runbook | Operator | Environment | Result | Correlation or run IDs |"
)
FALLBACK_ONLY: Final = frozenset({"R25", "R27"})
"""Rehearsed only once content-share fallback 1 is adopted (`86-operations.md` section 4.2)."""


def passed_rehearsals() -> set[tuple[str, str]]:
    """(runbook, environment) of every `passed` row of the log."""
    lines = (RUNBOOKS / "rehearsals.md").read_text(encoding="utf-8").splitlines()
    rows = lines[lines.index(LOG_HEADER) + 2 :]
    cells = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows if row]
    return {(cell[1], cell[3]) for cell in cells if len(cell) >= 5 and cell[4] == "passed"}


@pytest.mark.full
def test_t549_every_runbook_has_a_passed_rehearsal_row(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--stage-acceptance"):
        pytest.skip("read at the release stage's acceptance, with --stage-acceptance")
    passed = passed_rehearsals()
    # Whether each row is dated after the last change to its runbook's commands is the
    # performer's reading, recorded with the stage.
    missing = [
        f"{identifier} in {'load' if identifier == 'R11' else 'dev'}"
        for identifier in runbook_index()
        if identifier not in FALLBACK_ONLY
        and (identifier, "load" if identifier == "R11" else "dev") not in passed
    ]

    assert missing == []
