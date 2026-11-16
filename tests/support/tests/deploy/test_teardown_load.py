"""Unit tests of `scripts/deploy/teardown_load.py` (`83-infrastructure.md` sections 7.6, 10.14)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
GRAPH = "https://graph.microsoft.com/v1.0"
ACR_PULL = "/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
NOT_FOUND = 'ERROR: Not Found({"error":{"code":"Request_ResourceNotFound"}})'


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


teardown = _load_script("scripts/deploy/teardown_load.py", "docchat_script_teardown_load")


class FakeAzure:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.resources = [
            "app-docchat-api-load-q1w2e3",
            "kv-docchat-load-q1w2e3",
            "vnet-docchat-load",
        ]
        self.resources_after_deploy: list[list[str]] = [[]]
        self.applications = {
            f"docchat-load-q1w2e3-{role}": {"id": f"app-{role}", "appId": f"appid-{role}"}
            for role in ("web", "api", "test")
        }
        self.graph_failures: set[str] = set()
        self.tags = {"docchat-load-run": "123", "docchat-load-created": "2026-10-30T01:00:00Z"}

    def __call__(self, args: Sequence[str]) -> Any:
        words = list(args)
        self.calls.append(words)
        joined = " ".join(words)
        if joined.startswith("az rest"):
            return self._graph(words[3], words[words.index("--url") + 1])
        return teardown.CommandResult(0, json.dumps(self._answer(joined, words)), "")

    def _answer(self, joined: str, words: list[str]) -> Any:
        if joined.startswith("az resource list"):
            return self.resources
        if joined.startswith("az identity list"):
            return [
                {"name": "id-docchat-api-load", "id": "p-api"},
                {"name": "id-docchat-ingest-load", "id": "p-ingest"},
                {"name": "id-docchat-stub-load", "id": "p-stub"},
            ]
        if joined.startswith("az role assignment list"):
            return [
                {"id": "/ra/api-pull", "principalId": "p-api", "role": ACR_PULL},
                {"id": "/ra/dev-pull", "principalId": "p-dev", "role": ACR_PULL},
                {"id": "/ra/api-reader", "principalId": "p-api", "role": "/x/acdd72a7"},
            ]
        if joined.startswith("az deployment group create"):
            self.resources = (
                self.resources_after_deploy.pop(0)
                if self.resources_after_deploy
                else self.resources
            )
            return None
        if joined.startswith("az group show"):
            return {"id": "/subscriptions/s/resourceGroups/rg-docchat-load", "tags": self.tags}
        if joined.startswith(("az role assignment delete", "az tag update")):
            return None
        raise AssertionError(f"unexpected command {joined}")

    def _graph(self, method: str, url: str) -> Any:
        result = teardown.CommandResult
        if url in self.graph_failures:
            return result(1, "", "ERROR: Forbidden(...)")
        if method == "get" and "applications(uniqueName=" in url:
            name = url.split("'")[1]
            app = self.applications.get(name)
            return result(0, json.dumps(app), "") if app else result(1, "", NOT_FOUND)
        if method == "get" and "servicePrincipals(appId=" in url:
            app_id = url.split("'")[1]
            return result(0, json.dumps({"id": f"sp-{app_id.removeprefix('appid-')}"}), "")
        if method == "delete":
            return result(0, "", "")
        raise AssertionError(f"unexpected Graph call {method} {url}")

    def graph_deletes(self) -> list[str]:
        return [
            c[c.index("--url") + 1]
            for c in self.calls
            if c[:4] == ["az", "rest", "--method", "delete"]
        ]


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> FakeAzure:
    fake = FakeAzure()
    monkeypatch.setattr(teardown, "run_command", fake)
    monkeypatch.setattr(teardown, "sleep", lambda seconds: None)
    monkeypatch.delenv("DOCCHAT_RESOURCE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return fake


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    parsed: dict[str, str] = json.loads(capsys.readouterr().err.strip())
    return parsed


def test_teardown_deletes_each_registration_object_by_object(azure: FakeAzure) -> None:
    assert teardown.main() == 0

    expected: list[str] = []
    for role in ("web", "api", "test"):
        expected += [f"{GRAPH}/servicePrincipals/sp-{role}", f"{GRAPH}/applications/app-{role}"]
    for role in ("web", "api", "test"):
        expected += [
            f"{GRAPH}/directory/deletedItems/sp-{role}",
            f"{GRAPH}/directory/deletedItems/app-{role}",
        ]
    assert azure.graph_deletes() == expected


def test_teardown_removes_only_the_run_identities_acr_pull_and_empties_the_group(
    azure: FakeAzure,
) -> None:
    assert teardown.main() == 0

    delete = next(c for c in azure.calls if c[:4] == ["az", "role", "assignment", "delete"])
    assert delete[delete.index("--ids") + 1 :] == ["/ra/api-pull", "--output", "json"]
    deploy = next(c for c in azure.calls if c[:4] == ["az", "deployment", "group", "create"])
    assert deploy[deploy.index("--mode") + 1] == "Complete"
    assert deploy[deploy.index("--template-file") + 1] == "infra/teardown/empty.bicep"
    tags = next(c for c in azure.calls if c[:3] == ["az", "tag", "update"])
    assert tags[tags.index("--operation") + 1] == "Delete"
    assert "docchat-load-run=123" in tags


def test_a_404_counts_as_already_deleted(azure: FakeAzure) -> None:
    azure.applications.pop("docchat-load-q1w2e3-test")

    assert teardown.main() == 0

    assert f"{GRAPH}/applications/app-test" not in azure.graph_deletes()


def test_a_graph_failure_finishes_the_other_steps_then_exits_2(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.graph_failures.add(f"{GRAPH}/applications/app-api")

    assert teardown.main() == 2

    failure = _failure(capsys)
    assert failure["code"] == "graph_delete_failed"
    assert "docchat-load-q1w2e3-api" in failure["detail"]
    assert f"{GRAPH}/applications/app-test" in azure.graph_deletes()
    assert any(c[:3] == ["az", "tag", "update"] for c in azure.calls)


def test_resources_left_after_three_repeats_fail_with_teardown_incomplete(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.resources_after_deploy = []

    assert teardown.main() == 2

    failure = _failure(capsys)
    assert failure["code"] == "teardown_incomplete"
    assert "vnet-docchat-load" in failure["detail"]
    deployments = [c for c in azure.calls if c[:4] == ["az", "deployment", "group", "create"]]
    assert len(deployments) == 4
    # The run tags stay, so the nightly guard tries again.
    assert not any(c[:3] == ["az", "tag", "update"] for c in azure.calls)


def test_the_token_comes_from_the_azd_output_when_the_group_is_already_empty(
    monkeypatch: pytest.MonkeyPatch, azure: FakeAzure
) -> None:
    azure.resources = []
    monkeypatch.setenv("DOCCHAT_RESOURCE_TOKEN", "q1w2e3")

    assert teardown.main() == 0

    assert f"{GRAPH}/applications/app-web" in azure.graph_deletes()
