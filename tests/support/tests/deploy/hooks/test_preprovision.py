"""Unit tests of the azd `preprovision` hook (T-461, `83-infrastructure.md` section 9.2)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[5]
SHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OLD = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
REGISTRY = "crdocchatabc123.azurecr.io"
API = f"{REGISTRY}/docchat/api@sha256:{SHA}"
INGESTION = f"{REGISTRY}/docchat/ingestion@sha256:{SHA}"
STUB = f"{REGISTRY}/docchat/model-stub@sha256:{SHA}"
SUBSCRIPTION = "00000000-0000-4000-8000-000000000001"


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_script("scripts/deploy/hooks/preprovision.py", "docchat_script_preprovision")


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.sites: dict[str, str] = {}
        self.stub_site: str | None = None
        self.live: dict[str, str] = {}
        self.preflight_status = 0
        self.budget: Any = hook.CommandResult(
            0, json.dumps({"properties": {"timePeriod": {"startDate": "2026-03-01T00:00:00Z"}}}), ""
        )
        self.env_set: dict[str, str] = {}

    def __call__(self, args: Sequence[str], *, capture: bool = True) -> Any:
        words = list(args)
        self.calls.append(words)
        ok = hook.CommandResult
        if words[0] == sys.executable:
            assert words[1].endswith("scripts/preflight/preflight.py")
            assert not capture
            return ok(self.preflight_status, "", "")
        joined = " ".join(words)
        if joined.startswith("az resource list") and "--tag" in words:
            service = words[words.index("--tag") + 1].removeprefix("azd-service-name=")
            found = [self.sites[service]] if service in self.sites else []
            return ok(0, json.dumps(found), "")
        if joined.startswith("az resource list"):
            assert "starts_with(name, 'app-docchat-stub-load-')" in joined
            return ok(0, json.dumps([self.stub_site] if self.stub_site else []), "")
        if joined.startswith("az webapp config show"):
            return ok(0, self.live[words[words.index("--ids") + 1]] + "\n", "")
        if joined.startswith("az rest --method get"):
            return self.budget
        if joined.startswith("azd env set"):
            self.env_set[words[3]] = words[4]
            return ok(0, "", "")
        raise AssertionError(f"unexpected command {joined}")


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> FakeCommands:
    fake = FakeCommands()
    monkeypatch.setattr(hook, "run_command", fake)
    monkeypatch.setattr(hook, "today", lambda: date(2026, 10, 30))
    for name, value in {
        "AZURE_ENV_NAME": "dev",
        "AZURE_RESOURCE_GROUP": "rg-docchat-dev",
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "DOCCHAT_API_IMAGE": API,
        "DOCCHAT_INGESTION_IMAGE": INGESTION,
        "DOCCHAT_MODEL_STUB_IMAGE": "",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DOCCHAT_PREFLIGHT_DONE", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return fake


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    parsed: dict[str, str] = json.loads(capsys.readouterr().err.strip())
    return parsed


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("AZURE_ENV_NAME", "staging", "env_name_invalid"),
        ("AZURE_RESOURCE_GROUP", "rg-docchat-prod", "resource_group_mismatch"),
        ("DOCCHAT_API_IMAGE", f"{REGISTRY}/docchat/api:{SHA[:40]}", "image_reference_invalid"),
        (
            "DOCCHAT_INGESTION_IMAGE",
            f"{REGISTRY}/docchat/ingestion:latest",
            "image_reference_invalid",
        ),
        ("DOCCHAT_API_IMAGE", f"docker.io/docchat/api@sha256:{SHA}", "image_reference_invalid"),
        (
            "DOCCHAT_MODEL_STUB_IMAGE",
            f"{REGISTRY}/docchat/model-stub:v1",
            "image_reference_invalid",
        ),
    ],
    ids=["env", "group", "api-tag", "ingestion-latest", "other-registry", "stub-tag"],
)
def test_t461_preprovision_rejects_bad_names_and_tag_only_references(
    *,
    monkeypatch: pytest.MonkeyPatch,
    commands: FakeCommands,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
    code: str,
) -> None:
    monkeypatch.setenv(name, value)

    assert hook.main() == 2

    assert _failure(capsys)["code"] == code
    assert commands.calls == []


def test_t461_preprovision_keeps_the_running_images_and_the_budget_start_date(
    commands: FakeCommands,
) -> None:
    commands.sites = {"api": "/sites/app-docchat-api-dev-abc123", "ingestion": "/sites/func"}
    commands.live = {
        "/sites/app-docchat-api-dev-abc123": f"DOCKER|{REGISTRY}/docchat/api@sha256:{OLD}",
        "/sites/func": f"DOCKER|{REGISTRY}/docchat/ingestion@sha256:{OLD}",
    }

    assert hook.main() == 0

    assert commands.env_set == {
        "DOCCHAT_API_IMAGE_CURRENT": f"{REGISTRY}/docchat/api@sha256:{OLD}",
        "DOCCHAT_INGESTION_IMAGE_CURRENT": f"{REGISTRY}/docchat/ingestion@sha256:{OLD}",
        "DOCCHAT_MODEL_STUB_IMAGE_CURRENT": "",
        "DOCCHAT_BUDGET_START_DATE": "2026-03-01",
    }
    env_set = [call for call in commands.calls if call[:3] == ["azd", "env", "set"]]
    assert all(call[-2:] == ["--environment", "dev"] for call in env_set)
    budget = next(call for call in commands.calls if call[:2] == ["az", "rest"])
    assert budget[budget.index("--url") + 1] == (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/"
        "rg-docchat-dev/providers/Microsoft.Consumption/budgets/budget-docchat-dev"
        "?api-version=2026-06-01"
    )


def test_t461_preprovision_uses_the_release_for_sites_that_dont_exist_yet(
    commands: FakeCommands,
) -> None:
    commands.budget = hook.CommandResult(
        1, "", 'ERROR: Not Found({"error":{"code":"NotFound","message":"Budget not found"}})'
    )

    assert hook.main() == 0

    assert commands.env_set["DOCCHAT_API_IMAGE_CURRENT"] == API
    assert commands.env_set["DOCCHAT_INGESTION_IMAGE_CURRENT"] == INGESTION
    assert commands.env_set["DOCCHAT_BUDGET_START_DATE"] == "2026-10-01"


def test_preprovision_replaces_a_live_value_that_is_no_digest_reference(
    commands: FakeCommands,
) -> None:
    commands.sites = {"api": "/sites/api"}
    commands.live = {"/sites/api": ""}

    assert hook.main() == 0

    assert commands.env_set["DOCCHAT_API_IMAGE_CURRENT"] == API


def test_preprovision_reads_the_model_stub_in_load(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands
) -> None:
    monkeypatch.setenv("AZURE_ENV_NAME", "load")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-docchat-load")
    monkeypatch.setenv("DOCCHAT_MODEL_STUB_IMAGE", STUB)
    commands.stub_site = "/sites/app-docchat-stub-load-abc123"
    commands.live = {commands.stub_site: f"DOCKER|{REGISTRY}/docchat/model-stub@sha256:{OLD}"}

    assert hook.main() == 0

    assert commands.env_set["DOCCHAT_MODEL_STUB_IMAGE_CURRENT"] == (
        f"{REGISTRY}/docchat/model-stub@sha256:{OLD}"
    )


def test_preprovision_needs_the_model_stub_reference_in_load(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AZURE_ENV_NAME", "load")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-docchat-load")

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "image_reference_invalid"


def test_t461_preprovision_fails_on_a_budget_error_other_than_404(
    commands: FakeCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.budget = hook.CommandResult(1, "", "ERROR: (AuthorizationFailed) no access")

    assert hook.main() == 2

    failure = _failure(capsys)
    assert failure["code"] == "budget_read_failed"
    assert failure["message"] == hook.MESSAGES["budget_read_failed"]
    assert commands.env_set == {}


def test_preprovision_runs_the_preflight_unless_it_is_done(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands
) -> None:
    assert hook.main() == 0
    assert [call[2:] for call in commands.calls if call[0] == sys.executable] == [["--env", "dev"]]

    commands.calls.clear()
    monkeypatch.setenv("DOCCHAT_PREFLIGHT_DONE", "true")
    assert hook.main() == 0
    assert not any(call[0] == sys.executable for call in commands.calls)


def test_preprovision_stops_on_a_failed_preflight(commands: FakeCommands) -> None:
    commands.preflight_status = 2

    assert hook.main() == 2

    assert commands.env_set == {}
    assert len(commands.calls) == 1
