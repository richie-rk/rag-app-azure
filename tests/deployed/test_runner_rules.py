"""The transient runner rules (`83-infrastructure.md` section 4.6): the Activity Log check after a
deploy (T-445) and the deployed test-vault network test (T-459).

T-445 runs alone as a step of the `dev` deploy job of `deploy-environment.yaml`, after the job's
last SQL rule is closed, as the `dev` deploy identity with `.azure/dev/.env` refreshed:
`uv run pytest --deployed tests/deployed/test_runner_rules.py::test_t445_…`. The Activity Log
answers within minutes, so it polls for at most 20 minutes for the create and the delete of the
job's rule `ci-runner-<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>`, each under the deploy identity
(`claims.appid` is its client ID), then reads that no `ci-runner-*` rule is left on the SQL
server. Registry IP rules carry no name: the check reads that none holds this runner's address,
and, when the shared allowlist is known (`DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST`, which no job
holds, section 10.4), that every rule is inside it.

T-459 runs in the `pytest` job of `deployed-tests.yaml` (`dev-tests`, as `id-docchat-citest`)
with `--stage-acceptance`, at the acceptance of the stage that provisions `dev` (`84-testing.md`
section 8.4), marker `full`; without the flag it skips, so the nightly `full` run leaves the
vault's rules alone. By default it reads one `test-user-<alias>` secret before
`firewall.py open --target vault` (refused with `403 ForbiddenByFirewall`), inside the window
(read) and after `close` (refused again within 300 s). Under the private-networking option the
vault's name resolves to a private address and the read succeeds with no window. The rest of the
row needs another identity or another job: the Activity Log half (the window's two
`Microsoft.KeyVault/vaults/write` events under `id-docchat-citest`, no vault write by another
pipeline identity, no address outside the shared allowlist) is the person-run function below,
which the stage's performer runs with their own `az login` and the shared allowlist in their
shell; the private-networking option's refusal from a standard runner is a second job the
performer dispatches and records.
"""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import os
import shutil
import socket
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from urllib.parse import urlsplit

import pytest

from docchat_testing.apiclient import DeployedEnvironment
from docchat_testing.tokens import VaultForbiddenError, read_vault, run_firewall

pytestmark = pytest.mark.deployed

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
FIREWALL_SCRIPT: Final = REPOSITORY_ROOT / "scripts" / "deploy" / "firewall.py"
RESOURCE_GROUP: Final = "rg-docchat-dev"
SHARED_GROUP: Final = "rg-docchat-shared"
AZ_TIMEOUT_SECONDS: Final = 300.0
LOG_LIMIT: Final = timedelta(minutes=20)
LOG_POLL_SECONDS: Final = 60.0
CLOSE_LIMIT_SECONDS: Final = 300.0
CLOSE_POLL_SECONDS: Final = 15.0
RULE_WRITE: Final = "microsoft.sql/servers/firewallrules/write"
RULE_DELETE: Final = "microsoft.sql/servers/firewallrules/delete"
VAULT_WRITE: Final = "microsoft.keyvault/vaults/write"


