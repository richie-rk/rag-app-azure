"""The deployed checks after each provision (`84-testing.md` section 8.3): the resource naming
check T-437 and the budget persistence check T-472.

The `deploy` job of `deploy-environment.yaml` runs each alone after `azd provision`, in `dev`,
`prod` and `load`, as the environment's deploy identity, with `AZURE_ENV_NAME` naming the
environment and `.azure/<env>/.env` refreshed:
`uv run pytest --deployed tests/deployed/test_provision_checks.py::<test>`. They carry no suite
marker, so no suite selects them. What they compare is `docchat_testing.provision_checks`'s.

T-472's "before and after a second provision": the `preprovision` hook read the budget's start
date before this provision and wrote it to `DOCCHAT_BUDGET_START_DATE` (the first day of the
month when no budget existed yet), so the budget read after the provision must still carry it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from docchat_testing.provision_checks import budget_problems, expected_names, naming_problems

pytestmark = pytest.mark.deployed

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
ARM: Final = "https://management.azure.com"
BUDGETS_API: Final = "2026-06-01"
AZ_TIMEOUT_SECONDS: Final = 300.0


def environment_file(environment: str) -> dict[str, Any]:
    path = REPOSITORY_ROOT / "infra" / "env" / f"{environment}.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


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
    command = "az " + " ".join(word for word in args[:3] if not word.startswith("-"))
    last = (errors.decode(errors="replace").strip().splitlines() or ["no error output"])[-1]
    assert process.returncode == 0, f"{command} exited {process.returncode}: {last[:300]}"
    return json.loads(output) if output.strip() else None


async def test_t437_every_resource_is_named_as_its_pattern_says(
    azd_environment: dict[str, str], azd_environment_name: str
) -> None:
    environment = azd_environment_name
    token = azd_environment["DOCCHAT_RESOURCE_TOKEN"]
    listed = await az_json(
        "resource", "list", "--resource-group", f"rg-docchat-{environment}",
        "--query", "[].{name: name, type: type}",
    )  # fmt: skip
    expected = expected_names(environment, token, environment_file(environment))

    assert naming_problems(listed or [], expected) == []


async def test_t472_a_provision_keeps_the_budget_and_its_notifications(
    azd_environment: dict[str, str], azd_environment_name: str
) -> None:
    environment = azd_environment_name
    if not environment_file(environment)["budget"]["enabled"]:
        pytest.skip(f"budget.enabled is false in infra/env/{environment}.json")
    subscription = azd_environment["AZURE_SUBSCRIPTION_ID"]
    group = f"/subscriptions/{subscription}/resourceGroups/rg-docchat-{environment}"
    budget_id = f"{group}/providers/Microsoft.Consumption/budgets/budget-docchat-{environment}"
    budget = await az_json(
        "rest", "--method", "get", "--url", f"{ARM}{budget_id}?api-version={BUDGETS_API}"
    )
    action_group = f"{group}/providers/microsoft.insights/actionGroups/ag-docchat-{environment}-ops"

    problems = budget_problems(
        budget or {},
        start_date=azd_environment["DOCCHAT_BUDGET_START_DATE"],
        action_group_id=action_group,
    )

    assert problems == []
