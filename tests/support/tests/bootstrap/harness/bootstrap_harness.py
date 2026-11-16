"""Helpers of the bootstrap script tests (83-infrastructure.md sections 10.4, 11 and 12.3).

The bootstrap tests' conftest.py puts scripts/bootstrap and this directory on sys.path. The
scripts are imported by module name through importlib, so the type checker, which doesn't see
scripts/bootstrap, treats them as plain modules. Every `az` and `gh` call goes through
`FakeRunner`, which answers only the calls a test recorded and fails the test on any other.
"""

from __future__ import annotations

import importlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

bootstrap = importlib.import_module("bootstrap")

GRAPH = "https://graph.microsoft.com/v1.0"


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


TENANT = uid(1)
SUBSCRIPTION = uid(2)
OPERATOR = uid(3)
APPROVERS = {"dev": [uid(11), uid(12)], "prod": [uid(13), uid(14)], "load": [uid(15), uid(16)]}
GROUP_OWNERS = [uid(21), uid(22)]
ORGANIZATION = "contoso"
REPOSITORY = "docchat"


def config_data(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tenantId": TENANT,
        "subscriptions": {"dev": SUBSCRIPTION, "prod": SUBSCRIPTION, "load": SUBSCRIPTION},
        "location": "eastus2",
        "github": {"organization": ORGANIZATION, "repository": REPOSITORY},
        "ciPrivateNetworking": False,
        "ciNetworkLocation": "eastus2",
        "operators": [OPERATOR],
        "approverObjectIds": {env: list(ids) for env, ids in APPROVERS.items()},
        "groupOwners": list(GROUP_OWNERS),
        "securityAlertEmails": ["docchat-security@example.com"],
        "testUserAliases": ["admin", "member"],
        "allowedRoleAssigneePrincipalIds": [],
        "templateCosmosAssignmentIds": [],
    }
    data.update(changes)
    return data


@dataclass
class Call:
    args: tuple[str, ...]
    env: dict[str, str] | None
    stdin: str | None


Answer = Any


class FakeRunner:
    """Replays recorded answers by exact argument list; the last answer for a call repeats."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, ...], list[Answer]] = {}
        self.prefixes: list[tuple[tuple[str, ...], Callable[[Call], Any]]] = []
        self.calls: list[Call] = []

    def answer(
        self, args: Sequence[str], stdout: str = "", *, returncode: int = 0, stderr: str = ""
    ) -> None:
        result = bootstrap.CommandResult(returncode, stdout, stderr)
        self.answers.setdefault(tuple(args), []).append(result)

    def answer_json(self, args: Sequence[str], payload: Any) -> None:
        self.answer(args, json.dumps(payload))

    def answer_with(self, args: Sequence[str], respond: Callable[[Call], Any]) -> None:
        self.answers.setdefault(tuple(args), []).append(respond)

    def answer_prefix(self, prefix: Sequence[str], respond: Callable[[Call], Any]) -> None:
        """Answers every call that starts with `prefix` and has no exact answer."""
        self.prefixes.append((tuple(prefix), respond))

    def __call__(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> Any:
        call = Call(tuple(args), dict(env) if env else None, stdin)
        self.calls.append(call)
        queue = self.answers.get(call.args)
        if not queue:
            for prefix, respond in self.prefixes:
                if call.args[: len(prefix)] == prefix:
                    return respond(call)
            raise AssertionError(f"unexpected call: {' '.join(call.args)}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return answer(call) if callable(answer) else answer

    def called(self, *prefix: str) -> list[Call]:
        return [call for call in self.calls if call.args[: len(prefix)] == prefix]


class Harness:
    """A temporary repository, a fake runner and captured output for one script run."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.runner = FakeRunner()
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def runtime(self) -> Any:
        return bootstrap.Runtime(
            runner=self.runner,
            repo_root=self.repo,
            environ={},
            out=self.out,
            err=self.err,
            sleep=self.sleep,
            clock=self.clock,
        )

    @property
    def config_path(self) -> Path:
        return self.repo / "scripts" / "bootstrap" / "bootstrap.config.json"

    def write_config(self, data: Mapping[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_config(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.config_path.read_text(encoding="utf-8"))
        return loaded

    def template(self, name: str) -> str:
        return str(self.repo / "infra" / "bootstrap" / name)

    def refusal(self) -> dict[str, Any]:
        lines = self.err.getvalue().strip().splitlines()
        assert lines, "nothing was written to standard error"
        record: dict[str, Any] = json.loads(lines[-1])
        return record

    def reports(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.out.getvalue().splitlines()
            if line.startswith('{"step"')
        ]


def new_harness(repo: Path) -> Harness:
    instance = Harness(repo)
    instance.write_config(config_data())
    regions = repo / "infra" / "config" / "regions.json"
    regions.parent.mkdir(parents=True)
    regions.write_text(json.dumps({"githubPrivateNetworkingRegions": ["eastus2", "westus2"]}))
    return instance


# Argument lists the scripts send, built the way the scripts build them.


def identity_show(name: str) -> tuple[str, ...]:
    return (
        "az",
        "identity",
        "show",
        "--name",
        name,
        "--resource-group",
        "rg-docchat-identity",
        "--subscription",
        SUBSCRIPTION,
        "--query",
        "{clientId: clientId, principalId: principalId}",
        "--output",
        "json",
    )


def graph_get(url: str) -> tuple[str, ...]:
    return ("az", "rest", "--method", "GET", "--url", url, "--output", "json")


def graph_write(method: str, url: str, body: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        "az",
        "rest",
        "--method",
        method,
        "--url",
        url,
        "--body",
        json.dumps(body, separators=(",", ":")),
        "--headers",
        "Content-Type=application/json",
        "--output",
        "json",
    )


def group_lookup(name: str) -> tuple[str, ...]:
    query = quote(f"displayName eq '{name}'", safe="")
    return graph_get(f"{GRAPH}/groups?$filter={query}&$select=id,displayName")


def record_identity(runner: FakeRunner, name: str, client_id: str, principal_id: str) -> None:
    runner.answer_json(identity_show(name), {"clientId": client_id, "principalId": principal_id})


def record_group(runner: FakeRunner, name: str, group_id: str | None) -> None:
    value = [{"id": group_id, "displayName": name}] if group_id else []
    runner.answer_json(group_lookup(name), {"value": value})


def oidc_template(runner: FakeRunner, payload: Mapping[str, Any]) -> None:
    runner.answer_json(
        ("gh", "api", f"/repos/{ORGANIZATION}/{REPOSITORY}/actions/oidc/customization/sub"),
        payload,
    )


def vault_lookup() -> tuple[str, ...]:
    return (
        "az",
        "resource",
        "list",
        "--resource-group",
        "rg-docchat-shared",
        "--subscription",
        SUBSCRIPTION,
        "--resource-type",
        "Microsoft.KeyVault/vaults",
        "--query",
        "[?starts_with(name, 'kv-docchat-tst-')].{id: id, name: name}",
        "--output",
        "json",
    )


VAULT_NAME = "kv-docchat-tst-abc123"
VAULT_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-shared"
    f"/providers/Microsoft.KeyVault/vaults/{VAULT_NAME}"
)
