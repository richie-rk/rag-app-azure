"""The one-time bootstrap of DocChat's tenant, subscriptions and repository.

83-infrastructure.md section 11 (ADR 0114): everything a pipeline identity must never be able to
change is created here, outside the pipeline, by the bootstrap operator (Owner on every subscription
in use; Privileged Role Administrator, Groups Administrator and Security Administrator in Entra; an
owner of the GitHub organization):

    uv run --frozen python scripts/bootstrap/bootstrap.py [--step <name>]

Every step is idempotent and runs alone with `--step`. A refusal prints one JSON line
`{"code", "message", "detail"}` (section 10.18) and exits 2; a failing `az` or `gh` call stops the
script with that tool's own error text and exit status. Either way the earlier steps stay in place
and nothing further changes.

Python 3.13 standard library plus the `az` and `gh` command-line tools. configure_github.py,
lock_prod.py and tighten_conditions.py import the shared helpers of this module; every `az` and
`gh` call goes through `Tools`, so the unit tests replay recorded answers.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Final, NoReturn, Protocol, TextIO
from urllib.parse import quote

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
CONFIG_PATH: Final = Path("scripts/bootstrap/bootstrap.config.json")
BOOTSTRAP_TEMPLATES: Final = Path("infra/bootstrap")

ENVIRONMENTS: Final = ("dev", "prod", "load")
IDENTITY_GROUP: Final = "rg-docchat-identity"
SHARED_GROUP: Final = "rg-docchat-shared"
# Section 1.3: the identity and shared groups are in eastus2 whatever the environments' region.
SHARED_LOCATION: Final = "eastus2"
SECURITY_WORKSPACE: Final = "log-docchat-security"
DEV_GROUP: Final = "rg-docchat-dev"
DEV_WORKSPACE: Final = "log-docchat-dev"

REFUSAL_EXIT: Final = 2
TOOL_FAILURE_EXIT: Final = 1
SUCCEEDED: Final = "succeeded"
SKIPPED: Final = "skipped"

MIN_AZ_VERSION: Final = (2, 76, 0)
MIN_BICEP_VERSION: Final = (0, 47, 16)
MIN_APPROVERS: Final = 2
MIN_GROUP_OWNERS: Final = 2

_PREREQUISITE = (
    "A tool, role, action name, Graph grant or OIDC subject format check failed; "
    "nothing further changed."
)
_TIGHTENING = "Principal IDs were missing, or the stored condition doesn't match the rendered one."

# Section 10.18, the codes and messages of the bootstrap scripts.
MESSAGES: Final[Mapping[str, str]] = {
    "missing_prerequisite": _PREREQUISITE,
    "unknown_action": _PREREQUISITE,
    "graph_permission_failed": _PREREQUISITE,
    "oidc_subject_customised": _PREREQUISITE,
    "oidc_subject_shared": (
        "Two pipeline identities would trust the same GitHub OIDC subject; nothing was created."
    ),
    "approver_is_operator": (
        "A just-in-time approver is also listed in operators; nothing changed."
    ),
    "policy_definition_missing": (
        "A built-in policy definition is missing or can't deny; no policy was assigned."
    ),
    "github_protection_mismatch": (
        "A protected GitHub environment doesn't show the required reviewers, self-review block "
        "and no administrator bypass."
    ),
    "ci_region_unsupported": (
        "The CI network's region isn't one where GitHub joins hosted runners to an Azure network; "
        "nothing was created."
    ),
    "ci_private_networking_unavailable": (
        "The GitHub organization can't create the 2-core Ubuntu 24.04 larger runner that private "
        "networking needs (it needs GitHub Team or Enterprise Cloud); set ciPrivateNetworking to "
        "false and re-run the shared, assignments, github and ci-runner steps to use standard "
        "runners."
    ),
    "ci_runner_mismatch": (
        "The network configuration, runner group or runner read back doesn't match the design; "
        "re-run the step."
    ),
    "principal_ids_missing": _TIGHTENING,
    "condition_update_failed": _TIGHTENING,
    "prod_not_protected": (
        "Production lacks one of its six docchat-protect locks or the "
        "docchat-deny-store-deletion assignment."
    ),
}

DEPLOY_IDENTITIES: Final[Mapping[str, str]] = {
    "dev": "id-docchat-deploy-dev",
    "prod": "id-docchat-deploy-prod",
    "load": "id-docchat-deploy-load",
}
PR_IDENTITY: Final = "id-docchat-pr"
CITEST_IDENTITY: Final = "id-docchat-citest"

OPERATORS_GROUP: Final = "sg-docchat-operators"
JIT_GROUP_ROLES: Final = ("conversation-readers", "user-data-operators", "config-operators")

OWNER_ROLE: Final = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
TEST_VAULT_FIREWALL_EDITOR_ROLE: Final = "28936cba-1b39-4715-987a-4fc5adcdc5ab"
# Section 6.1 person roles: the bootstrap operator's Entra roles, by role template ID.
ENTRA_ROLES: Final[Mapping[str, str]] = {
    "Privileged Role Administrator": "e8611ab8-c189-46e8-94e1-60213ab1f814",
    "Groups Administrator": "fdd7a751-b60b-444a-984c-02652fe8fa1c",
    "Security Administrator": "194ae4cb-b126-40b2-bd5b-6091b380977d",
}

PROVIDERS: Final = (
    "Microsoft.Web",
    "Microsoft.Sql",
    "Microsoft.DocumentDB",
    "Microsoft.Storage",
    "Microsoft.Search",
    "Microsoft.CognitiveServices",
    "Microsoft.KeyVault",
    "Microsoft.ContainerRegistry",
    "Microsoft.Network",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.Security",
    "Microsoft.EventGrid",
    "Microsoft.AlertsManagement",
    "Microsoft.ContainerInstance",
)
GITHUB_NETWORK_PROVIDER: Final = "GitHub.Network"

GRAPH: Final = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID: Final = "00000003-0000-0000-c000-000000000000"
APPLICATION_READWRITE_OWNEDBY: Final = "18a4783c-866b-4cc7-a460-3d5e5662c884"
GRAPH_RETRY_LIMIT_SECONDS: Final = 300.0
GRAPH_RETRY_WAIT_SECONDS: Final = 20.0
ENTRA_DIAGNOSTIC_SETTING_URL: Final = (
    "https://management.azure.com/providers/microsoft.aadiam/diagnosticSettings/"
    "docchat-security?api-version=2017-04-01"
)
ENTRA_LOG_CATEGORIES: Final = (
    "AuditLogs",
    "SignInLogs",
    "ServicePrincipalSignInLogs",
    "ManagedIdentitySignInLogs",
)

OIDC_ISSUER: Final = "https://token.actions.githubusercontent.com"
# Section 6.1: identity, credential name and the subject's part after the repository prefix.
FEDERATED_CREDENTIALS: tuple[tuple[str, str, str], ...] = (
    ("id-docchat-deploy-dev", "github-environment-dev", "environment:dev"),
    ("id-docchat-deploy-prod", "github-environment-prod", "environment:prod"),
    ("id-docchat-deploy-load", "github-environment-load", "environment:load"),
    (PR_IDENTITY, "github-pull-request", "pull_request"),
    (CITEST_IDENTITY, "github-environment-dev-tests", "environment:dev-tests"),
    (CITEST_IDENTITY, "github-environment-load-tests", "environment:load-tests"),
)
CREDENTIAL_CONFLICT_RETRIES: Final = 6
CREDENTIAL_CONFLICT_WAIT_SECONDS: Final = 20.0
_CONFLICT = re.compile(r"\b409\b|Conflict")
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

CI_NETWORK_SETTINGS: Final = "ghns-docchat-ci"
CI_NETWORK_SETTINGS_API: Final = "2024-04-02"
CI_NETWORK_CONFIGURATION: Final = "docchat-ci"
CI_RUNNER_GROUP: Final = "docchat-ci-private"
CI_RUNNER: Final = "docchat-ci-private"
CI_RUNNER_SIZE: Final = "2-core"
CI_RUNNER_IMAGE: Final = "ubuntu-24.04"
CI_MAX_RUNNERS: Final = 10

CONFIG_KEYS: Final = frozenset(
    {
        "tenantId",
        "subscriptions",
        "location",
        "github",
        "ciPrivateNetworking",
        "ciNetworkLocation",
        "operators",
        "approverObjectIds",
        "groupOwners",
        "securityAlertEmails",
        "testUserAliases",
        "allowedRoleAssigneePrincipalIds",
        "templateCosmosAssignmentIds",
    }
)

# Section 11.5, printed by the `checklist` step.
POST_PROVISION_STEPS: Final = (
    (
        "1. scripts/identity_post_provision.py (section 7.5): tenant-wide consent, first admins, "
        "test-group assignments. Who: Cloud Application Administrator or Application "
        "Administrator. Environments: all; load on every run, at the load-access gate."
    ),
    (
        "2. In dev first scripts/bootstrap/bootstrap.py --step assignments (id-docchat-citest's "
        "Log Analytics Reader on log-docchat-dev, which the provision created), then "
        "scripts/bootstrap/tighten_conditions.py --env <env> (sections 6.11 and 11.6). "
        "Who: bootstrap operator. Environments: dev, prod."
    ),
    (
        "3. scripts/bootstrap/lock_prod.py (section 12.3). Who: bootstrap operator. "
        "Environments: prod."
    ),
    (
        "4. Entra diagnostic setting docchat-prod-audit (AuditLogs to the prod workspace, "
        "section 12.4). Who: Security Administrator. Environments: prod."
    ),
    (
        "5. The first daily scanner canary reports clean, after which uploads open (section "
        "11.7). Who: operator. Environments: prod, and any environment that turns scanning on."
    ),
    (
        "6. First test-user passwords for both user sets (docchat-ops testusers rotate). Who: "
        "operator holding Password Administrator scoped to the administrative unit DocChat test "
        "users. Once, after the bootstrap."
    ),
    (
        "7. Assign DocChat Cosmos Data Access Granter to each approver at the environment's "
        "Cosmos DB account. Who: bootstrap operator. Only when cosmosGroupDataAccess is false."
    ),
    (
        "8. Configure PIM for Groups on the three just-in-time groups (eligible member "
        "assignments for the people in operators only). Who: a Privileged Role Administrator. "
        "Tenants with Entra ID P2 or ID Governance."
    ),
    (
        "9. Add the host account to docchat-deny-storage-shared-key's notScopes. "
        "Who: bootstrap operator. Only under content-share fallback 1 (section 2.7)."
    ),
)


class ScriptError(Exception):
    """A refusal of section 10.18: one JSON line on standard error and exit 2."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class StepError(Exception):
    """A failure the spec leaves to the tool ("the az error", "the write error")."""

    def __init__(self, text: str, exit_code: int = TOOL_FAILURE_EXIT) -> None:
        super().__init__(text)
        self.text = text
        self.exit_code = exit_code


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class ToolError(StepError):
    """An `az` or `gh` call exited non-zero; its own error text is relayed."""

    def __init__(self, args: Sequence[str], result: CommandResult) -> None:
        self.summary = f"`{' '.join(args[:3])}` exited {result.returncode}"
        self.result = result
        text = result.stderr if result.stderr.endswith("\n") else f"{result.stderr}\n"
        super().__init__(text, result.returncode or TOOL_FAILURE_EXIT)

    @property
    def http_status(self) -> int | None:
        match = _HTTP_STATUS.search(self.result.stderr)
        return int(match.group(1)) if match else None


