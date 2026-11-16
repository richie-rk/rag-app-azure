"""T-125: the typed environment files (83-infrastructure.md section 8.2; 81-config.md section 4.2).

All three files build, and each negative fixture fails `bicep build-params` whichever environment
the build targets, because main.bicep type-checks every file against the sealed environmentConfig.
The negative cases run the Bicep CLI the infra-tests job installed (`az bicep install` puts it in
~/.azure/bin) on a copy of infra/ with one file changed.

The shipped values: the five deployments of 83-infrastructure.md section 5.3 in dev and prod, the
same model objects in load, and the three model alert thresholds of section 8.3.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from arm_template import load_deployment
from compiled_support import ENVIRONMENTS, INFRA, REPO_ROOT, env_file, load_json

API_IMAGE = "crdocchat000000.azurecr.io/docchat/api@sha256:" + "0" * 64
IMAGE = "crdocchat000000.azurecr.io/docchat/ingestion@sha256:" + "0" * 64


def _bicep() -> str:
    found = shutil.which("bicep")
    if found is not None:
        return found
    installed = Path.home() / ".azure" / "bin" / "bicep"
    if installed.is_file():
        return str(installed)
    pytest.fail("no Bicep CLI: install it with az bicep install --version v0.47.16")


def _add_top_level(config: dict[str, Any]) -> None:
    config["unexpectedField"] = True


def _remove_member(config: dict[str, Any]) -> None:
    del config["budget"]


def _mistype(config: dict[str, Any]) -> None:
    config["ingestion"]["alwaysReady"] = "1"


def _add_nested(config: dict[str, Any]) -> None:
    config["openAi"]["deployments"]["chat"]["unexpectedField"] = "x"


FIXTURES: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "undeclared member": (_add_top_level, "BCP037"),
    "removed member": (_remove_member, "BCP035"),
    "mistyped field": (_mistype, "BCP036"),
    "extra nested field": (_add_nested, "BCP037"),
}


def _build_params(infra: Path, environment: str) -> subprocess.CompletedProcess[str]:
    environment_variables = {
        **os.environ,
        "AZURE_ENV_NAME": environment,
        "DOCCHAT_API_IMAGE_CURRENT": API_IMAGE,
        "DOCCHAT_INGESTION_IMAGE_CURRENT": IMAGE,
        "DOCCHAT_BUDGET_START_DATE": "2026-10-01",
    }
    return subprocess.run(  # noqa: S603 - fixed arguments, the Bicep CLI on a temporary copy
        [_bicep(), "build-params", str(infra / "main.bicepparam"), "--stdout"],
        capture_output=True,
        text=True,
        env=environment_variables,
        check=False,
        timeout=240,
    )


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t125_every_environment_file_builds(
    compiled_dir: Path, main_template: dict[str, Any], environment: str
) -> None:
    parameters = load_json(compiled_dir / f"main.{environment}.parameters.json")["parameters"]
    assert parameters["environmentName"]["value"] == environment
    values = {key: entry["value"] for key, entry in parameters.items()}
    deployment = load_deployment(main_template, values, resource_group=f"rg-docchat-{environment}")
    # main.json embeds all three files, whichever environment the build targets.
    assert set(deployment.variable("envConfigs")) == set(ENVIRONMENTS)
    assert deployment.variable("config") == env_file(environment)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("kind", sorted(FIXTURES))
def test_t125_negative_fixture_fails_the_build(tmp_path: Path, kind: str, environment: str) -> None:
    infra = tmp_path / "infra"
    shutil.copytree(INFRA, infra)
    # The changed file is prod.json in every case, so dev and load show that one bad file fails
    # every build, not only its own environment's.
    changed = env_file("prod")
    mutate, diagnostic = FIXTURES[kind]
    mutate(changed)
    (infra / "env" / "prod.json").write_text(json.dumps(changed, indent=2) + "\n", encoding="utf-8")
    result = _build_params(infra, environment)
    assert result.returncode != 0
    assert diagnostic in result.stdout + result.stderr


def _section_5_3() -> dict[str, dict[str, Any]]:
    """83-infrastructure.md section 5.3's table: each deployment's defaults by name."""
    text = (REPO_ROOT / "docs" / "spec" / "83-infrastructure.md").read_text(encoding="utf-8")
    section = text.split("### 5.3 Default deployments", 1)[1].split("\n### ", 1)[0]
    table: dict[str, dict[str, Any]] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 9 or not re.fullmatch(r"\d+ / \d+", cells[8]):
            continue
        name, model, version, kind, upgrade, policy, encoding = (
            None if cell == "none" else re.findall(r"`([^`]+)`", cell)[0] for cell in cells[1:8]
        )
        dev, prod = (int(value) for value in cells[8].split(" / "))
        table[str(name)] = {
            "model": model,
            "version": version,
            "type": kind,
            "versionUpgradeOption": upgrade,
            "raiPolicyName": policy,
            "encoding": encoding,
            "capacity": {"dev": dev, "prod": prod},
        }
    return table


def _deployments(config: dict[str, Any], environment: str) -> dict[str, dict[str, Any]]:
    """The deployments an environment file declares, in section 5.3's terms."""
    open_ai = config["openAi"]
    found: dict[str, dict[str, Any]] = {}
    for entry in open_ai["deployments"].values():
        found[entry["name"]] = {
            "model": entry["model"],
            "version": entry["version"],
            "type": entry["type"],
            "versionUpgradeOption": entry["versionUpgradeOption"],
            "raiPolicyName": entry["raiPolicyName"],
            "encoding": entry["encoding"],
            "capacity": {environment: entry["capacity"]},
        }
    for model in open_ai["embeddingModels"]:
        for prefix, type_key, capacity_key in (
            ("embed-query", "queryType", "queryCapacity"),
            ("embed-ingest", "ingestionType", "ingestionCapacity"),
        ):
            found[f"{prefix}-{model['key']}"] = {
                "model": model["model"],
                "version": model["version"],
                "type": model[type_key],
                # Embedding deployments never upgrade in place and name no policy (section 5.2).
                "versionUpgradeOption": "NoAutoUpgrade",
                "raiPolicyName": None,
                "encoding": model["encoding"],
                "capacity": {environment: model[capacity_key]},
            }
    return found


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_the_deployments_equal_section_5_3(environment: str) -> None:
    expected = {
        name: {**row, "capacity": {environment: row["capacity"][environment]}}
        for name, row in _section_5_3().items()
    }
    assert len(expected) == 5
    assert _deployments(env_file(environment), environment) == expected


