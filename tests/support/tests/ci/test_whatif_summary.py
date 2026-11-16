"""`whatif_summary.py` on recorded what-if results (83-infrastructure.md section 10.5 step 4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import whatif_summary  # noqa: E402

GROUP = "/subscriptions/0000/resourceGroups/rg-docchat-dev/providers"

# The shape of `az deployment group what-if --no-pretty-print`.
RESULT: dict[str, Any] = {
    "status": "Succeeded",
    "error": None,
    "changes": [
        {
            "resourceId": f"{GROUP}/Microsoft.Web/sites/app-docchat-api-dev-x",
            "changeType": "NoChange",
        },
        {
            "resourceId": f"{GROUP}/Microsoft.Web/sites/app-docchat-api-dev-x",
            "changeType": "Modify",
            "delta": [
                {
                    "path": "properties.siteConfig",
                    "propertyChangeType": "Modify",
                    "children": [
                        {"path": "minTlsVersion", "propertyChangeType": "Modify"},
                        {"path": "ftpsState", "propertyChangeType": "NoEffect"},
                    ],
                },
                {"path": "tags.docchat-release-sha", "propertyChangeType": "Modify"},
            ],
        },
        {
            "resourceId": f"{GROUP}/Microsoft.Storage/storageAccounts/stdocchatdevx/blobServices/"
            "default/containers/renditions",
            "changeType": "Create",
        },
        {"resourceId": f"{GROUP}/Microsoft.Insights/actionGroups/old", "changeType": "Delete"},
        {
            "resourceId": "/providers/Microsoft.Graph/applications/docchat-dev-x-web",
            "changeType": "Unsupported",
            "unsupportedReason": "Changes to the resource type 'Microsoft.Graph/applications' "
            "can't be predicted.",
        },
        {"resourceId": f"{GROUP}/Microsoft.Web/sites/ignored", "changeType": "Ignore"},
    ],
}


def write(tmp_path: Path, name: str, content: Any) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_the_change_list_names_every_changed_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert whatif_summary.main([str(write(tmp_path, "whatif-dev.json", RESULT))]) == 0
    text = summary.read_text(encoding="utf-8")
    assert "### What-if: whatif-dev" in text
    assert "Create: 1, Modify: 1, Delete: 1, Unsupported: 1, NoChange: 1, Ignore: 1" in text
    assert (
        "| Create | Microsoft.Storage/storageAccounts/blobServices/containers | "
        "stdocchatdevx/default/renditions |  |"
    ) in text
    assert (
        "| Modify | Microsoft.Web/sites | app-docchat-api-dev-x | "
        "properties.siteConfig.minTlsVersion, tags.docchat-release-sha |"
    ) in text
    assert "| Delete | Microsoft.Insights/actionGroups | old |" in text
    assert "| Unsupported | Microsoft.Graph/applications | docchat-dev-x-web |" in text
    assert "rely on code-owner review" in text
    assert "ignored" not in text


def test_each_file_gets_its_own_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    empty = write(tmp_path, "whatif-shared.json", {"status": "Succeeded", "changes": []})
    nested = write(tmp_path, "whatif-dev.json", {"properties": RESULT})

    assert whatif_summary.main([str(nested), str(empty)]) == 0
    output = capsys.readouterr().out
    assert "### What-if: whatif-shared\n\nNo resource changes." in output
    assert "| Delete | Microsoft.Insights/actionGroups | old |" in output


def test_a_failed_what_if_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    failed = {"status": "Failed", "error": {"code": "InvalidTemplate"}, "changes": []}

    assert whatif_summary.main([str(write(tmp_path, "whatif.json", failed))]) == 1
    assert "What-if failed: `InvalidTemplate`." in capsys.readouterr().out


@pytest.mark.parametrize("content", ["not json", '{"changes": {}}'])
def test_an_unreadable_result_fails(tmp_path: Path, content: str) -> None:
    path = tmp_path / "whatif.json"
    path.write_text(content, encoding="utf-8")

    assert whatif_summary.main([str(path)]) == 1


def test_pipes_in_values_cannot_break_the_table() -> None:
    change = {
        "resourceId": f"{GROUP}/Microsoft.Web/sites/x",
        "changeType": "Unsupported",
        "unsupportedReason": "a | b",
    }
    markdown, _ = whatif_summary.summarise("x", {"changes": [change]})

    assert "a \\| b" in markdown
