"""Constants and helpers shared by the compiled-template tests and their fixtures."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arm_template import Deployment, Resource

ENVIRONMENTS = ("dev", "prod", "load")
REPO_ROOT = Path(__file__).resolve().parents[3]
INFRA = REPO_ROOT / "infra"

# Distinct stand-ins for the bootstrap-written group object IDs, so each assignment's principal
# is identifiable while the committed files still hold the nil UUID.
GROUP_IDS = {
    "operators": "10000000-0000-0000-0000-000000000001",
    "sqlAdmins": "10000000-0000-0000-0000-000000000002",
    "users": "10000000-0000-0000-0000-000000000003",
    "admins": "10000000-0000-0000-0000-000000000004",
    "configOperators": "10000000-0000-0000-0000-000000000005",
    "conversationReaders": "10000000-0000-0000-0000-000000000006",
    "userDataOperators": "10000000-0000-0000-0000-000000000007",
}

SECURITY_WORKSPACE_ID = (
    "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-docchat-identity"
    "/providers/Microsoft.OperationalInsights/workspaces/log-docchat-security"
)

MainFactory = Callable[..., Deployment]
SharedFactory = Callable[..., Deployment]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def env_file(environment: str) -> dict[str, Any]:
    content: dict[str, Any] = load_json(INFRA / "env" / f"{environment}.json")
    return content


def with_config(
    template: dict[str, Any], environment: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """A copy of main.json with `changes` merged into its embedded file for `environment`."""
    patched = copy.deepcopy(template)
    address_space = env_file(environment)["network"]["addressSpace"]
    embedded = [
        value
        for value in patched["variables"].values()
        if isinstance(value, dict) and value.get("network", {}).get("addressSpace") == address_space
    ]
    if len(embedded) != 1:
        raise AssertionError(f"main.json should embed infra/env/{environment}.json once")
    embedded[0].update(copy.deepcopy(changes))
    return patched


def avm_call(deployment: Deployment, name: str) -> dict[str, Any]:
    """The parameters of the one Azure Verified Module call deployed under `name`."""
    calls = [call for call in deployment.external_calls() if call.name == name]
    if len(calls) != 1:
        raise AssertionError(f"{len(calls)} module calls named {name}")
    return calls[0].module_parameters


def avm_calls(deployment: Deployment, prefix: str) -> list[Resource]:
    return [call for call in deployment.external_calls() if str(call.name).startswith(prefix)]


def of_type(deployment: Deployment, resource_type: str) -> list[Resource]:
    return [
        resource for resource in deployment.walk() if resource.type.lower() == resource_type.lower()
    ]
