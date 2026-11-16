"""The azd `preprovision` hook (`83-infrastructure.md` section 9.2).

1. `AZURE_ENV_NAME` is `dev`, `prod` or `load` and `AZURE_RESOURCE_GROUP` is `rg-docchat-<env>`.
2. Every image reference is a digest reference in the shared registry.
3. The preflight runs unless `DOCCHAT_PREFLIGHT_DONE` is `true`.
4. Each site's live image goes to `DOCCHAT_*_IMAGE_CURRENT`, so provisioning never changes the
   running image; a site that doesn't exist yet takes the new release's reference.
5. The budget's start date goes to `DOCCHAT_BUDGET_START_DATE`, so a provision never moves it.

Each failure exits 2 and nothing is provisioned.
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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENTS = ("dev", "prod", "load")
IMAGE_REFERENCE = re.compile(r"^[a-z0-9.-]+\.azurecr\.io/docchat/[a-z-]+@sha256:[0-9a-f]{64}$")
BUDGET_API = "2026-06-01"
BUDGET_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
COMMAND_TIMEOUT_SECONDS = 600.0
# (azd service name, release reference, current reference); the model stub only in `load`.
SERVICE_IMAGES = (
    ("api", "DOCCHAT_API_IMAGE", "DOCCHAT_API_IMAGE_CURRENT"),
    ("ingestion", "DOCCHAT_INGESTION_IMAGE", "DOCCHAT_INGESTION_IMAGE_CURRENT"),
)
STUB_IMAGE = ("DOCCHAT_MODEL_STUB_IMAGE", "DOCCHAT_MODEL_STUB_IMAGE_CURRENT")
# The stub is no azd service, so it carries no azd-service-name tag; its name is fixed.
STUB_SITE_PREFIX = "app-docchat-stub-load-"

MESSAGES = {
    "env_name_invalid": (
        "The environment name, resource group or an image reference is not in the required form."
    ),
    "resource_group_mismatch": (
        "The environment name, resource group or an image reference is not in the required form."
    ),
    "image_reference_invalid": (
        "The environment name, resource group or an image reference is not in the required form."
    ),
    "budget_read_failed": (
        "The existing budget's start date couldn't be read; nothing was provisioned."
    ),
}
UNCODED_MESSAGE = "A deployment command failed; nothing was provisioned."


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class HookError(Exception):
    def __init__(self, code: str | None, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def run_command(args: Sequence[str], *, capture: bool = True) -> CommandResult:
    executable = shutil.which(args[0]) or args[0]
    try:
        # Fixed command words plus values this hook validated; no shell is involved.
        completed = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=capture,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(127, "", f"{args[0]}: {type(error).__name__}")
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def today() -> date:
    return datetime.now(UTC).date()


def report_failure(code: str | None, detail: str) -> None:
    message = MESSAGES.get(code, UNCODED_MESSAGE) if code else UNCODED_MESSAGE
    line = json.dumps({"code": code, "message": message, "detail": detail})
    sys.stderr.write(line + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the failure; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def last_line(text: str) -> str:
    return (text.strip().splitlines() or ["no error output"])[-1][:300]


def az(*args: str) -> str:
    result = run_command(["az", *args])
    if result.returncode != 0:
        raise HookError(None, f"az {' '.join(args[:3])} failed: {last_line(result.stderr)}")
    return result.stdout


def az_json(*args: str) -> Any:
    output = az(*args, "--output", "json")
    return json.loads(output) if output.strip() else None


# --- the steps ------------------------------------------------------------------------------


def check_names() -> str:
    env = os.environ.get("AZURE_ENV_NAME", "")
    if env not in ENVIRONMENTS:
        raise HookError("env_name_invalid", f"AZURE_ENV_NAME {env[:20]!r} isn't dev, prod or load")
    group = os.environ.get("AZURE_RESOURCE_GROUP", "")
    if group != f"rg-docchat-{env}":
        raise HookError("resource_group_mismatch", f"AZURE_RESOURCE_GROUP isn't rg-docchat-{env}")
    return env


def release_images(env: str) -> dict[str, str]:
    """The release's references by variable name; the stub's only in `load`."""
    names = [release for _, release, _ in SERVICE_IMAGES]
    if env == "load" or os.environ.get(STUB_IMAGE[0]):
        names.append(STUB_IMAGE[0])
    images: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name, "")
        if not IMAGE_REFERENCE.fullmatch(value):
            # A tag can be re-pointed; only a digest names exactly the bytes that were scanned.
            raise HookError("image_reference_invalid", f"{name} isn't a digest reference")
        images[name] = value
    return images


def run_preflight(env: str) -> int:
    if os.environ.get("DOCCHAT_PREFLIGHT_DONE") == "true":
        return 0
    script = REPO_ROOT / "scripts" / "preflight" / "preflight.py"
    return run_command([sys.executable, str(script), "--env", env], capture=False).returncode


def find_site(env: str, service: str) -> str | None:
    ids = az_json(
        "resource", "list", "--resource-group", f"rg-docchat-{env}",
        "--tag", f"azd-service-name={service}", "--query", "[].id",
    )  # fmt: skip
    return str(ids[0]) if ids else None


def find_stub_site(env: str) -> str | None:
    ids = az_json(
        "resource", "list", "--resource-group", f"rg-docchat-{env}",
        "--resource-type", "Microsoft.Web/sites",
        "--query", f"[?starts_with(name, '{STUB_SITE_PREFIX}')].id",
    )  # fmt: skip
    return str(ids[0]) if ids else None


def live_image(site_id: str) -> str | None:
    value = az("webapp", "config", "show", "--ids", site_id, "--query", "linuxFxVersion")
    reference = value.strip().strip('"').removeprefix("DOCKER|")
    # Anything but a digest reference (a site a failed first provision left half-made) runs
    # no release, so the new release's reference takes its place.
    return reference if IMAGE_REFERENCE.fullmatch(reference) else None


def current_images(env: str, images: dict[str, str]) -> dict[str, str]:
    current: dict[str, str] = {}
    for service, release, target in SERVICE_IMAGES:
        site = find_site(env, service)
        current[target] = (live_image(site) if site else None) or images[release]
    stub_release, stub_target = STUB_IMAGE
    if env == "load":
        site = find_stub_site(env)
        current[stub_target] = (live_image(site) if site else None) or images[stub_release]
    else:
        current[stub_target] = ""
    return current


def budget_start_date(env: str) -> str:
    subscription = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", subscription):
        raise HookError("budget_read_failed", "AZURE_SUBSCRIPTION_ID isn't a subscription ID")
    url = (
        f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/"
        f"rg-docchat-{env}/providers/Microsoft.Consumption/budgets/budget-docchat-{env}"
        f"?api-version={BUDGET_API}"
    )
    result = run_command(["az", "rest", "--method", "get", "--url", url, "--output", "json"])
    if result.returncode != 0:
        if re.search(r"\bNot Found\b", result.stderr):
            return today().replace(day=1).isoformat()
        raise HookError("budget_read_failed", last_line(result.stderr))
    try:
        start = str(json.loads(result.stdout)["properties"]["timePeriod"]["startDate"])[:10]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise HookError("budget_read_failed", "the budget has no timePeriod.startDate") from error
    if not BUDGET_DATE.fullmatch(start):
        raise HookError("budget_read_failed", "the budget's start date isn't yyyy-MM-dd")
    return start


def azd_env_set(env: str, name: str, value: str) -> None:
    result = run_command(["azd", "env", "set", name, value, "--environment", env])
    if result.returncode != 0:
        raise HookError(None, f"azd env set {name} failed: {last_line(result.stderr)}")


def main() -> int:
    try:
        env = check_names()
        images = release_images(env)
        if run_preflight(env) != 0:
            # The preflight printed its own report and failure line.
            return 2
        values = current_images(env, images)
        values["DOCCHAT_BUDGET_START_DATE"] = budget_start_date(env)
        for name, value in values.items():
            azd_env_set(env, name, value)
    except HookError as error:
        report_failure(error.code, error.detail)
        return 2
    sys.stdout.write(json.dumps({"hook": "preprovision", "env": env, **values}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
