"""T-422: `check_bicep_modules.py` fails both seeded violations (83-infrastructure.md 8.6).

The fixture has the shape Bicep CLI 0.47.16 compiles (`languageVersion` 2.0, modules inlined as
`Microsoft.Resources/deployments` with their template under `properties.template`).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_bicep_modules  # noqa: E402

PREVIEW = "2024-11-01-preview"
AVM_MODULE: dict[str, Any] = {
    "type": "Microsoft.Resources/deployments",
    "apiVersion": "2025-04-01",
    "name": "kv-deploy",
    "properties": {
        "expressionEvaluationOptions": {"scope": "inner"},
        "mode": "Incremental",
        "parameters": {
            "name": {"value": "[parameters('config').name]"},
            "enableTelemetry": {"value": False},
        },
        "template": {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {
                "name": {"type": "string"},
                "enableTelemetry": {"type": "bool", "defaultValue": True},
            },
            "resources": [
                {
                    "condition": "[parameters('enableTelemetry')]",
                    "type": "Microsoft.Resources/deployments",
                    "apiVersion": "2024-03-01",
                    "name": "[format('46d3xbcp.res.keyvault-vault.{0}', '0-14-2')]",
                    "properties": {
                        "mode": "Incremental",
                        "template": {"contentVersion": "1.0.0.0", "resources": []},
                    },
                },
                {
                    "type": "Microsoft.KeyVault/vaults",
                    "apiVersion": "2026-02-01",
                    "name": "[parameters('name')]",
                },
                {
                    "type": "Microsoft.Insights/diagnosticSettings",
                    "apiVersion": "2021-05-01-preview",
                    "scope": "[resourceId('Microsoft.KeyVault/vaults', parameters('name'))]",
                    "name": "diag",
                },
            ],
        },
    },
}

LOCAL_MODULE: dict[str, Any] = {
    "type": "Microsoft.Resources/deployments",
    "apiVersion": "2025-04-01",
    "name": "cosmos",
    "properties": {
        "mode": "Incremental",
        "parameters": {},
        "template": {
            "contentVersion": "1.0.0.0",
            "parameters": {},
            "resources": {
                "account": {
                    "type": "Microsoft.DocumentDB/databaseAccounts",
                    "apiVersion": "2026-03-15",
                    "name": "cosmos-x",
                }
            },
        },
    },
}

MAIN: dict[str, Any] = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "languageVersion": "2.0",
    "contentVersion": "1.0.0.0",
    "resources": {
        "existingSite": {
            "existing": True,
            "type": "Microsoft.Web/sites",
            "apiVersion": "2025-03-01",
            "name": "app-x",
        },
        "kv": AVM_MODULE,
        "cosmos": LOCAL_MODULE,
    },
    "outputs": {"host": {"type": "string", "value": "[reference('existingSite').defaultHostName]"}},
}


def run(tmp_path: Path, main: dict[str, Any], shared: dict[str, Any] | None = None) -> int:
    main_path = tmp_path / "main.json"
    shared_path = tmp_path / "shared.json"
    main_path.write_text(json.dumps(main), encoding="utf-8")
    shared_path.write_text(json.dumps(shared or {"resources": []}), encoding="utf-8")
    return check_bicep_modules.main([str(main_path), str(shared_path)])


def test_t422_compiled_templates_within_both_rules_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, MAIN) == 0
    assert "every AVM call sets enableTelemetry: false" in capsys.readouterr().out


def test_t422_a_seeded_preview_api_version_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main = copy.deepcopy(MAIN)
    resources = main["resources"]["cosmos"]["properties"]["template"]["resources"]
    resources["account"]["apiVersion"] = "2026-04-01-preview"

    assert run(tmp_path, main) == 1
    assert (
        "main.json/cosmos/account: Microsoft.DocumentDB/databaseAccounts@2026-04-01-preview "
        "is a preview API version"
    ) in capsys.readouterr().out


def test_t422_a_seeded_avm_call_without_enable_telemetry_false_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main = copy.deepcopy(MAIN)
    del main["resources"]["kv"]["properties"]["parameters"]["enableTelemetry"]

    assert run(tmp_path, main) == 1
    assert "main.json/kv: Azure Verified Module call without enableTelemetry: false" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("value", [True, "[parameters('telemetry')]"])
def test_enable_telemetry_must_be_the_literal_false(tmp_path: Path, value: object) -> None:
    main = copy.deepcopy(MAIN)
    main["resources"]["kv"]["properties"]["parameters"]["enableTelemetry"] = {"value": value}

    assert run(tmp_path, main) == 1


def test_a_preview_api_version_inside_an_expression_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = {
        "resources": [],
        "outputs": {
            "key": {
                "type": "string",
                "value": f"[reference(resourceId('Microsoft.Web/sites', 'x'), '{PREVIEW}')]",
            }
        },
    }

    assert run(tmp_path, MAIN, shared) == 1
    assert "shared.json: an expression uses the preview API version 2024-11-01-preview" in (
        capsys.readouterr().out
    )


def test_diagnostic_settings_on_their_registered_preview_pass_in_an_expression(
    tmp_path: Path,
) -> None:
    shared = {
        "resources": [],
        "variables": {
            "diag": "[reference(resourceId('Microsoft.Insights/diagnosticSettings', 'd'), "
            "'2021-05-01-preview')]"
        },
    }

    assert run(tmp_path, MAIN, shared) == 0


def test_another_preview_of_diagnostic_settings_fails(tmp_path: Path) -> None:
    main = copy.deepcopy(MAIN)
    resources = main["resources"]["kv"]["properties"]["template"]["resources"]
    resources[2]["apiVersion"] = "2017-05-01-preview"

    assert run(tmp_path, main) == 1


def test_an_unreadable_template_fails(tmp_path: Path) -> None:
    assert check_bicep_modules.main([str(tmp_path / "absent.json")]) == 1
