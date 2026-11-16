"""T-456, the workflow-rules check (`84-testing.md` section 5.9; `83-infrastructure.md` section
10.2), over every file of `.github/workflows/`.

- Top-level `permissions: {}`; no `pull_request_target`; every action pinned to a 40-character
  commit SHA with its release tag as a trailing comment (the repository's own reusable workflows
  excepted); `actions/checkout` with `persist-credentials: false`.
- Every job sets `timeout-minutes`, except a reusable-workflow call, which GitHub doesn't let set
  one (the called jobs carry theirs).
- `id-token: write` only in a job that signs in to Azure or attests a build, or a call that
  passes it to such jobs; no job holding it runs `npm ci`, `npm install`, `npm run` or a Docker
  build.
- Every job that signs in to Azure names one client-ID variable and one GitHub environment (the
  pull-request identity's `preflight-whatif` excepted: its federated credential trusts the
  `pull_request` subject, section 10.5) and runs `az config set extension.use_dynamic_install=no`.
- Only `dev-tests` and `load-tests` jobs read `DOCCHAT_CI_TEST_CLIENT_ID`, each on the test
  runner's expression and ending with `firewall.py close --target vault` under `if: always()`;
  only they name the vault target, every `firewall.py` target is `sql`, `registry` or `vault`,
  and no other job names `docchat-ci-private`.
- A job that serves dev and load by `inputs.environment` is held to these rules as each of the
  two jobs it runs as.
- `deploy-environment.yaml`'s dispatched `mode` is a choice list without `full`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
WORKFLOWS: Final = REPOSITORY_ROOT / ".github" / "workflows"
FILES: Final = sorted(path.name for path in WORKFLOWS.glob("*.yaml"))
PINNED: Final = re.compile(r"^\s*(?:-\s+)?uses:\s+([^\s@]+)@([0-9a-f]{40})\s+#\s+v\S+\s*$")
USES_LINE: Final = re.compile(r"^\s*(?:-\s+)?uses:\s+(\S+)")
NPM_OR_DOCKER_BUILD: Final = re.compile(
    r"\bnpm\s+(?:ci|install|i|run)\b|\bdocker\s+(?:buildx\s+)?build\b"
)
FIREWALL_TARGET: Final = re.compile(r"firewall\.py\s+\w+\s+--target\s+(\S+)")
CLIENT_IDS: Final = frozenset(
    {
        "${{ vars.AZURE_CLIENT_ID }}",
        "${{ vars.DOCCHAT_CI_TEST_CLIENT_ID }}",
        "${{ vars.DOCCHAT_PR_CLIENT_ID }}",
    }
)
TEST_RUNNER: Final = (
    "${{ vars.DOCCHAT_CI_PRIVATE_NETWORKING == 'true' && "
    "fromJSON('{\"group\":\"docchat-ci-private\"}') || 'ubuntu-24.04' }}"
)
TEST_ENVIRONMENTS: Final = frozenset({"dev-tests", "load-tests"})
# A job that serves both dev and load (`deployed-tests.yaml`'s `endpoints` and `pytest`,
# `84-testing.md` section 6.3) picks its GitHub environment, identity and runner with
# `${{ inputs.environment == 'load' && <load's> || <dev's> }}` and gates its load-only steps on
# `inputs.environment == 'load'`; every rule holds for the job as it runs in each of the two.
SELECTOR: Final = re.compile(r"\$\{\{ inputs\.environment == 'load' && (.+) \|\| (.+?) \}\}")
LOAD_ONLY: Final = "inputs.environment == 'load'"


def document(name: str) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def operand(text: str) -> str:
    """One side of a selector as the plain value or the expression it stands for."""
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return f"${{{{ {text} }}}}"


def selected(value: Any, *, load: bool) -> Any:
    """`value` with every selector replaced by the side `load` picks."""
    if isinstance(value, dict):
        return {key: selected(item, load=load) for key, item in value.items()}
    if isinstance(value, list):
        return [selected(item, load=load) for item in value]
    if isinstance(value, str) and (match := SELECTOR.fullmatch(value)):
        return operand(match.group(1 if load else 2))
    return value


def as_runs(job_name: str, job: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The job as each environment runs it: itself, or its load and dev runs if it selects."""
    load = selected(job, load=True)
    if load == job:
        return [(job_name, job)]
    dev = selected(job, load=False)
    dev["steps"] = [item for item in steps(dev) if LOAD_ONLY not in str(item.get("if", ""))]
    return [(f"{job_name} (load)", load), (f"{job_name} (dev)", dev)]