class ChildScriptError(Exception):
    """A child script already reported its own refusal; only its exit status remains."""

    def __init__(self, returncode: int) -> None:
        super().__init__(returncode)
        self.returncode = returncode


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult: ...


def run_command(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> CommandResult:
    """Runs one tool with an argument list (never a shell) and captures its output."""
    executable = shutil.which(args[0])
    if executable is None:
        return CommandResult(127, "", f"{args[0]}: command not found\n")
    environment = {**os.environ, **env} if env else None
    # The arguments come from this script and the committed configuration, and no shell parses them.
    completed = subprocess.run(  # noqa: S603
        [executable, *args[1:]],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def parse_json(text: str, what: str) -> Any:
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError as error:
        raise StepError(f"{what} returned output that isn't JSON\n") from error


def as_dict(value: Any, what: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise StepError(f"{what}: expected a JSON object\n")


def as_list(value: Any, what: str) -> list[Any]:
    if isinstance(value, list):
        return value
    raise StepError(f"{what}: expected a JSON array\n")


def as_str(value: Any, what: str) -> str:
    if isinstance(value, str):
        return value
    raise StepError(f"{what}: expected a string\n")


def listing(payload: Any, key: str, what: str) -> list[dict[str, Any]]:
    """The items of a GitHub list answer, bare or wrapped as {"total_count", <key>: [...]}."""
    items = payload.get(key, []) if isinstance(payload, dict) else payload
    return [as_dict(item, what) for item in as_list(items, what)]


class Tools:
    """Every `az` and `gh` call of the bootstrap scripts."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        return self._runner(list(args), env=env, stdin=stdin)

    def checked(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> str:
        result = self.run(args, env=env, stdin=stdin)
        if result.returncode != 0:
            raise ToolError(args, result)
        return result.stdout

    def az(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        return self.checked(["az", *args], env=env)

    def az_json(self, *args: str) -> Any:
        return parse_json(self.az(*args, "--output", "json"), f"az {args[0]}")

    def az_tsv(self, *args: str) -> str:
        return self.az(*args, "--output", "tsv").strip()

    def gh(self, *args: str, stdin: str | None = None) -> str:
        return self.checked(["gh", *args], stdin=stdin)

    def gh_json(self, *args: str, stdin: str | None = None) -> Any:
        return parse_json(self.gh(*args, stdin=stdin), f"gh {args[0]}")

    def rest(self, method: str, url: str, body: Mapping[str, Any] | None = None) -> Any:
        args = ["rest", "--method", method, "--url", url]
        if body is not None:
            args += [
                "--body",
                json.dumps(body, separators=(",", ":")),
                "--headers",
                "Content-Type=application/json",
            ]
        return parse_json(self.az(*args, "--output", "json"), "az rest")

    def graph_list(self, url: str) -> list[dict[str, Any]]:
        """Every item of a Microsoft Graph collection, following @odata.nextLink."""
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            page = as_dict(self.rest("GET", next_url), "Microsoft Graph")
            items += [
                as_dict(item, "Microsoft Graph") for item in as_list(page.get("value", []), "value")
            ]
            link = page.get("@odata.nextLink")
            next_url = link if isinstance(link, str) else None
        return items


@dataclass(frozen=True)
class Config:
    """scripts/bootstrap/bootstrap.config.json (section 8.5; 81-config.md section 4.7)."""

    path: Path
    raw: dict[str, Any]
    tenant_id: str
    subscriptions: Mapping[str, str]
    location: str
    github_organization: str
    github_repository: str
    ci_private_networking: bool
    ci_network_location: str
    operators: tuple[str, ...]
    approver_object_ids: Mapping[str, tuple[str, ...]]
    group_owners: tuple[str, ...]
    security_alert_emails: tuple[str, ...]
    test_user_aliases: tuple[str, ...]
    allowed_role_assignee_principal_ids: tuple[str, ...]
    template_cosmos_assignment_ids: tuple[str, ...]

    @property
    def home_subscription(self) -> str:
        """The subscription of rg-docchat-identity and rg-docchat-shared: `dev`'s, where the
        pull-request what-if reads both rg-docchat-dev and rg-docchat-shared."""
        return self.subscriptions["dev"]

    def subscriptions_in_use(self) -> list[str]:
        seen: list[str] = []
        for environment in ENVIRONMENTS:
            subscription = self.subscriptions[environment]
            if subscription.lower() not in {known.lower() for known in seen}:
                seen.append(subscription)
        return seen

    def environments_in(self, subscription: str) -> list[str]:
        return [
            environment
            for environment in ENVIRONMENTS
            if self.subscriptions[environment].lower() == subscription.lower()
        ]


def _config_error(key: str, problem: str) -> NoReturn:
    raise ScriptError("missing_prerequisite", f"bootstrap.config.json {key}: {problem}")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        _config_error(key, "must be a non-empty string")
    return value


def _uuid(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not _is_uuid(value):
        _config_error(key, "must be a UUID")
    return str(value)


def _strings(data: Mapping[str, Any], key: str, *, uuids: bool = False) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _config_error(key, "must be an array of strings")
    if uuids and not all(_is_uuid(item) for item in value):
        _config_error(key, "must hold UUIDs only")
    return tuple(value)


def _per_environment(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict) or set(value) != set(ENVIRONMENTS):
        _config_error(key, "must be an object with exactly the keys dev, prod and load")
    return value


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScriptError(
            "missing_prerequisite", f"{path.name} can't be read ({type(error).__name__})"
        ) from error
    if not isinstance(raw, dict):
        _config_error("(root)", "must be a JSON object")
    unknown = sorted(set(raw) - CONFIG_KEYS)
    missing = sorted(CONFIG_KEYS - set(raw))
    if unknown or missing:
        _config_error(", ".join(unknown + missing), "unknown or missing key")
    subscriptions = _per_environment(raw, "subscriptions")
    approvers = _per_environment(raw, "approverObjectIds")
    github = raw.get("github")
    if not isinstance(github, dict) or set(github) != {"organization", "repository"}:
        _config_error("github", "must be an object with organization and repository")
    if not isinstance(raw.get("ciPrivateNetworking"), bool):
        _config_error("ciPrivateNetworking", "must be true or false")
    return Config(
        path=path,
        raw=raw,
        tenant_id=_uuid(raw, "tenantId"),
        subscriptions={env: _uuid(subscriptions, env) for env in ENVIRONMENTS},
        location=_text(raw, "location"),
        github_organization=_text(github, "organization"),
        github_repository=_text(github, "repository"),
        ci_private_networking=raw["ciPrivateNetworking"],
        ci_network_location=_text(raw, "ciNetworkLocation"),
        operators=_strings(raw, "operators", uuids=True),
        approver_object_ids={env: _strings(approvers, env, uuids=True) for env in ENVIRONMENTS},
        group_owners=_strings(raw, "groupOwners", uuids=True),
        security_alert_emails=_strings(raw, "securityAlertEmails"),
        test_user_aliases=_strings(raw, "testUserAliases"),
        allowed_role_assignee_principal_ids=_strings(
            raw, "allowedRoleAssigneePrincipalIds", uuids=True
        ),
        template_cosmos_assignment_ids=_strings(raw, "templateCosmosAssignmentIds"),
    )


def json_text(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_with_diff(path: Path, new_text: str, label: str, out: TextIO) -> bool:
    """Writes a file the operator commits, and prints the diff for review; False when unchanged."""
    try:
        old_text = path.read_text(encoding="utf-8")
        if old_text == new_text:
            return False
        path.write_text(new_text, encoding="utf-8")
    except OSError as error:
        raise StepError(f"{label} can't be written ({type(error).__name__})\n") from error
    out.writelines(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )
    return True


def merge_ids(existing: Iterable[str], additions: Iterable[str]) -> list[str]:
    """`existing` followed by every addition it lacks, compared without case."""
    merged = list(existing)
    known = {item.lower() for item in merged}
    for item in additions:
        if item.lower() not in known:
            merged.append(item)
            known.add(item.lower())
    return merged


@dataclass
class Context:
    repo_root: Path
    config: Config
    tools: Tools
    environ: Mapping[str, str]
    out: TextIO
    err: TextIO
    sleep: Callable[[float], None]
    clock: Callable[[], float]

    def template(self, name: str) -> Path:
        return self.repo_root / BOOTSTRAP_TEMPLATES / name

    def update_config(self, **changes: Any) -> None:
        raw = {**self.config.raw, **changes}
        write_with_diff(self.config.path, json_text(raw), str(CONFIG_PATH), self.out)
        self.config = load_config(self.config.path)


@dataclass(frozen=True)
class Runtime:
    """What a script touches outside itself; the unit tests replace each part."""

    runner: Runner = field(default=run_command)
    repo_root: Path = REPO_ROOT
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)


def write_summary(line: str, environ: Mapping[str, str]) -> None:
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def report(record: Mapping[str, Any], stream: TextIO, environ: Mapping[str, str]) -> None:
    line = json.dumps(record)
    stream.write(line + "\n")
    write_summary(line, environ)


def run_script(body: Callable[[Context], None], runtime: Runtime) -> int:
    """Loads the configuration, runs `body` and turns its failure into the exit status."""
    try:
        context = Context(
            repo_root=runtime.repo_root,
            config=load_config(runtime.repo_root / CONFIG_PATH),
            tools=Tools(runtime.runner),
            environ=runtime.environ,
            out=runtime.out,
            err=runtime.err,
            sleep=runtime.sleep,
            clock=runtime.clock,
        )
        body(context)
    except ScriptError as error:
        record = {"code": error.code, "message": MESSAGES[error.code], "detail": error.detail}
        report(record, runtime.err, runtime.environ)
        return REFUSAL_EXIT
    except ChildScriptError as error:
        return error.returncode
    except StepError as error:
        runtime.err.write(error.text)
        return error.exit_code
    return 0


# Shared Azure lookups ---------------------------------------------------------------------------


def identity_ids(ctx: Context, name: str) -> tuple[str, str]:
    """(client ID, principal ID) of a pipeline identity in rg-docchat-identity."""
    data = as_dict(
        ctx.tools.az_json(
            "identity",
            "show",
            "--name",
            name,
            "--resource-group",
            IDENTITY_GROUP,
            "--subscription",
            ctx.config.home_subscription,
            "--query",
            "{clientId: clientId, principalId: principalId}",
        ),
        name,
    )
    return as_str(data.get("clientId"), name), as_str(data.get("principalId"), name)


def find_group_id(ctx: Context, name: str) -> str | None:
    query = quote(f"displayName eq '{name}'", safe="")
    groups = ctx.tools.graph_list(f"{GRAPH}/groups?$filter={query}&$select=id,displayName")
    if len(groups) > 1:
        raise StepError(f"more than one Entra group is named {name}\n")
    return as_str(groups[0].get("id"), name) if groups else None


def require_group_id(ctx: Context, name: str) -> str:
    group_id = find_group_id(ctx, name)
    if group_id is None:
        raise ScriptError(
            "missing_prerequisite", f"group {name} doesn't exist; run the groups step"
        )
    return group_id


def find_test_vault(ctx: Context) -> tuple[str, str]:
    """(name, resource ID) of the test-credential vault kv-docchat-tst-<sharedToken>."""
    rows = as_list(
        ctx.tools.az_json(
            "resource",
            "list",
            "--resource-group",
            SHARED_GROUP,
            "--subscription",
            ctx.config.home_subscription,
            "--resource-type",
            "Microsoft.KeyVault/vaults",
            "--query",
            "[?starts_with(name, 'kv-docchat-tst-')].{id: id, name: name}",
        ),
        "test vault",
    )
    if len(rows) != 1:
        raise ScriptError(
            "missing_prerequisite",
            f"expected one test-credential vault in {SHARED_GROUP}, found {len(rows)}; "
            "run the shared step",
        )
    row = as_dict(rows[0], "test vault")
    return as_str(row.get("name"), "test vault"), as_str(row.get("id"), "test vault")


def security_workspace_id(config: Config) -> str:
    return (
        f"/subscriptions/{config.home_subscription}/resourceGroups/{IDENTITY_GROUP}"
        f"/providers/Microsoft.OperationalInsights/workspaces/{SECURITY_WORKSPACE}"
    )


def deploy_to_group(
    ctx: Context, template: str, group: str, parameters: Path, env: Mapping[str, str]
) -> None:
    ctx.tools.az(
        *group_deployment_args(ctx, template, group, parameters),
        env=env,
    )


def group_deployment_args(ctx: Context, template: str, group: str, parameters: Path) -> list[str]:
    return [
        "deployment",
        "group",
        "create",
        "--name",
        f"docchat-bootstrap-{template}",
        "--resource-group",
        group,
        "--subscription",
        ctx.config.home_subscription,
        "--parameters",
        str(parameters),
        "--output",
        "none",
    ]


def deploy_to_subscription(
    ctx: Context, template: str, subscription: str, env: Mapping[str, str]
) -> None:
    ctx.tools.az(
        "deployment",
        "sub",
        "create",
        "--name",
        f"docchat-bootstrap-{template}",
        "--location",
        ctx.config.location,
        "--subscription",
        subscription,
        "--parameters",
        str(ctx.template(f"{template}.bicepparam")),
        "--output",
        "none",
        env=env,
    )


def compiled_variable(ctx: Context, template: str, variable: str) -> list[dict[str, Any]]:
    """A literal variable of a compiled bootstrap template, the one list both the template and
    the script's checks use."""
    output = ctx.tools.az("bicep", "build", "--file", str(ctx.template(template)), "--stdout")
    compiled = as_dict(parse_json(output, template), template)
    values = as_dict(compiled.get("variables"), template).get(variable)
    return [as_dict(item, f"{template} {variable}") for item in as_list(values, variable)]


# OIDC subjects (section 6.1) ---------------------------------------------------------------------


def oidc_subject_prefix(ctx: Context) -> str:
    """The subject prefix of the repository's OIDC template; refuses a customised one."""
    organization = ctx.config.github_organization
    repository = ctx.config.github_repository
    template = as_dict(
        ctx.tools.gh_json(
            "api", f"/repos/{organization}/{repository}/actions/oidc/customization/sub"
        ),
        "OIDC subject template",
    )
    if template.get("use_default") is not True:
        raise ScriptError(
            "oidc_subject_customised",
            f"{organization}/{repository} uses a customised OIDC subject template",
        )
    if template.get("use_immutable_subject") is not True:
        return f"repo:{organization}/{repository}"
    # The immutable form (ADR 0120): repo:{owner}@{owner_id}/{repo}@{repo_id}.
    details = as_dict(ctx.tools.gh_json("api", f"/repos/{organization}/{repository}"), "repository")
    owner = as_dict(details.get("owner"), "repository owner")
    return (
        f"repo:{as_str(owner.get('login'), 'owner')}@{owner.get('id')}"
        f"/{as_str(details.get('name'), 'repository')}@{details.get('id')}"
    )


def federated_credential_plan(prefix: str) -> list[dict[str, str]]:
    return [
        {"identity": identity, "name": name, "subject": f"{prefix}:{suffix}"}
        for identity, name, suffix in FEDERATED_CREDENTIALS
    ]


def check_unique_subjects(plan: Sequence[Mapping[str, str]]) -> None:
    """Stops when two identities would trust one issuer and subject pair: a job of that subject
    could then choose the stronger identity by its client ID (ADR 0120)."""
    trusted: dict[tuple[str, str], str] = {}
    for credential in plan:
        pair = (OIDC_ISSUER, credential["subject"])
        first = trusted.setdefault(pair, credential["identity"])
        if first != credential["identity"]:
            raise ScriptError(
                "oidc_subject_shared",
                f"{first} and {credential['identity']} would both trust {credential['subject']}",
            )


# Steps -------------------------------------------------------------------------------------------


def step_prerequisites(ctx: Context) -> str:
    check_configuration(ctx.config)
    try:
        check_tools(ctx)
        check_caller_roles(ctx)
    except ToolError as error:
        raise ScriptError("missing_prerequisite", error.summary) from error
    return SUCCEEDED


def check_configuration(config: Config) -> None:
    emails = config.security_alert_emails
    if not emails or not all(_EMAIL.fullmatch(address) for address in emails):
        raise ScriptError(
            "missing_prerequisite",
            "securityAlertEmails needs at least one shared mailbox, the only receiver of every "
            "security alert",
        )
    operators = {operator.lower() for operator in config.operators}
    for environment in ENVIRONMENTS:
        approvers = {approver.lower() for approver in config.approver_object_ids[environment]}
        both = sorted(approvers & operators)
        if both:
            raise ScriptError(
                "approver_is_operator", f"{environment} approver {both[0]} is listed in operators"
            )
        if len(approvers) < MIN_APPROVERS:
            raise ScriptError(
                "missing_prerequisite",
                f"approverObjectIds.{environment} needs at least {MIN_APPROVERS} people",
            )
    if len({owner.lower() for owner in config.group_owners}) < MIN_GROUP_OWNERS:
        raise ScriptError(
            "missing_prerequisite", f"groupOwners needs at least {MIN_GROUP_OWNERS} object IDs"
        )


def _version(text: str, tool: str) -> tuple[int, int, int]:
    match = _VERSION.search(text)
    if match is None:
        raise ScriptError("missing_prerequisite", f"the {tool} version can't be read")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def check_tools(ctx: Context) -> None:
    versions = as_dict(parse_json(ctx.tools.az("version", "--output", "json"), "az version"), "az")
    az_version = _version(str(versions.get("azure-cli", "")), "az")
    if az_version < MIN_AZ_VERSION:
        raise ScriptError("missing_prerequisite", f"az {az_version} is older than 2.76.0")
    bicep_version = _version(ctx.tools.az("bicep", "version"), "Bicep")
    if bicep_version < MIN_BICEP_VERSION:
        raise ScriptError("missing_prerequisite", f"Bicep {bicep_version} is older than 0.47.16")
    organization = ctx.config.github_organization
    login = ctx.tools.gh("api", "user", "--jq", ".login").strip()
    role = ctx.tools.gh("api", f"/orgs/{organization}/memberships/{login}", "--jq", ".role").strip()
    if role != "admin":
        raise ScriptError(
            "missing_prerequisite", f"gh is signed in as {login}, not an owner of {organization}"
        )


def check_caller_roles(ctx: Context) -> None:
    caller = ctx.tools.az_tsv("ad", "signed-in-user", "show", "--query", "id")
    for subscription in ctx.config.subscriptions_in_use():
        owners = ctx.tools.az_tsv(
            "role",
            "assignment",
            "list",
            "--assignee",
            caller,
            "--role",
            OWNER_ROLE,
            "--scope",
            f"/subscriptions/{subscription}",
            "--include-inherited",
            "--include-groups",
            "--query",
            "length(@)",
        )
        if owners in {"", "0"}:
            raise ScriptError(
                "missing_prerequisite",
                f"the caller doesn't hold Owner on subscription {subscription}",
            )
    roles = ctx.tools.graph_list(
        f"{GRAPH}/me/transitiveMemberOf/microsoft.graph.directoryRole?$select=roleTemplateId"
    )
    held = {str(role.get("roleTemplateId", "")).lower() for role in roles}
    missing = [name for name, template in ENTRA_ROLES.items() if template not in held]
    if missing:
        raise ScriptError(
            "missing_prerequisite", f"the caller lacks the Entra roles {', '.join(missing)}"
        )


def step_providers(ctx: Context) -> str:
    home = ctx.config.home_subscription
    for subscription in ctx.config.subscriptions_in_use():
        namespaces = list(PROVIDERS)
        # GitHub.Network serves only the CI network, which lives in rg-docchat-shared.
        if ctx.config.ci_private_networking and subscription == home:
            namespaces.append(GITHUB_NETWORK_PROVIDER)
        for namespace in namespaces:
            register_provider(ctx, namespace, subscription)
    return SUCCEEDED


def register_provider(ctx: Context, namespace: str, subscription: str) -> None:
    state_query = ("--namespace", namespace, "--subscription", subscription)
    show = ("provider", "show", *state_query, "--query", "registrationState")
    if ctx.tools.az_tsv(*show) == "Registered":
        return
    ctx.tools.az("provider", "register", *state_query, "--wait", "--output", "none")
    state = ctx.tools.az_tsv(*show)
    if state != "Registered":
        raise StepError(f"provider {namespace} is {state} in subscription {subscription}\n")


def resource_group_plan(config: Config) -> list[tuple[str, str, str, dict[str, str]]]:
    """(name, subscription, region, tags) of the five resource groups of section 1.3."""
    home = config.home_subscription
    plan = [
        (
            IDENTITY_GROUP,
            home,
            SHARED_LOCATION,
            {"docchat-env": "shared", "docchat-purpose": "identity"},
        ),
        (
            SHARED_GROUP,
            home,
            SHARED_LOCATION,
            {"docchat-env": "shared", "docchat-purpose": "shared"},
        ),
    ]
    plan += [
        (
            f"rg-docchat-{environment}",
            config.subscriptions[environment],
            config.location,
            {"docchat-env": environment, "docchat-purpose": "environment"},
        )
        for environment in ENVIRONMENTS
    ]
    return plan


def step_resource_groups(ctx: Context) -> str:
    for name, subscription, location, tags in resource_group_plan(ctx.config):
        tag_args = [f"{key}={value}" for key, value in tags.items()]
        exists = ctx.tools.az_tsv("group", "exists", "--name", name, "--subscription", subscription)
        if exists == "true":
            # Merge, never replace: rg-docchat-load carries the run tags of a live load run.
            ctx.tools.az(
                "tag",
                "update",
                "--resource-id",
                f"/subscriptions/{subscription}/resourceGroups/{name}",
                "--operation",
                "Merge",
                "--tags",
                *tag_args,
                "--output",
                "none",
            )
        else:
            ctx.tools.az(
                "group",
                "create",
                "--name",
                name,
                "--location",
                location,
                "--subscription",
                subscription,
                "--tags",
                *tag_args,
                "--output",
                "none",
            )
    return SUCCEEDED


def provider_operations(ctx: Context, namespace: str) -> set[str]:
    result = ctx.tools.run(
        ["az", "provider", "operation", "show", "--namespace", namespace, "--output", "json"]
    )
    if result.returncode != 0:
        raise ScriptError("unknown_action", f"the provider namespace {namespace} isn't known")
    provider = as_dict(parse_json(result.stdout, namespace), namespace)
    operations = list(as_list(provider.get("operations") or [], namespace))
    for resource_type in as_list(provider.get("resourceTypes") or [], namespace):
        operations += as_list(as_dict(resource_type, namespace).get("operations") or [], namespace)
    return {str(as_dict(operation, namespace).get("name", "")).lower() for operation in operations}


def step_roles(ctx: Context) -> str:
    known: dict[str, set[str]] = {}
    for role in compiled_variable(ctx, "roles.bicep", "roles"):
        role_name = role.get("roleName")
        actions = as_list(role.get("actions"), "actions") + as_list(
            role.get("dataActions"), "dataActions"
        )
        for action in actions:
            name = as_str(action, "action")
            namespace = name.split("/", 1)[0]
            if namespace not in known:
                known[namespace] = provider_operations(ctx, namespace)
            if name.lower() not in known[namespace]:
                raise ScriptError("unknown_action", f"{role_name}: {name}")
    deploy_to_subscription(ctx, "roles", ctx.config.home_subscription, {})
    return SUCCEEDED


def step_identity(ctx: Context) -> str:
    plan = federated_credential_plan(oidc_subject_prefix(ctx))
    check_unique_subjects(plan)
    args = [
        "az",
        *group_deployment_args(
            ctx, "identity", IDENTITY_GROUP, ctx.template("identity.bicepparam")
        ),
    ]
    env = {"DOCCHAT_BOOTSTRAP_FEDERATED_CREDENTIALS": json.dumps(plan)}
    result = ctx.tools.run(args, env=env)
    retries = 0
    # Entra ID replicates a new credential for up to 2 minutes; a 409 in that time is retried.
    while (
        result.returncode != 0
        and retries < CREDENTIAL_CONFLICT_RETRIES
        and _CONFLICT.search(result.stderr)
    ):
        retries += 1
        ctx.sleep(CREDENTIAL_CONFLICT_WAIT_SECONDS)
        result = ctx.tools.run(args, env=env)
    if result.returncode != 0:
        raise ToolError(args, result)
    return SUCCEEDED


def step_shared(ctx: Context) -> str:
    config = ctx.config
    env = {
        "DOCCHAT_CI_PRIVATE_NETWORKING": "true" if config.ci_private_networking else "false",
        "DOCCHAT_CI_NETWORK_LOCATION": config.ci_network_location,
        "DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST": ctx.environ.get(
            "DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", ""
        ),
        "DOCCHAT_SECURITY_WORKSPACE_ID": security_workspace_id(config),
    }
    deploy_to_group(ctx, "shared", SHARED_GROUP, ctx.repo_root / "infra" / "shared.bicepparam", env)
    return SUCCEEDED


def ensure_group(
    ctx: Context, name: str, *, owners: Sequence[str] = (), members: Sequence[str] = ()
) -> str:
    """Finds or creates a security group and adds missing owners and members, keeping the rest."""
    group_id = find_group_id(ctx, name)
    if group_id is None:
        created = ctx.tools.rest(
            "POST",
            f"{GRAPH}/groups",
            {
                "displayName": name,
                "mailNickname": name,
                "securityEnabled": True,
                "mailEnabled": False,
                "isAssignableToRole": False,
            },
        )
        group_id = as_str(as_dict(created, name).get("id"), name)
    add_missing(ctx, group_id, "owners", owners)
    add_missing(ctx, group_id, "members", members)
    return group_id


def add_missing(ctx: Context, group_id: str, relation: str, wanted: Sequence[str]) -> None:
    if not wanted:
        return
    present = {
        str(item.get("id", "")).lower()
        for item in ctx.tools.graph_list(f"{GRAPH}/groups/{group_id}/{relation}?$select=id")
    }
    for object_id in merge_ids([], wanted):
        if object_id.lower() not in present:
            ctx.tools.rest(
                "POST",
                f"{GRAPH}/groups/{group_id}/{relation}/$ref",
                {"@odata.id": f"{GRAPH}/directoryObjects/{object_id}"},
            )


def step_groups(ctx: Context) -> str:
    config = ctx.config
    approvers = [item for env in ENVIRONMENTS for item in config.approver_object_ids[env]]
    ensure_group(ctx, OPERATORS_GROUP, members=merge_ids(config.operators, approvers))
    for environment in ENVIRONMENTS:
        _, deploy_principal = identity_ids(ctx, DEPLOY_IDENTITIES[environment])
        ensure_group(ctx, f"sg-docchat-{environment}-sql-admins", members=[deploy_principal])
        for role in ("users", "admins"):
            ensure_group(ctx, f"sg-docchat-{environment}-{role}", owners=config.group_owners)
        for role in JIT_GROUP_ROLES:
            ensure_group(
                ctx,
                f"sg-docchat-{environment}-{role}",
                owners=config.approver_object_ids[environment],
            )
    return SUCCEEDED


def _grant_graph_permission(ctx: Context, graph_principal: str, principal_id: str) -> None:
    assignments = ctx.tools.graph_list(
        f"{GRAPH}/servicePrincipals/{principal_id}/appRoleAssignments"
    )
    if any(
        str(item.get("appRoleId", "")).lower() == APPLICATION_READWRITE_OWNEDBY
        and str(item.get("resourceId", "")).lower() == graph_principal.lower()
        for item in assignments
    ):
        return
    ctx.tools.rest(
        "POST",
        f"{GRAPH}/servicePrincipals/{graph_principal}/appRoleAssignedTo",
        {
            "principalId": principal_id,
            "resourceId": graph_principal,
            "appRoleId": APPLICATION_READWRITE_OWNEDBY,
        },
    )


def _attempt(action: Callable[[], None]) -> ToolError | None:
    try:
        action()
    except ToolError as error:
        return error
    return None


def grant_with_retry(ctx: Context, graph_principal: str, name: str) -> None:
    """A new service principal takes minutes to replicate in Entra ID, so every failure is
    retried for up to 5 minutes (section 11.2 step 8)."""
    _, principal_id = identity_ids(ctx, name)
    deadline = ctx.clock() + GRAPH_RETRY_LIMIT_SECONDS
    while True:
        failure = _attempt(partial(_grant_graph_permission, ctx, graph_principal, principal_id))
        if failure is None:
            return
        if ctx.clock() >= deadline:
            raise ScriptError("graph_permission_failed", f"{name}: {failure.summary}") from failure
        ctx.sleep(GRAPH_RETRY_WAIT_SECONDS)


def step_graph_permission(ctx: Context) -> str:
    try:
        graph = ctx.tools.rest(
            "GET", f"{GRAPH}/servicePrincipals(appId='{GRAPH_APP_ID}')?$select=id"
        )
    except ToolError as error:
        raise ScriptError("graph_permission_failed", error.summary) from error
    graph_principal = as_str(as_dict(graph, "Microsoft Graph").get("id"), "Microsoft Graph")
    for environment in ENVIRONMENTS:
        grant_with_retry(ctx, graph_principal, DEPLOY_IDENTITIES[environment])
    return SUCCEEDED


def pipeline_principal_ids(ctx: Context) -> dict[str, str]:
    names = {
        "deployDev": DEPLOY_IDENTITIES["dev"],
        "deployProd": DEPLOY_IDENTITIES["prod"],
        "deployLoad": DEPLOY_IDENTITIES["load"],
        "pr": PR_IDENTITY,
        "citest": CITEST_IDENTITY,
    }
    return {key: identity_ids(ctx, name)[1] for key, name in names.items()}


def dev_workspace_exists(ctx: Context) -> bool:
    """Whether `dev`'s first provision has created log-docchat-dev, the scope of
    id-docchat-citest's Log Analytics Reader; until then that one assignment waits for this
    step's run after the provision (section 11.5 step 2)."""
    found = ctx.tools.az_tsv(
        "resource",
        "list",
        "--resource-group",
        DEV_GROUP,
        "--subscription",
        ctx.config.subscriptions["dev"],
        "--resource-type",
        "Microsoft.OperationalInsights/workspaces",
        "--name",
        DEV_WORKSPACE,
        "--query",
        "[].id",
    )
    return bool(found)


def step_assignments(ctx: Context) -> str:
    config = ctx.config
    principals = pipeline_principal_ids(ctx)
    operators = require_group_id(ctx, OPERATORS_GROUP)
    dev_config_operators = require_group_id(ctx, "sg-docchat-dev-config-operators")
    workspace_exists = dev_workspace_exists(ctx)
    if not workspace_exists:
        ctx.out.write(
            f"{DEV_WORKSPACE} doesn't exist yet: run this step again after dev's first "
            "provision, before tightening, to give id-docchat-citest Log Analytics Reader on it "
            "(post-provision step 2)\n"
        )
    for subscription in config.subscriptions_in_use():
        env = {
            "DOCCHAT_BOOTSTRAP_ENVIRONMENTS": ",".join(config.environments_in(subscription)),
            "DOCCHAT_BOOTSTRAP_PIPELINE_PRINCIPAL_IDS": json.dumps(principals),
            "DOCCHAT_BOOTSTRAP_OPERATORS_GROUP_ID": operators,
            "DOCCHAT_BOOTSTRAP_DEV_CONFIG_OPERATORS_GROUP_ID": dev_config_operators,
            "DOCCHAT_BOOTSTRAP_DEV_WORKSPACE_EXISTS": "true" if workspace_exists else "false",
        }
        deploy_to_subscription(ctx, "assignments", subscription, env)
    if config.ci_private_networking:
        remove_vault_firewall_editor(ctx, principals["citest"])
    return SUCCEEDED


def remove_vault_firewall_editor(ctx: Context, citest_principal: str) -> None:
    """Under the private-networking option nobody edits the test vault's network rules, and an
    incremental deployment deletes nothing, so an assignment made earlier is removed here."""
    _, vault_id = find_test_vault(ctx)
    found = ctx.tools.az_tsv(
        "role",
        "assignment",
        "list",
        "--assignee",
        citest_principal,
        "--role",
        TEST_VAULT_FIREWALL_EDITOR_ROLE,
        "--scope",
        vault_id,
        "--subscription",
        ctx.config.home_subscription,
        "--query",
        "[].id",
    ).split()
    if found:
        ctx.tools.az("role", "assignment", "delete", "--ids", *found, "--output", "none")


def check_policy_definition(ctx: Context, definition: str) -> None:
    result = ctx.tools.run(
        ["az", "policy", "definition", "show", "--name", definition, "--output", "json"]
    )
    if result.returncode != 0:
        raise ScriptError(
            "policy_definition_missing", f"built-in definition {definition} wasn't found"
        )
    data = as_dict(parse_json(result.stdout, definition), definition)
    parameters = data.get("parameters") or as_dict(data.get("properties") or {}, definition).get(
        "parameters"
    )
    effect = {
        key.lower(): value for key, value in as_dict(parameters or {}, definition).items()
    }.get("effect")
    allowed = as_list(as_dict(effect or {}, definition).get("allowedValues") or [], definition)
    if "deny" not in {str(value).lower() for value in allowed}:
        raise ScriptError(
            "policy_definition_missing", f"{definition}'s effect parameter doesn't allow Deny"
        )


def step_policies(ctx: Context) -> str:
    for policy in compiled_variable(ctx, "policies.bicep", "builtInPolicies"):
        check_policy_definition(ctx, as_str(policy.get("definition"), "definition"))
    for subscription in ctx.config.subscriptions_in_use():
        environments = ",".join(ctx.config.environments_in(subscription))
        deploy_to_subscription(
            ctx, "policies", subscription, {"DOCCHAT_BOOTSTRAP_ENVIRONMENTS": environments}
        )
    return SUCCEEDED


def seed_allowed_assignees(ctx: Context) -> None:
    """`allowedRoleAssigneePrincipalIds` starts with sg-docchat-operators, id-docchat-citest and
    each config-operators group (section 8.5), so their bootstrap assignments raise no alert."""
    seeds = [
        require_group_id(ctx, OPERATORS_GROUP),
        identity_ids(ctx, CITEST_IDENTITY)[1],
        *(require_group_id(ctx, f"sg-docchat-{env}-config-operators") for env in ENVIRONMENTS),
    ]
    current = ctx.config.allowed_role_assignee_principal_ids
    merged = merge_ids(current, seeds)
    if len(merged) != len(current):
        ctx.update_config(allowedRoleAssigneePrincipalIds=merged)


def step_security(ctx: Context) -> str:
    seed_allowed_assignees(ctx)
    deploy_identities = []
    for environment in ENVIRONMENTS:
        name = DEPLOY_IDENTITIES[environment]
        client_id, principal_id = identity_ids(ctx, name)
        deploy_identities.append({"name": name, "clientId": client_id, "principalId": principal_id})
    jit_groups = [
        require_group_id(ctx, f"sg-docchat-{environment}-{role}")
        for environment in ENVIRONMENTS
        for role in JIT_GROUP_ROLES
    ]
    # The Activity Log and Entra settings come first, so the alert queries have their tables.
    for subscription in ctx.config.subscriptions_in_use():
        deploy_to_subscription(ctx, "activity-log", subscription, {})
    ctx.tools.rest(
        "PUT",
        ENTRA_DIAGNOSTIC_SETTING_URL,
        {
            "properties": {
                "workspaceId": security_workspace_id(ctx.config),
                "logs": [
                    {"category": category, "enabled": True} for category in ENTRA_LOG_CATEGORIES
                ],
            }
        },
    )
    env = {
        "DOCCHAT_BOOTSTRAP_DEPLOY_IDENTITY_IDS": json.dumps(deploy_identities),
        "DOCCHAT_BOOTSTRAP_JIT_GROUP_IDS": ",".join(jit_groups),
    }
    deploy_to_group(ctx, "security", IDENTITY_GROUP, ctx.template("security.bicepparam"), env)
    return SUCCEEDED


def step_config(ctx: Context) -> str:
    config = ctx.config
    operators = require_group_id(ctx, OPERATORS_GROUP)
    for environment in ENVIRONMENTS:
        group = f"sg-docchat-{environment}"
        groups = {
            "operators": operators,
            "sqlAdmins": require_group_id(ctx, f"{group}-sql-admins"),
            "users": require_group_id(ctx, f"{group}-users"),
            "admins": require_group_id(ctx, f"{group}-admins"),
            "configOperators": require_group_id(ctx, f"{group}-config-operators"),
            "conversationReaders": require_group_id(ctx, f"{group}-conversation-readers"),
            "userDataOperators": require_group_id(ctx, f"{group}-user-data-operators"),
        }
        label = f"infra/env/{environment}.json"
        path = ctx.repo_root / label
        try:
            data = as_dict(json.loads(path.read_text(encoding="utf-8")), label)
        except (OSError, ValueError) as error:
            raise StepError(f"{label} can't be read ({type(error).__name__})\n") from error
        data["subscriptionId"] = config.subscriptions[environment]
        data["tenantId"] = config.tenant_id
        data["groups"] = groups
        write_with_diff(path, json_text(data), label, ctx.out)
    return SUCCEEDED


def step_github(ctx: Context) -> str:
    script = ctx.repo_root / "scripts" / "bootstrap" / "configure_github.py"
    result = ctx.tools.run([sys.executable, str(script)])
    ctx.out.write(result.stdout)
    ctx.err.write(result.stderr)
    if result.returncode != 0:
        raise ChildScriptError(result.returncode)
    return SUCCEEDED


# The ci-runner step (section 11.3) ---------------------------------------------------------------


def step_ci_runner(ctx: Context) -> str:
    config = ctx.config
    if not config.ci_private_networking:
        return SKIPPED
    check_ci_region(ctx)
    organization = config.github_organization
    check_larger_runners(ctx, organization)
    github_id = ensure_network_settings(ctx, organization_database_id(ctx, organization))
    repository = as_dict(
        ctx.tools.gh_json("api", f"/repos/{organization}/{config.github_repository}"), "repository"
    )
    repository_id = repository.get("id")
    public = repository.get("visibility") == "public" or repository.get("private") is False
    configuration_id = ensure_github_object(
        ctx,
        f"/orgs/{organization}/settings/network-configurations",
        "network_configurations",
        {
            "name": CI_NETWORK_CONFIGURATION,
            "compute_service": "actions",
            "network_settings_ids": [github_id],
        },
    )
    group_id = ensure_github_object(
        ctx,
        f"/orgs/{organization}/actions/runner-groups",
        "runner_groups",
        {
            "name": CI_RUNNER_GROUP,
            "visibility": "selected",
            "selected_repository_ids": [repository_id],
            "allows_public_repositories": public,
            "restricted_to_workflows": False,
            "network_configuration_id": configuration_id,
        },
    )
    runner_id = ensure_github_object(
        ctx,
        f"/orgs/{organization}/actions/hosted-runners",
        "runners",
        {
            "name": CI_RUNNER,
            "image": {"id": CI_RUNNER_IMAGE, "source": "github"},
            "size": CI_RUNNER_SIZE,
            "runner_group_id": group_id,
            "maximum_runners": CI_MAX_RUNNERS,
            "enable_static_ip": False,
        },
    )
    differences = [
        *network_configuration_differences(ctx, organization, configuration_id, github_id),
        *runner_group_differences(
            ctx,
            organization,
            group_id,
            configuration_id=configuration_id,
            repository_id=repository_id,
            public=public,
        ),
        *hosted_runner_differences(ctx, organization, runner_id, group_id),
    ]
    if differences:
        raise ScriptError("ci_runner_mismatch", "; ".join(differences))
    return SUCCEEDED


def check_ci_region(ctx: Context) -> None:
    label = "infra/config/regions.json"
    try:
        regions = as_dict(json.loads((ctx.repo_root / label).read_text(encoding="utf-8")), label)
    except (OSError, ValueError) as error:
        raise StepError(f"{label} can't be read ({type(error).__name__})\n") from error
    allowed = as_list(
        regions.get("githubPrivateNetworkingRegions"), "githubPrivateNetworkingRegions"
    )
    if ctx.config.ci_network_location not in allowed:
        raise ScriptError(
            "ci_region_unsupported",
            f"ciNetworkLocation {ctx.config.ci_network_location} isn't in "
            "githubPrivateNetworkingRegions",
        )


def _unavailable(error: ToolError, what: str) -> ScriptError:
    return ScriptError(
        "ci_private_networking_unavailable", f"{what} answered HTTP {error.http_status}"
    )


def github_list(ctx: Context, path: str, key: str) -> list[dict[str, Any]]:
    try:
        payload = ctx.tools.gh_json("api", f"{path}?per_page=100")
    except ToolError as error:
        # An organization without larger runners is refused these lists.
        if error.http_status in {403, 404}:
            raise _unavailable(error, path) from error
        raise
    return listing(payload, key, path)


def check_larger_runners(ctx: Context, organization: str) -> None:
    base = f"/orgs/{organization}/actions/hosted-runners"
    sizes = {item.get("id") for item in github_list(ctx, f"{base}/machine-sizes", "machine_specs")}
    if CI_RUNNER_SIZE not in sizes:
        raise ScriptError(
            "ci_private_networking_unavailable",
            f"no {CI_RUNNER_SIZE} hosted-runner size is offered",
        )
    images = {item.get("id") for item in github_list(ctx, f"{base}/images/github-owned", "images")}
    if CI_RUNNER_IMAGE not in images:
        raise ScriptError(
            "ci_private_networking_unavailable",
            f"no {CI_RUNNER_IMAGE} hosted-runner image is offered",
        )


def organization_database_id(ctx: Context, organization: str) -> int:
    answer = ctx.tools.gh_json(
        "api",
        "graphql",
        "-f",
        "query=query($login: String!) { organization(login: $login) { databaseId } }",
        "-F",
        f"login={organization}",
    )
    data = as_dict(as_dict(answer, "graphql").get("data"), "graphql")
    database_id = as_dict(data.get("organization"), "graphql").get("databaseId")
    if not isinstance(database_id, int):
        raise StepError(f"the databaseId of {organization} can't be read\n")
    return database_id


def find_network_settings(ctx: Context) -> str:
    return ctx.tools.az_tsv(
        "resource",
        "list",
        "--resource-group",
        SHARED_GROUP,
        "--subscription",
        ctx.config.home_subscription,
        "--resource-type",
        "GitHub.Network/networkSettings",
        "--name",
        CI_NETWORK_SETTINGS,
        "--query",
        "[].id",
    )


def ensure_network_settings(ctx: Context, database_id: int) -> str:
    """Deploys ghns-docchat-ci only while it doesn't exist, so no redeploy rewrites the GitHubId
    tag the service sets, and returns that tag."""
    resource_id = find_network_settings(ctx)
    if not resource_id:
        deploy_to_group(
            ctx,
            "ci-network-settings",
            SHARED_GROUP,
            ctx.template("ci-network-settings.bicepparam"),
            {"DOCCHAT_BOOTSTRAP_GITHUB_DATABASE_ID": str(database_id)},
        )
        resource_id = find_network_settings(ctx)
    if not resource_id:
        raise ScriptError(
            "ci_runner_mismatch", f"{CI_NETWORK_SETTINGS} wasn't found after its deployment"
        )
    github_id = ctx.tools.az_tsv(
        "resource",
        "show",
        "--ids",
        resource_id,
        "--api-version",
        CI_NETWORK_SETTINGS_API,
        "--query",
        "tags.GitHubId",
    )
    if not github_id:
        raise ScriptError("ci_runner_mismatch", f"{CI_NETWORK_SETTINGS} carries no GitHubId tag")
    return github_id


def ensure_github_object(ctx: Context, path: str, key: str, body: Mapping[str, Any]) -> Any:
    """The ID of the object named body["name"], created only when absent."""
    for item in github_list(ctx, path, key):
        if item.get("name") == body["name"]:
            return item.get("id")
    args = ["gh", "api", "--method", "POST", path, "--input", "-"]
    result = ctx.tools.run(args, stdin=json.dumps(body))
    if result.returncode != 0:
        error = ToolError(args, result)
        if error.http_status == 403:  # noqa: PLR2004 - the HTTP status GitHub refuses a plan with
            raise _unavailable(error, f"POST {path}") from None
        raise error
    return as_dict(parse_json(result.stdout, path), path).get("id")


def _differs(what: str, actual: Any, expected: Any) -> list[str]:
    return [] if actual == expected else [f"{what} is {actual!r}, expected {expected!r}"]


def network_configuration_differences(
    ctx: Context, organization: str, configuration_id: Any, github_id: str
) -> list[str]:
    configuration = as_dict(
        ctx.tools.gh_json(
            "api", f"/orgs/{organization}/settings/network-configurations/{configuration_id}"
        ),
        CI_NETWORK_CONFIGURATION,
    )
    return [
        *_differs(
            "network configuration compute_service", configuration.get("compute_service"), "actions"
        ),
        *_differs(
            "network configuration network_settings_ids",
            configuration.get("network_settings_ids"),
            [github_id],
        ),
    ]


def runner_group_differences(
    ctx: Context,
    organization: str,
    group_id: Any,
    *,
    configuration_id: Any,
    repository_id: Any,
    public: bool,
) -> list[str]:
    path = f"/orgs/{organization}/actions/runner-groups/{group_id}"
    group = as_dict(ctx.tools.gh_json("api", path), CI_RUNNER_GROUP)
    repositories = listing(
        ctx.tools.gh_json("api", f"{path}/repositories?per_page=100"),
        "repositories",
        CI_RUNNER_GROUP,
    )
    return [
        *_differs("runner group visibility", group.get("visibility"), "selected"),
        *_differs(
            "runner group allows_public_repositories",
            group.get("allows_public_repositories"),
            public,
        ),
        *_differs(
            "runner group restricted_to_workflows", group.get("restricted_to_workflows"), False
        ),
        *_differs(
            "runner group network_configuration_id",
            str(group.get("network_configuration_id")),
            str(configuration_id),
        ),
        *_differs(
            "runner group repositories", [item.get("id") for item in repositories], [repository_id]
        ),
    ]


def hosted_runner_differences(
    ctx: Context, organization: str, runner_id: Any, group_id: Any
) -> list[str]:
    runner = as_dict(
        ctx.tools.gh_json("api", f"/orgs/{organization}/actions/hosted-runners/{runner_id}"),
        CI_RUNNER,
    )
    image = runner.get("image_details") or runner.get("image") or {}
    size = runner.get("machine_size_details") or {}
    return [
        *_differs("runner image", as_dict(image, CI_RUNNER).get("id"), CI_RUNNER_IMAGE),
        *_differs("runner size", as_dict(size, CI_RUNNER).get("id"), CI_RUNNER_SIZE),
        *_differs("runner runner_group_id", runner.get("runner_group_id"), group_id),
        *_differs("runner maximum_runners", runner.get("maximum_runners"), CI_MAX_RUNNERS),
        *_differs("runner public_ip_enabled", runner.get("public_ip_enabled"), False),
    ]


def step_checklist(ctx: Context) -> str:
    ctx.out.write("Post-provision steps (83-infrastructure.md section 11.5), in this order:\n")
    ctx.out.writelines(f"{line}\n" for line in POST_PROVISION_STEPS)
    return SUCCEEDED


STEPS: Final[tuple[tuple[str, Callable[[Context], str]], ...]] = (
    ("prerequisites", step_prerequisites),
    ("providers", step_providers),
    ("resource-groups", step_resource_groups),
    ("roles", step_roles),
    ("identity", step_identity),
    ("shared", step_shared),
    ("groups", step_groups),
    ("graph-permission", step_graph_permission),
    ("assignments", step_assignments),
    ("policies", step_policies),
    ("security", step_security),
    ("config", step_config),
    ("github", step_github),
    ("ci-runner", step_ci_runner),
    ("checklist", step_checklist),
)


def run_steps(ctx: Context, only: str | None) -> None:
    for name, step in STEPS:
        if only is None or name == only:
            status = step(ctx)
            report({"step": name, "status": status}, ctx.out, ctx.environ)


def main(argv: Sequence[str] | None = None, runtime: Runtime | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DocChat bootstrap (83-infrastructure.md section 11)."
    )
    parser.add_argument("--step", choices=[name for name, _ in STEPS], help="run this step alone")
    arguments = parser.parse_args(argv)
    return run_script(lambda ctx: run_steps(ctx, arguments.step), runtime or Runtime())


if __name__ == "__main__":
    sys.exit(main())
