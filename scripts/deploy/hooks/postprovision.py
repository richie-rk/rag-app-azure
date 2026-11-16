"""The azd `postprovision` hook: migrations, grants and the capacity check (section 10.9).

Runs under the deploy identity after every provision and before any app deployment:

1. `firewall.py sweep --target sql`, then `open --target sql` (section 4.6);
2. waits for a TDS login, retrying errors 40613 and 40615, for at most 5 minutes;
3. the Alembic migrations; 4. `sql_principals.py --env <env>`;
5. in `dev` and `prod`, `docchat-ops --env <env> chat capacity-check` (a failure stops `prod`);
6. in `finally`, `firewall.py close --target sql`.

`sql_principals.py` and the operator CLI run as their own processes, so this hook imports
neither the SQL engine nor the CLI.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_DIR = REPO_ROOT / "scripts" / "deploy"
ENVIRONMENTS = ("dev", "prod", "load")
SQL_SCOPE = "https://database.windows.net/.default"
TRANSIENT_LOGIN_ERRORS = (40613, 40615)
LOGIN_WAIT_SECONDS = 300.0
LOGIN_FIRST_DELAY_SECONDS = 5.0
LOGIN_FAST_RETRIES = 12
LOGIN_MAX_DELAY_SECONDS = 30.0
CAPACITY_EXCEEDED_EXIT = 2
COMMAND_TIMEOUT_SECONDS = 3600.0

MESSAGES = {
    "sql_unreachable": "The database didn't accept a connection in time.",
    "migration_failed": "A migration or a principal grant failed; nothing was deployed.",
    "sql_principal_failed": "A migration or a principal grant failed; nothing was deployed.",
    "chat_capacity_exceeded": (
        "One user's worst-case model use would exceed a quarter of a production chat "
        "deployment's capacity; raise the capacity or lower the per-user limits, then deploy "
        "once more."
    ),
    "chat_capacity_check_failed": (
        "The chat capacity check couldn't run, so the release wasn't deployed."
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class HookError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _load_firewall() -> ModuleType:
    """`firewall.py` holds the standard-library TDS 8.0 login both steps 1 and 2 need."""
    name = "docchat_deploy_firewall"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, DEPLOY_DIR / "firewall.py")
    if spec is None or spec.loader is None:
        raise ImportError("scripts/deploy/firewall.py can't be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


firewall = _load_firewall()


# --- seams to the outside world -------------------------------------------------------------


def run_command(args: Sequence[str], *, capture: bool = False) -> CommandResult:
    executable = shutil.which(args[0]) or args[0]
    try:
        # Fixed command words plus the validated environment name; no shell is involved.
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


def sql_login(server: str, database: str, token: str) -> int | None:
    result: int | None = firewall.tds_login(server, database, token)
    return result


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def monotonic() -> float:
    return time.monotonic()


# --- reporting ------------------------------------------------------------------------------


def append_summary(text: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the result; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def report_failure(code: str, detail: str) -> None:
    line = json.dumps({"code": code, "message": MESSAGES[code], "detail": detail})
    sys.stderr.write(line + "\n")
    append_summary(line)


# --- the steps ------------------------------------------------------------------------------


def firewall_step(command: str, env: str) -> int:
    script = DEPLOY_DIR / "firewall.py"
    args = [sys.executable, str(script), command, "--target", "sql", "--env", env]
    return run_command(args).returncode


def access_token() -> str:
    result = run_command(
        ["az", "account", "get-access-token", "--scope", SQL_SCOPE, "--query", "accessToken",
         "--output", "tsv"],
        capture=True,
    )  # fmt: skip
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise HookError("sql_unreachable", "az account get-access-token failed")
    return token


def login_delay(attempt: int) -> float:
    """Every 5 s for the first minute, then doubling up to 30 s."""
    doublings = max(0, attempt - LOGIN_FAST_RETRIES)
    return min(LOGIN_MAX_DELAY_SECONDS, LOGIN_FIRST_DELAY_SECONDS * (1 << doublings))


def wait_for_login(server: str, database: str) -> None:
    token = access_token()
    started = monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            error_number = sql_login(server, database, token)
        except OSError as error:
            last = type(error).__name__
        else:
            if error_number is None:
                return
            if error_number not in TRANSIENT_LOGIN_ERRORS:
                raise HookError("sql_unreachable", f"the login failed with error {error_number}")
            last = f"error {error_number}"
        delay = login_delay(attempt)
        if monotonic() - started + delay > LOGIN_WAIT_SECONDS:
            raise HookError("sql_unreachable", f"no login within 5 minutes; last: {last}")
        sleep(delay)


def openai_enabled(env: str) -> bool:
    path = REPO_ROOT / "infra" / "env" / f"{env}.json"
    config: Any = json.loads(path.read_text(encoding="utf-8"))
    return bool(config["openAi"]["enabled"])


def run_migrations() -> None:
    result = run_command(
        ["uv", "run", "--frozen", "--package", "docchat-db", "alembic", "-c", "db/alembic.ini",
         "upgrade", "head"]
    )  # fmt: skip
    if result.returncode != 0:
        raise HookError("migration_failed", f"alembic upgrade head exited {result.returncode}")


def run_sql_principals(env: str) -> None:
    script = DEPLOY_DIR / "sql_principals.py"
    result = run_command([sys.executable, str(script), "--env", env])
    if result.returncode != 0:
        raise HookError("sql_principal_failed", f"sql_principals.py exited {result.returncode}")


def capacity_check(env: str) -> None:
    try:
        enabled = openai_enabled(env)
    except (OSError, ValueError, KeyError, TypeError) as error:
        if env == "prod":
            detail = f"infra/env/{env}.json can't be read: {type(error).__name__}"
            raise HookError("chat_capacity_check_failed", detail) from error
        enabled = env != "load"
    if not enabled:
        return
    result = run_command(
        ["uv", "run", "--frozen", "docchat-ops", "--env", env, "chat", "capacity-check"],
        capture=True,
    )
    output = (result.stdout + result.stderr).strip()
    sys.stdout.write(output + "\n")
    append_summary(f"### Chat capacity check ({env})\n\n```text\n{output}\n```")
    # `dev` only reports; `prod` deploys nothing while the capacity invariant is unproven.
    if env != "prod" or result.returncode == 0:
        return
    if result.returncode == CAPACITY_EXCEEDED_EXIT:
        raise HookError("chat_capacity_exceeded", "a share is above 25%")
    raise HookError("chat_capacity_check_failed", f"capacity-check exited {result.returncode}")


def sql_target() -> tuple[str, str]:
    server = os.environ.get("DOCCHAT_SQL_SERVER", "")
    database = os.environ.get("DOCCHAT_SQL_DATABASE", "")
    if not server.endswith(".database.windows.net") or not database:
        raise HookError("sql_unreachable", "DOCCHAT_SQL_SERVER or DOCCHAT_SQL_DATABASE is unset")
    return server, database


def open_sql_rule(env: str) -> None:
    for command in ("sweep", "open"):
        status = firewall_step(command, env)
        if status != 0:
            raise HookError("sql_unreachable", f"firewall.py {command} exited {status}")


def main() -> int:
    env = os.environ.get("AZURE_ENV_NAME", "")
    if env not in ENVIRONMENTS:
        report_failure("sql_unreachable", "AZURE_ENV_NAME isn't dev, prod or load")
        return 2
    try:
        server, database = sql_target()
        open_sql_rule(env)
    except HookError as error:
        report_failure(error.code, error.detail)
        return 2
    failure: HookError | None = None
    try:
        wait_for_login(server, database)
        run_migrations()
        run_sql_principals(env)
        capacity_check(env)
    except HookError as error:
        failure = error
    finally:
        close_status = firewall_step("close", env)
    if failure is not None:
        report_failure(failure.code, failure.detail)
        return 2
    # firewall.py has already reported its own rule_close_failed.
    return close_status


if __name__ == "__main__":
    sys.exit(main())
