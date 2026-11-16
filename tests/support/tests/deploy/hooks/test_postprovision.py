"""Unit tests of the azd `postprovision` hook (T-461, `83-infrastructure.md` section 10.9)."""

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

REPO_ROOT = Path(__file__).resolve().parents[5]
SERVER = "sql-docchat-dev-abc123.database.windows.net"


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_script("scripts/deploy/hooks/postprovision.py", "docchat_script_postprovision")


class FakeCommands:
    """Answers by step; `steps` records them in the order the hook ran them."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.status: dict[str, int] = {}
        self.capacity_output = "chat share 12.0% of 500 units; chat-small share 11.3% of 530 units"

    def __call__(self, args: Sequence[str], *, capture: bool = False) -> Any:
        words = list(args)
        step = self._step(words)
        self.steps.append(step)
        stdout = ""
        if step == "token":
            stdout = "header.payload.signature\n"
        elif step == "capacity-check":
            stdout = self.capacity_output + "\n"
        return hook.CommandResult(self.status.get(step, 0), stdout, "")

    @staticmethod
    def _step(words: list[str]) -> str:
        joined = " ".join(words)
        if words[0] == sys.executable and words[1].endswith("scripts/deploy/firewall.py"):
            assert words[3:] == ["--target", "sql", "--env", words[-1]]
            return f"firewall {words[2]}"
        if words[0] == sys.executable and words[1].endswith("scripts/deploy/sql_principals.py"):
            assert words[2:4] == ["--env", words[3]]
            return "sql_principals"
        if joined.startswith("az account get-access-token"):
            assert words[words.index("--scope") + 1] == "https://database.windows.net/.default"
            return "token"
        if joined == "uv run --frozen --package docchat-db alembic -c db/alembic.ini upgrade head":
            return "migrations"
        if (
            joined.startswith("uv run --frozen docchat-ops --env ")
            and "chat capacity-check" in joined
        ):
            return "capacity-check"
        raise AssertionError(f"unexpected command {joined}")


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCommands:
    fake = FakeCommands()
    monkeypatch.setattr(hook, "run_command", fake)
    monkeypatch.setattr(hook, "sql_login", lambda server, database, token: None)
    monkeypatch.setattr(hook, "sleep", lambda seconds: None)
    monkeypatch.setattr(hook, "REPO_ROOT", tmp_path)
    (tmp_path / "infra" / "env").mkdir(parents=True)
    for env, enabled in (("dev", True), ("prod", True), ("load", False)):
        config = {"openAi": {"enabled": enabled}, "modelStub": {"enabled": not enabled}}
        (tmp_path / "infra" / "env" / f"{env}.json").write_text(json.dumps(config))
    monkeypatch.setenv("AZURE_ENV_NAME", "dev")
    monkeypatch.setenv("DOCCHAT_SQL_SERVER", SERVER)
    monkeypatch.setenv("DOCCHAT_SQL_DATABASE", "docchat")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return fake


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert len(lines) == 1
    parsed: dict[str, str] = json.loads(lines[0])
    return parsed


FULL_RUN = [
    "firewall sweep",
    "firewall open",
    "token",
    "migrations",
    "sql_principals",
    "capacity-check",
    "firewall close",
]


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_t461_postprovision_runs_migrations_grants_and_the_capacity_check_in_order(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands, tmp_path: Path, env: str
) -> None:
    monkeypatch.setenv("AZURE_ENV_NAME", env)

    assert hook.main() == 0

    assert commands.steps == FULL_RUN
    assert commands.capacity_output in (tmp_path / "summary.md").read_text()


def test_t461_postprovision_skips_the_capacity_check_in_load(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands
) -> None:
    monkeypatch.setenv("AZURE_ENV_NAME", "load")

    assert hook.main() == 0

    assert "capacity-check" not in commands.steps
    assert commands.steps[-1] == "firewall close"


@pytest.mark.parametrize(
    ("status", "code"), [(2, "chat_capacity_exceeded"), (7, "chat_capacity_check_failed")]
)
def test_t461_postprovision_maps_the_prod_capacity_exit(
    monkeypatch: pytest.MonkeyPatch,
    commands: FakeCommands,
    capsys: pytest.CaptureFixture[str],
    status: int,
    code: str,
) -> None:
    monkeypatch.setenv("AZURE_ENV_NAME", "prod")
    commands.status["capacity-check"] = status

    assert hook.main() == 2

    assert _failure(capsys)["code"] == code
    assert commands.steps[-1] == "firewall close"


@pytest.mark.parametrize("status", [2, 7])
def test_t461_postprovision_only_reports_the_capacity_check_in_dev(
    commands: FakeCommands, status: int
) -> None:
    commands.status["capacity-check"] = status

    assert hook.main() == 0


@pytest.mark.parametrize(
    ("step", "code", "ran"),
    [
        (
            "migrations",
            "migration_failed",
            ["firewall sweep", "firewall open", "token", "migrations"],
        ),
        (
            "sql_principals",
            "sql_principal_failed",
            ["firewall sweep", "firewall open", "token", "migrations", "sql_principals"],
        ),
    ],
)
def test_postprovision_stops_at_a_failed_step_and_closes_the_rule(
    commands: FakeCommands,
    capsys: pytest.CaptureFixture[str],
    step: str,
    code: str,
    ran: list[str],
) -> None:
    commands.status[step] = 1

    assert hook.main() == 2

    assert _failure(capsys)["code"] == code
    assert commands.steps == [*ran, "firewall close"]


@pytest.mark.parametrize("step", ["firewall sweep", "firewall open"])
def test_postprovision_without_a_rule_is_sql_unreachable(
    commands: FakeCommands, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    commands.status[step] = 3

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "sql_unreachable"
    assert "migrations" not in commands.steps


def test_postprovision_returns_the_close_failure(commands: FakeCommands) -> None:
    commands.status["firewall close"] = 4

    assert hook.main() == 4


def test_postprovision_waits_through_40613_and_40615(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands
) -> None:
    answers: list[Any] = [40615, 40613, ConnectionResetError(), None]
    logins: list[tuple[str, str, str]] = []
    sleeps: list[float] = []

    def login(server: str, database: str, token: str) -> int | None:
        logins.append((server, database, token))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        result: int | None = answer
        return result

    monkeypatch.setattr(hook, "sql_login", login)
    monkeypatch.setattr(hook, "sleep", sleeps.append)

    assert hook.main() == 0

    assert logins[0] == (SERVER, "docchat", "header.payload.signature")
    assert sleeps == [5.0, 5.0, 5.0]


def test_postprovision_gives_up_after_5_minutes(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(hook, "sql_login", lambda server, database, token: 40613)
    monkeypatch.setattr(hook, "sleep", advance)
    monkeypatch.setattr(hook, "monotonic", lambda: clock["now"])

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "sql_unreachable"
    assert sleeps[:12] == [5.0] * 12
    assert sleeps[12:15] == [10.0, 20.0, 30.0]
    assert max(sleeps) == 30.0
    assert clock["now"] <= 300.0
    assert "migrations" not in commands.steps
    assert commands.steps[-1] == "firewall close"


def test_postprovision_fails_at_once_on_another_login_error(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hook, "sql_login", lambda server, database, token: 18456)

    assert hook.main() == 2

    failure = _failure(capsys)
    assert failure["code"] == "sql_unreachable"
    assert "18456" in failure["detail"]
