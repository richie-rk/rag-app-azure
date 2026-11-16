"""Unit tests of `scripts/deploy/prod_checks.py` (`83-infrastructure.md` section 10.7 step 9)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
SUBSCRIPTION = "00000000-0000-4000-8000-000000000001"
WEB = "11111111-1111-4111-8111-111111111111"
API_SITE = "/subscriptions/s/resourceGroups/rg-docchat-prod/providers/Microsoft.Web/sites/app"


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checks = _load_script("scripts/deploy/prod_checks.py", "docchat_script_prod_checks")


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.values = {
            "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
            "DOCCHAT_RESOURCE_TOKEN": "abc123",
            "DOCCHAT_KEY_VAULT_NAME": "kv-docchat-prod-abc123",
            "TEST_CLIENT_ID": "",
            "WEB_CLIENT_ID": WEB,
        }
        self.graph = checks.CommandResult(
            1, "", 'ERROR: Not Found({"error":{"code":"Request_ResourceNotFound"}})'
        )
        self.allowed = [WEB]
        self.settings = [{"name": "DOCCHAT_AUTH_TEST_CLIENT_ID", "value": ""}]
        self.secret_pages: list[dict[str, Any]] = [{"value": []}]

    def __call__(self, args: Sequence[str]) -> Any:
        words = list(args)
        self.calls.append(words)
        joined = " ".join(words)
        if "graph.microsoft.com" in joined:
            assert words[words.index("--url") + 1] == (
                "https://graph.microsoft.com/v1.0/applications(uniqueName='docchat-prod-abc123-test')"
            )
            return self.graph
        if joined.startswith("azd env refresh"):
            assert words[3:] == ["-e", "prod", "--no-prompt"]
        routes: list[tuple[str, Callable[[], str]]] = [
            ("azd env refresh", lambda: ""),
            ("azd env get-values", lambda: json.dumps(self.values)),
            ("az resource list", lambda: json.dumps([API_SITE])),
            ("authsettingsV2", self._auth),
            ("az webapp config appsettings list", lambda: json.dumps(self.settings)),
            ("/secrets?api-version=2026-02-01", lambda: json.dumps(self.secret_pages.pop(0))),
        ]
        for marker, answer in routes:
            if joined.startswith(marker) or marker in joined:
                return checks.CommandResult(0, answer(), "")
        raise AssertionError(f"unexpected command {joined}")

    def _auth(self) -> str:
        policy = {"defaultAuthorizationPolicy": {"allowedApplications": self.allowed}}
        validation = {"azureActiveDirectory": {"validation": policy}}
        return json.dumps({"properties": {"identityProviders": validation}})


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> FakeCommands:
    fake = FakeCommands()
    monkeypatch.setattr(checks, "run_command", fake)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return fake


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    parsed: dict[str, str] = json.loads(capsys.readouterr().err.strip())
    return parsed


def test_prod_checks_pass_for_an_empty_vault_and_no_test_client(commands: FakeCommands) -> None:
    assert checks.main() == 0

    vault = next(call for call in commands.calls if "/secrets?" in " ".join(call))
    assert vault[vault.index("--url") + 1] == (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-prod"
        "/providers/Microsoft.KeyVault/vaults/kv-docchat-prod-abc123/secrets?api-version=2026-02-01"
    )


def test_the_content_share_secret_of_fallback_1_is_allowed(commands: FakeCommands) -> None:
    commands.secret_pages = [{"value": [{"name": "ingestion-content-share-connection"}]}]

    assert checks.main() == 0


def test_any_other_secret_fails_with_prod_vault_not_empty(
    commands: FakeCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    next_link = (
        "https://management.azure.com/subscriptions/s/resourceGroups/rg-docchat-prod/providers/"
        "Microsoft.KeyVault/vaults/kv/secrets?api-version=2026-02-01&$skiptoken=nextLink"
    )
    commands.secret_pages = [
        {"value": [{"name": "ingestion-content-share-connection"}], "nextLink": next_link},
        {"value": [{"name": "test-user-admin"}]},
    ]

    assert checks.main() == 2

    failure = _failure(capsys)
    assert failure["code"] == "prod_vault_not_empty"
    assert "test-user-admin" in failure["detail"]


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.values.update({"TEST_CLIENT_ID": "22222222-2222-4222-8222-222222222222"}),
        lambda c: setattr(c, "graph", checks.CommandResult(0, json.dumps({"id": "x"}), "")),
        lambda c: setattr(c, "graph", checks.CommandResult(1, "", "ERROR: Forbidden(...)")),
        lambda c: setattr(c, "allowed", [WEB, "22222222-2222-4222-8222-222222222222"]),
        lambda c: setattr(c, "settings", [{"name": "DOCCHAT_AUTH_TEST_CLIENT_ID", "value": "x"}]),
    ],
    ids=["output", "graph-finds-it", "graph-error", "authsettings", "app-setting"],
)
def test_a_test_client_anywhere_fails_with_prod_test_client_present(
    commands: FakeCommands, capsys: pytest.CaptureFixture[str], change: Any
) -> None:
    change(commands)

    assert checks.main() == 2

    assert _failure(capsys)["code"] == "prod_test_client_present"


def test_both_findings_are_reported_on_one_line(
    commands: FakeCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.values["TEST_CLIENT_ID"] = "22222222-2222-4222-8222-222222222222"
    commands.secret_pages = [{"value": [{"name": "stray"}]}]

    assert checks.main() == 2

    failure = _failure(capsys)
    assert failure["code"] == "prod_test_client_present"
    assert "prod_vault_not_empty: secrets: stray" in failure["detail"]
