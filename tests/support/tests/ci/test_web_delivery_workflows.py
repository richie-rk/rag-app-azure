"""The web delivery wiring of the workflows (stage 5 of `90-build-order.md`).

Checks what the workflows must keep for the web client: the credential-free jobs build `web-dist`
and the `swa-cli` archive, the deploy job uploads that run's artifacts with `deploy_web.py` after
the API (`83-infrastructure.md` sections 10.6, 10.7 and 10.10), the `security` job audits every npm
package the gate expects a report of (`84-testing.md` section 9.2), `web-deployed` runs with no
identity (`84-testing.md` section 6.3), no job that can mint an Azure token installs or runs npm
(`83-infrastructure.md` section 10.2), no step handles the Static Web Apps token, and the ruleset
requires every `ci.yaml` job (`01-repository.md` section 2.6).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
WORKFLOWS: Final = REPOSITORY_ROOT / ".github" / "workflows"

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(REPOSITORY_ROOT / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import security_gate  # noqa: E402

NPM_INSTALL_OR_RUN: Final = re.compile(
    r"\bnpm\s+(?:ci|install|i|run)\b|\bdocker\s+(?:buildx\s+)?build\b"
)


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = job.get("steps", [])
    return found


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in steps(job) if item.get("name") == name)


def position(job: dict[str, Any], name: str) -> int:
    return next(index for index, item in enumerate(steps(job)) if item.get("name") == name)


def uploads(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["with"]["name"]: item["with"]
        for item in steps(job)
        if str(item.get("uses", "")).startswith("actions/upload-artifact@")
    }


def test_the_security_job_audits_every_npm_package_the_gate_expects() -> None:
    audit = step(workflow("ci.yaml")["jobs"]["security"], "Audit the npm packages")["run"]
    packages = re.search(r"for package in ([^;\n]+)", audit)
    assert packages is not None
    directories = packages.group(1).split()

    assert sorted(directories) == sorted(security_gate.NPM_DIRECTORIES.values())
    # The report is named after the package's last path component, the gate's key.
    assert "npm-${package##*/}.json" in audit
    assert {d.rsplit("/", 1)[-1]: d for d in directories} == security_gate.NPM_DIRECTORIES
    assert "npm audit --json" in audit
    assert "|| test $? -eq 1" in audit


def test_the_web_job_builds_tests_and_keeps_the_unconfigured_build() -> None:
    job = workflow("ci.yaml")["jobs"]["web"]
    commands = [item.get("run", "") for item in steps(job)]

    assert job["timeout-minutes"] == 20
    assert job["permissions"] == {"contents": "read"}
    assert job["defaults"]["run"]["working-directory"] == "apps/web"
    for command in (
        "npm ci",
        "npm run lint",
        "npm run typecheck",
        "npm run format:check",
        "npm test",
        "npm run build",
        "npm run generate:api",
    ):
        assert command in commands, command
    assert any("git diff --exit-code -- src/api/generated" in command for command in commands)
    assert uploads(job)["web-dist"]["path"] == "apps/web/dist/"


def test_the_credential_free_jobs_hand_over_web_dist_and_the_cli_archive() -> None:
    jobs = workflow("deploy.yaml")["jobs"]

    for name in ("build", "resolve-web"):
        job = jobs[name]
        assert job["permissions"] == {"contents": "read"}, name
        artifacts = uploads(job)
        assert artifacts["web-dist"]["path"] == "apps/web/dist/"
        assert artifacts["swa-cli"]["path"] == "build/swa-cli/swa-cli.tgz"
        assert {artifacts[a]["retention-days"] for a in ("web-dist", "swa-cli")} == {90}
        pack = step(job, "Pack the pinned Static Web Apps CLI")["run"]
        assert "npm ci --ignore-scripts --prefix scripts/deploy/swa-cli" in pack
        assert "tar -czf build/swa-cli/swa-cli.tgz -C scripts/deploy swa-cli" in pack
    assert jobs["resolve-web"]["needs"] == "resolve"
    assert step(jobs["resolve-web"], "Check out the image commit")["with"]["ref"] == (
        "${{ inputs.image_commit }}"
    )
    deploy_dev = jobs["deploy-dev"]
    assert "resolve-web" in deploy_dev["needs"]
    assert deploy_dev["with"]["web_artifact_run_id"] == "${{ github.run_id }}"


def test_the_deploy_job_uploads_the_runs_web_build_after_the_api() -> None:
    job = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    order = [
        "Deploy the API image",
        "Deploy the ingestion image",
        "Refresh the environment's outputs",
        "Download the web build",
        "Download the Static Web Apps CLI archive",
        "Deploy the web client",
        "app-settings-check",
    ]

    assert [position(job, name) for name in order] == sorted(position(job, name) for name in order)
    for name, artifact in (
        ("Download the web build", "web-dist"),
        ("Download the Static Web Apps CLI archive", "swa-cli"),
    ):
        download = step(job, name)
        assert download["with"]["name"] == artifact
        assert download["with"]["run-id"] == "${{ inputs.web_artifact_run_id }}"
        assert download["with"]["github-token"] == "${{ github.token }}"
    command = " ".join(step(job, "Deploy the web client")["run"].split())
    assert command == (
        'uv run --frozen python scripts/deploy/deploy_web.py --env "$DEPLOY_ENV" '
        '--dist "$RUNNER_TEMP/web-dist" --swa-cli "$RUNNER_TEMP/swa-cli/swa-cli.tgz"'
    )
    assert job["permissions"]["actions"] == "read"


def test_web_deployed_runs_with_no_identity_after_dev_smoke_and_in_prod_smoke() -> None:
    job = workflow("deployed-tests.yaml")["jobs"]["web-deployed"]
    condition = " ".join(job["if"].split())

    assert "environment" not in job
    assert job["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] == 20
    assert set(job["needs"]) == {"endpoints", "prod-smoke"}
    assert (
        "(inputs.environment == 'dev' && inputs.suite == 'smoke' "
        "&& needs.endpoints.result == 'success')"
    ) in condition
    assert (
        "(inputs.environment == 'prod' && inputs.suite == 'prod_smoke' "
        "&& needs.prod-smoke.result == 'success')"
    ) in condition
    assert set(job["env"]) == {
        "WEB_BASE_URL",
        "API_BASE_URL",
        "WEB_CLIENT_ID",
        "API_SCOPE",
        "AZURE_TENANT_ID",
    }
    # prod's values are the workflow's inputs (no job here signs in to prod); dev's are the
    # endpoints job's outputs.
    for variable, key in (
        ("WEB_BASE_URL", "web_base_url"),
        ("API_BASE_URL", "api_base_url"),
        ("WEB_CLIENT_ID", "web_client_id"),
        ("API_SCOPE", "api_scope"),
    ):
        assert job["env"][variable] == (
            f"${{{{ inputs.environment == 'prod' && inputs.{key} "
            f"|| needs.endpoints.outputs.{key} }}}}"
        )
    assert job["env"]["AZURE_TENANT_ID"] == "${{ vars.AZURE_TENANT_ID }}"
    commands = [item.get("run", "") for item in steps(job)]
    assert "npm ci" in commands
    assert any("playwright test specs/web-deployed/ --project chromium" in c for c in commands)


def test_no_job_that_can_mint_an_azure_token_installs_or_runs_npm() -> None:
    offenders = [
        f"{path.name}:{name}"
        for path in sorted(WORKFLOWS.glob("*.yaml"))
        for name, job in workflow(path.name)["jobs"].items()
        if (job.get("permissions") or {}).get("id-token") == "write"
        and any(NPM_INSTALL_OR_RUN.search(item.get("run", "")) for item in steps(job))
    ]

    assert offenders == []


def test_no_workflow_step_handles_the_static_web_apps_token() -> None:
    # deploy_web.py alone reads the token, masks it and hands it to the CLI's environment.
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        for needle in ("staticwebapp secrets", "SWA_CLI_DEPLOYMENT_TOKEN", "--deployment-token"):
            assert needle not in text, f"{path.name} names {needle}"


def test_the_ruleset_requires_every_ci_job() -> None:
    ruleset = json.loads(
        (REPOSITORY_ROOT / ".github" / "rulesets" / "main.json").read_text(encoding="utf-8")
    )
    (checks,) = [
        rule["parameters"]["required_status_checks"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
    ]
    required = {check["context"] for check in checks}

    assert set(workflow("ci.yaml")["jobs"]) <= required
    languages = workflow("codeql.yaml")["jobs"]["analyze"]["strategy"]["matrix"]["language"]
    assert {f"analyze ({language})" for language in languages} <= required