def load_firewall_script() -> ModuleType:
    # scripts/deploy holds command-line scripts, not a package; the check reuses the script's
    # runner-address resolution and its reading of the shared allowlist.
    name = "docchat_deployed_runner_rules_firewall"
    spec = importlib.util.spec_from_file_location(name, FIREWALL_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FIREWALL: Final = load_firewall_script()


async def az_json(*args: str) -> Any:
    """One Azure CLI command as the job's identity, its JSON output parsed."""
    executable = shutil.which("az")
    assert executable is not None, "the Azure CLI isn't on PATH"
    process = await asyncio.create_subprocess_exec(
        executable, *args, "--output", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    async with asyncio.timeout(AZ_TIMEOUT_SECONDS):
        output, errors = await process.communicate()
    command = "az " + " ".join(word for word in args[:4] if not word.startswith("-"))
    last = (errors.decode(errors="replace").strip().splitlines() or ["no error output"])[-1]
    assert process.returncode == 0, f"{command} exited {process.returncode}: {last[:300]}"
    return json.loads(output) if output.strip() else None


def operation(event: Mapping[str, Any]) -> str:
    return str((event.get("operationName") or {}).get("value", "")).lower()


def status(event: Mapping[str, Any]) -> str:
    return str((event.get("status") or {}).get("value", ""))


def app_id(event: Mapping[str, Any]) -> str:
    return str((event.get("claims") or {}).get("appid", "")).lower()


def rule_events(events: list[Mapping[str, Any]], rule: str) -> list[Mapping[str, Any]]:
    """The succeeded create and delete events of one SQL firewall rule."""
    suffix = f"/firewallrules/{rule}".lower()
    return [
        event
        for event in events
        if str(event.get("resourceId", "")).lower().endswith(suffix)
        and operation(event) in {RULE_WRITE, RULE_DELETE}
        and status(event) == "Succeeded"
    ]


def rule_log_problems(events: list[Mapping[str, Any]], client_id: str) -> list[str]:
    problems: list[str] = []
    seen = {operation(event) for event in events}
    problems += [f"no {name} event" for name in (RULE_WRITE, RULE_DELETE) if name not in seen]
    problems += [
        f"{operation(event)} by {app_id(event) or 'an unknown caller'}"
        for event in events
        if app_id(event) != client_id.lower()
    ]
    return problems


def registry_problems(rules: list[str], runner: str, allowlist: frozenset[str] | None) -> list[str]:
    addresses = [FIREWALL.bare_address(rule) for rule in rules]
    problems: list[str] = []
    if runner in addresses:
        problems.append(f"the registry still admits this runner ({runner})")
    if allowlist is not None:
        problems += [
            f"the registry admits {address}, outside the shared allowlist"
            for address in addresses
            if address not in allowlist
        ]
    return problems


async def poll(read: Callable[[], Any], done: Callable[[Any], bool]) -> Any:
    """`read()` every 60 s until `done` holds, for at most 20 minutes."""
    deadline = asyncio.get_running_loop().time() + LOG_LIMIT.total_seconds()
    while True:
        value = await read()
        if done(value) or asyncio.get_running_loop().time() >= deadline:
            return value
        await asyncio.sleep(LOG_POLL_SECONDS)


@pytest.mark.timeout(int(LOG_LIMIT.total_seconds()) + 600)
async def test_t445_the_deploy_jobs_runner_rules_are_logged_and_gone(
    azd_environment: dict[str, str], azd_environment_name: str
) -> None:
    if azd_environment_name != "dev":
        pytest.skip("T-445 runs after each dev deploy")
    rule = f"ci-runner-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    client_id = os.environ["AZURE_CLIENT_ID"]
    server = azd_environment["DOCCHAT_SQL_SERVER_NAME"]

    async def read() -> list[Mapping[str, Any]]:
        events = await az_json(
            "monitor", "activity-log", "list", "--resource-group", RESOURCE_GROUP,
            "--offset", "3h", "--max-events", "5000",
        )  # fmt: skip
        return rule_events(events or [], rule)

    events = await poll(read, lambda found: not rule_log_problems(found, client_id))
    left = await az_json(
        "sql", "server", "firewall-rule", "list", "--server", server,
        "--resource-group", RESOURCE_GROUP,
    )  # fmt: skip
    registry = await az_json("acr", "list", "--resource-group", SHARED_GROUP, "--query", "[0].name")
    listing = await az_json("acr", "network-rule", "list", "--name", str(registry))
    runner = await asyncio.to_thread(FIREWALL.resolve_runner_address)

    assert rule_log_problems(events, client_id) == []
    assert [item["name"] for item in left or [] if str(item["name"]).startswith("ci-runner-")] == []
    rules = FIREWALL.ip_rule_values(listing)
    assert registry_problems(rules, runner, FIREWALL.known_shared_allowlist()) == []


# --- T-459 ---------------------------------------------------------------------------------------


async def vault_read(environment: DeployedEnvironment) -> bool:
    """One read of the `member` alias's secret: `True` when it succeeds, `False` when the vault
    refuses it with `403 ForbiddenByFirewall`."""
    try:
        await read_vault(environment.vault_uri, [environment.roles["member"]])
    except VaultForbiddenError:
        return False
    return True


def needs_stage_acceptance(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--stage-acceptance"):
        pytest.skip(
            "T-459 runs at the acceptance of the stage that provisions dev "
            "(84-testing.md section 8.4), with --stage-acceptance"
        )


@pytest.mark.full
async def test_t459_the_test_vault_answers_only_inside_the_read_window(
    request: pytest.FixtureRequest, deployed_environment: DeployedEnvironment
) -> None:
    needs_stage_acceptance(request)
    environment = deployed_environment
    if os.environ.get("DOCCHAT_CI_PRIVATE_NETWORKING") == "true":
        host = urlsplit(environment.vault_uri).hostname or ""
        resolved = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}
        assert addresses, f"{host} doesn't resolve"
        assert all(address.is_private for address in addresses), f"{host} resolves to {addresses}"
        assert await vault_read(environment), "the private endpoint refused the read"
        return

    before = await vault_read(environment)
    assert await run_firewall("open", environment.name) == 0, "firewall.py open failed"
    try:
        inside = await vault_read(environment)
        if not inside:
            # Another job's concurrent write can undo a rule (section 4.6 item 5).
            assert await run_firewall("open", environment.name) == 0
            inside = await vault_read(environment)
    finally:
        closed = await run_firewall("close", environment.name)
    after = inside
    deadline = asyncio.get_running_loop().time() + CLOSE_LIMIT_SECONDS
    while after and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(CLOSE_POLL_SECONDS)
        after = await vault_read(environment)

    assert before is False, "the vault answered before the window opened"
    assert inside is True, "the vault refused the read inside the window"
    assert closed == 0, "firewall.py close failed"
    assert after is False, "the vault still answered 300 s after the window closed"


def vault_write_problems(events: list[Mapping[str, Any]], client_id: str) -> list[str]:
    """Pipeline writes to the vault by anyone but `id-docchat-citest`, or too few of its own: a
    pipeline identity signs in as an application (`claims.idtyp` `app`)."""
    writes = [
        event
        for event in events
        if operation(event) == VAULT_WRITE and status(event) == "Succeeded"
    ]
    pipeline = [
        event for event in writes if str((event.get("claims") or {}).get("idtyp", "")) == "app"
    ]
    problems = [
        f"a vault write by the application {app_id(event)}"
        for event in pipeline
        if app_id(event) != client_id.lower()
    ]
    if sum(app_id(event) == client_id.lower() for event in pipeline) < 2:
        problems.append("fewer than the window's two writes under id-docchat-citest")
    return problems


@pytest.mark.full
async def test_t459_the_activity_log_holds_only_the_test_identitys_vault_writes(
    request: pytest.FixtureRequest,
) -> None:
    needs_stage_acceptance(request)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip(
            "person-run: the Activity Log and the shared allowlist are read by the stage's "
            "performer after the dev-tests job's window (84-testing.md section 8.4)"
        )
    vault = urlsplit(os.environ["DOCCHAT_TEST_VAULT_URI"]).hostname or ""
    name = vault.split(".", 1)[0]
    shown = await az_json("keyvault", "show", "--name", name, "--resource-group", SHARED_GROUP)
    started = datetime.now(UTC) - timedelta(hours=3)
    events = await az_json(
        "monitor", "activity-log", "list", "--resource-id", str(shown["id"]),
        "--start-time", started.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )  # fmt: skip
    allowlist = FIREWALL.known_shared_allowlist()
    assert allowlist is not None, "set DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST in this shell"
    rules = (shown.get("properties") or {}).get("networkAcls", {}).get("ipRules") or []
    outside = [
        FIREWALL.bare_address(str(rule.get("value")))
        for rule in rules
        if FIREWALL.bare_address(str(rule.get("value"))) not in allowlist
    ]

    assert vault_write_problems(events or [], os.environ["DOCCHAT_CI_TEST_CLIENT_ID"]) == []
    assert outside == [], f"the vault admits {outside}, outside the shared allowlist"