def jobs() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (name, run_name, run)
        for name in FILES
        for job_name, job in (document(name)["jobs"] or {}).items()
        for run_name, run in as_runs(job_name, job)
    ]


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = job.get("steps", [])
    return found


def runs(job: dict[str, Any]) -> str:
    return "\n".join(str(item.get("run", "")) for item in steps(job))


def logins(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in steps(job) if str(item.get("uses", "")).startswith("azure/login@")]


def job_text(job: dict[str, Any]) -> str:
    return str(yaml.safe_dump(job))


@pytest.mark.parametrize("name", FILES)
def test_t456_top_level_permissions_are_empty_and_no_pull_request_target(name: str) -> None:
    workflow = document(name)
    assert workflow["permissions"] == {}
    # PyYAML reads the bare key `on` as the boolean true.
    assert "pull_request_target" not in workflow[True]


@pytest.mark.parametrize("name", FILES)
def test_t456_every_action_is_pinned_to_a_commit_with_its_tag(name: str) -> None:
    for line in (WORKFLOWS / name).read_text(encoding="utf-8").splitlines():
        used = USES_LINE.match(line)
        if used is None or used.group(1).startswith("./.github/workflows/"):
            continue
        assert PINNED.match(line), f"{name}: {line.strip()}"


def test_t456_checkout_never_keeps_the_token() -> None:
    for name, job_name, job in jobs():
        for item in steps(job):
            if str(item.get("uses", "")).startswith("actions/checkout@"):
                assert item["with"]["persist-credentials"] is False, (name, job_name)


def test_t456_every_job_sets_its_timeout() -> None:
    for name, job_name, job in jobs():
        if "uses" in job:
            assert "timeout-minutes" not in job, (name, job_name)
        else:
            assert isinstance(job.get("timeout-minutes"), int | str), (name, job_name)


def test_t456_only_sign_in_and_attestation_jobs_hold_an_oidc_token() -> None:
    for name, job_name, job in jobs():
        if (job.get("permissions") or {}).get("id-token") != "write":
            continue
        assert not NPM_OR_DOCKER_BUILD.search(runs(job)), (name, job_name)
        if "uses" in job:
            continue
        attests = any(
            str(item.get("uses", "")).startswith("actions/attest-build-provenance@")
            for item in steps(job)
        )
        assert logins(job) or attests, (name, job_name)


def test_t456_each_azure_sign_in_names_one_identity_and_environment() -> None:
    for name, job_name, job in jobs():
        signed_in = logins(job)
        if not signed_in:
            continue
        client_ids = {str(item["with"]["client-id"]) for item in signed_in}
        assert len(client_ids) == 1, (name, job_name)
        assert client_ids <= CLIENT_IDS, (name, job_name)
        if client_ids != {"${{ vars.DOCCHAT_PR_CLIENT_ID }}"}:
            assert isinstance(job.get("environment"), str), (name, job_name)
        assert "az config set extension.use_dynamic_install=no" in runs(job), (name, job_name)


def test_t456_only_test_jobs_read_the_test_vault() -> None:
    for name, job_name, job in jobs():
        text = job_text(job)
        is_test_job = job.get("environment") in TEST_ENVIRONMENTS
        if "DOCCHAT_CI_TEST_CLIENT_ID" in text or "--target vault" in runs(job):
            assert is_test_job, (name, job_name)
        if "docchat-ci-private" in text:
            assert is_test_job, (name, job_name)
        if not is_test_job:
            continue
        assert job["runs-on"] == TEST_RUNNER, (name, job_name)
        last = steps(job)[-1]
        assert re.search(r"firewall\.py close --target vault", str(last.get("run", ""))), (
            name,
            job_name,
        )
        assert str(last.get("if", "")).startswith("${{ always()"), (name, job_name)


def test_t456_every_firewall_target_is_sql_registry_or_vault() -> None:
    for name, job_name, job in jobs():
        for target in FIREWALL_TARGET.findall(runs(job)):
            assert target in {"sql", "registry", "vault"}, (name, job_name, target)


def test_t456_a_dispatch_of_deploy_environment_never_deploys_an_image() -> None:
    mode = document("deploy-environment.yaml")[True]["workflow_dispatch"]["inputs"]["mode"]
    assert mode["type"] == "choice"
    assert "full" not in mode["options"]
    assert "images-only" not in mode["options"]
