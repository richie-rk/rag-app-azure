"""`prod` post-deploy checks (`83-infrastructure.md` section 10.7 step 9; T-452).

Runs as the `prod` deploy identity in every `full` and `images-only` run, after the release is
deployed. It refreshes the azd outputs, then fails with

- `prod_test_client_present` when `TEST_CLIENT_ID` isn't empty, when Microsoft Graph finds the
  application `docchat-prod-<token>-test` (anything but `404`), when the API's `authsettingsV2`
  allows any application other than the web client, or when the API carries a test-client
  setting;
- `prod_vault_not_empty` when the Key Vault's management-plane secret list names any secret
  other than `ingestion-content-share-connection` (content-share fallback 1).

The release stays deployed; the run fails for an operator to investigate.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ENV = "prod"
GRAPH = "https://graph.microsoft.com/v1.0"
ARM = "https://management.azure.com"
KEY_VAULT_API = "2026-02-01"
SITES_API = "2025-03-01"
CONTENT_SHARE_ENTRY = "ingestion-content-share-connection"
TEST_CLIENT_SETTING = "DOCCHAT_AUTH_TEST_CLIENT_ID"
COMMAND_TIMEOUT_SECONDS = 300.0

MESSAGES = {
    "prod_test_client_present": (
        "Production has a test client, or its Key Vault holds a secret it shouldn't; the release "
        "is deployed but the run fails for an operator to investigate."
    ),
    "prod_vault_not_empty": (
        "Production has a test client, or its Key Vault holds a secret it shouldn't; the release "
        "is deployed but the run fails for an operator to investigate."
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CheckError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def run_command(args: Sequence[str]) -> CommandResult:
    executable = shutil.which(args[0]) or args[0]
    try:
        # Fixed command words plus values read from the azd outputs; no shell is involved.
        completed = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(127, "", f"{args[0]}: {type(error).__name__}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def report_failure(code: str, detail: str) -> None:
    line = json.dumps({"code": code, "message": MESSAGES[code], "detail": detail})
    sys.stderr.write(line + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the failure; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def last_line(text: str) -> str:
    return (text.strip().splitlines() or ["no error output"])[-1][:300]


def command_json(code: str, args: Sequence[str]) -> Any:
    result = run_command(args)
    if result.returncode != 0:
        raise CheckError(code, f"{' '.join(args[:4])} failed: {last_line(result.stderr)}")
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError as error:
        raise CheckError(code, f"{' '.join(args[:4])} answered no JSON") from error


def azd_values() -> dict[str, str]:
    code = "prod_test_client_present"
    refresh = run_command(["azd", "env", "refresh", "-e", ENV, "--no-prompt"])
    if refresh.returncode != 0:
        raise CheckError(code, f"azd env refresh failed: {last_line(refresh.stderr)}")
    values = command_json(code, ["azd", "env", "get-values", "-e", ENV, "--output", "json"])
    if not isinstance(values, dict):
        raise CheckError(code, "azd env get-values answered no object")
    return {str(key): str(value) for key, value in values.items()}


def api_site(code: str) -> str:
    ids = command_json(
        code,
        ["az", "resource", "list", "--resource-group", f"rg-docchat-{ENV}",
         "--tag", "azd-service-name=api", "--query", "[].id", "--output", "json"],
    )  # fmt: skip
    if not ids:
        raise CheckError(code, "no site tagged azd-service-name=api")
    return str(ids[0])


def test_client_findings(values: dict[str, str]) -> list[str]:
    findings: list[str] = []
    if values.get("TEST_CLIENT_ID", ""):
        findings.append("TEST_CLIENT_ID is set")
    token = values.get("DOCCHAT_RESOURCE_TOKEN", "")
    if not re.fullmatch(r"[a-z0-9]{6}", token):
        findings.append("DOCCHAT_RESOURCE_TOKEN isn't a resource token")
    else:
        url = f"{GRAPH}/applications(uniqueName='docchat-{ENV}-{token}-test')"
        result = run_command(["az", "rest", "--method", "get", "--url", url, "--output", "json"])
        # The deploy identity owns every registration it could have created, so it would see one.
        if result.returncode == 0:
            findings.append(f"Graph finds docchat-{ENV}-{token}-test")
        elif not re.search(r"\bNot Found\b", result.stderr):
            findings.append(f"the Graph lookup failed: {last_line(result.stderr)}")
    return findings


def platform_auth_findings(values: dict[str, str]) -> list[str]:
    code = "prod_test_client_present"
    site = api_site(code)
    auth = command_json(
        code,
        ["az", "rest", "--method", "get", "--url",
         f"{ARM}{site}/config/authsettingsV2?api-version={SITES_API}", "--output", "json"],
    )  # fmt: skip
    policy = (
        (auth or {})
        .get("properties", {})
        .get("identityProviders", {})
        .get("azureActiveDirectory", {})
        .get("validation", {})
        .get("defaultAuthorizationPolicy", {})
    )
    allowed = policy.get("allowedApplications") or []
    findings: list[str] = []
    if sorted(str(app) for app in allowed) != [values.get("WEB_CLIENT_ID", "")]:
        findings.append(f"authsettingsV2 allows {len(allowed)} applications, not the web client")
    settings = command_json(
        code, ["az", "webapp", "config", "appsettings", "list", "--ids", site, "--output", "json"]
    )
    if any(s.get("name") == TEST_CLIENT_SETTING and s.get("value") for s in settings or []):
        findings.append(f"the API sets {TEST_CLIENT_SETTING}")
    return findings


def vault_secret_names(values: dict[str, str]) -> list[str]:
    code = "prod_vault_not_empty"
    subscription = values.get("AZURE_SUBSCRIPTION_ID", "")
    vault = values.get("DOCCHAT_KEY_VAULT_NAME", "")
    if not subscription or not vault:
        raise CheckError(code, "AZURE_SUBSCRIPTION_ID or DOCCHAT_KEY_VAULT_NAME is missing")
    # Through Resource Manager and Contributor, so the vault's firewall and data roles don't apply.
    url = (
        f"{ARM}/subscriptions/{subscription}/resourceGroups/rg-docchat-{ENV}/providers/"
        f"Microsoft.KeyVault/vaults/{vault}/secrets?api-version={KEY_VAULT_API}"
    )
    names: list[str] = []
    while url:
        page = command_json(code, ["az", "rest", "--method", "get", "--url", url])
        names += [str(item.get("name")) for item in (page or {}).get("value", [])]
        next_link = (page or {}).get("nextLink")
        url = next_link if isinstance(next_link, str) and next_link.startswith(ARM) else ""
    return names


def main() -> int:
    failures: list[tuple[str, str]] = []
    try:
        values = azd_values()
    except CheckError as error:
        report_failure(error.code, error.detail)
        return 2
    try:
        findings = test_client_findings(values) + platform_auth_findings(values)
    except CheckError as error:
        findings = [error.detail]
    if findings:
        failures.append(("prod_test_client_present", "; ".join(findings)))
    try:
        unexpected = [name for name in vault_secret_names(values) if name != CONTENT_SHARE_ENTRY]
    except CheckError as error:
        failures.append((error.code, error.detail))
    else:
        if unexpected:
            # Secret names only; a value is never read.
            failures.append(("prod_vault_not_empty", "secrets: " + ", ".join(sorted(unexpected))))
    if not failures:
        sys.stdout.write(json.dumps({"prod_checks": "pass"}) + "\n")
        return 0
    code = failures[0][0]
    report_failure(code, " | ".join(f"{c}: {detail}" for c, detail in failures))
    return 2


if __name__ == "__main__":
    sys.exit(main())