def test_load_carries_the_model_objects_of_prod() -> None:
    # load calls the model stub: Bicep reads only names, models, versions and encodings there.
    load, prod = env_file("load")["openAi"], env_file("prod")["openAi"]
    assert (load["enabled"], prod["enabled"]) == (False, True)
    assert load["deployments"] == prod["deployments"]
    assert load["embeddingModels"] == prod["embeddingModels"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_the_model_alert_thresholds(environment: str) -> None:
    alerts = env_file(environment)["alerts"]
    assert alerts["enabled"] is (environment != "load")
    assert (alerts["ttftP95Ms"], alerts["openAiFirstChunkP95Ms"], alerts["openAiCallMeanMs"]) == (
        20000,
        10000,
        8000,
    )


# Section 8.3's shipped values, dev / prod / load, for every field the bootstrap doesn't write
# and section 5.3 doesn't own; prod's column is the release's (90-build-order.md section 3.7).
SHIPPED: dict[str, tuple[Any, Any, Any]] = {
    "location": ("eastus2", "eastus2", "eastus2"),
    "staticWebAppLocation": ("eastus2", "eastus2", "eastus2"),
    "network.addressSpace": ("10.61.0.0/22", "10.62.0.0/22", "10.63.0.0/22"),
    "webCustomDomains": ([], [], []),
    "web.sku": ("Free", "Standard", "Free"),
    "api.sku": ("B1", "P1v3", "P1v3"),
    "api.minInstances": (1, 2, 1),
    "api.maxInstances": (1, 6, 6),
    "api.zoneRedundant": (False, False, False),
    "ingestion.sku": ("EP1", "EP2", "EP2"),
    "ingestion.alwaysReady": (1, 1, 2),
    "ingestion.maxBurst": (2, 4, 4),
    "sql.sku": ("GP_S_Gen5_2", "GP_Gen5_2", "GP_Gen5_2"),
    "sql.minCapacity": (0.5, None, None),
    "sql.autoPauseDelayMinutes": (15, -1, -1),
    "sql.maxSizeGb": (32, 64, 32),
    "sql.zoneRedundant": (False, False, False),
    "sql.backupRedundancy": ("Local", "Geo", "Local"),
    "sql.pitrDays": (7, 14, 7),
    "cosmos.serverless": (True, False, False),
    "cosmos.backupTier": ("Continuous7Days", "Continuous30Days", "Continuous7Days"),
    "cosmos.maxThroughput": (
        None,
        {"conversations": 4000, "memories": 1000, "usage": 1000},
        {"conversations": 30000, "memories": 30000, "usage": 30000},
    ),
    "conversationRetentionDays": (0, 0, 0),
    "memoryRetentionDays": (0, 0, 0),
    "cosmosGroupDataAccess": (True, True, True),
    "search.sku": ("basic", "standard", "standard"),
    "search.replicas": (1, 2, 2),
    "search.partitions": (1, 1, 1),
    "openAi.enabled": (True, True, False),
    "modelStub.enabled": (False, False, True),
    "storage.malwareScanning": (False, True, False),
    "storage.malwareScanCapGbPerMonth": (1000, 1000, 1000),
    "keyVault.purgeProtection": (False, True, False),
    "keyVault.softDeleteDays": (7, 90, 7),
    "monitoring.retentionDays": (30, 90, 30),
    "monitoring.dailyCapGb": (2, -1, -1),
    "telemetrySamplingRatio": (1.0, 1.0, 1.0),
    "budget.enabled": (True, True, True),
    "budget.amount": (650, 2200, 400),
}


def _field(config: dict[str, Any], path: str) -> Any:
    value: Any = config
    for key in path.split("."):
        value = value[key]
    return value


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_the_shipped_values_equal_section_8_3(environment: str) -> None:
    column = ENVIRONMENTS.index(environment)
    config = env_file(environment)
    assert {path: _field(config, path) for path in SHIPPED} == {
        path: values[column] for path, values in SHIPPED.items()
    }
    # Exactly one model source in every environment (section 2.21).
    assert config["openAi"]["enabled"] != config["modelStub"]["enabled"]
