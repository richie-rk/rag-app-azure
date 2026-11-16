"""The release wiring of the workflows (stage 6 of `90-build-order.md`).

`deploy.yaml`'s `prod` jobs (`83-infrastructure.md` section 10.6): `deploy-prod` behind the `prod`
reviewers after `test-dev` or on a dispatch, `test-prod` running the anonymous `prod_smoke` suite
with `deploy-prod`'s public values, and `tag-prod` writing the `deploy-prod-*` tag through the
REST API. `deploy-environment.yaml`'s `rollback` input and `prod` path (section 10.7): the
release-ordering check (section 10.11), the public outputs, and the post-deploy checks.
`deployed-tests.yaml`'s `prod` inputs and `prod-smoke` job (`84-testing.md` section 6.3), and
`nightly.yaml`'s `trivy-rescan` (`83-infrastructure.md` section 10.15; `84-testing.md` section
10.1). The registry jobs' runner rules, which no job sweeps (`83-infrastructure.md` section 4.6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
WORKFLOWS: Final = REPOSITORY_ROOT / ".github" / "workflows"
PUBLIC_OUTPUTS: Final = (
    "api_base_url",
    "web_base_url",
    "web_client_id",
    "test_client_id",
    "api_scope",
)
PROD_INPUTS: Final = ("api_base_url", "web_base_url", "web_client_id", "api_scope")


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def triggers(name: str) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean true.
    document: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    loaded: dict[str, Any] = document[True]
    return loaded


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = job.get("steps", [])
    return found


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in steps(job) if item.get("name") == name)


def position(job: dict[str, Any], name: str) -> int:
    return next(index for index, item in enumerate(steps(job)) if item.get("name") == name)


def script(item: dict[str, Any]) -> str:
    return " ".join(str(item.get("run", "")).split())


def test_a_dispatch_deploys_dev_or_prod_and_may_roll_back() -> None:
    inputs = triggers("deploy.yaml")["workflow_dispatch"]["inputs"]

    assert inputs["environment"]["options"] == ["dev", "prod"]
    assert inputs["rollback"] == {
        "description": inputs["rollback"]["description"],
        "type": "boolean",
        "default": False,
    }


def test_deploy_prod_follows_test_dev_or_a_prod_dispatch_and_waits_for_the_reviewers() -> None:
    jobs = workflow("deploy.yaml")["jobs"]
    job = jobs["deploy-prod"]

    assert job["uses"] == "./.github/workflows/deploy-environment.yaml"
    assert set(job["needs"]) == {"publish", "resolve", "resolve-web", "test-dev"}
    assert job["if"] == (
        "${{ !cancelled() && !failure() && "
        "(github.event_name == 'push' || inputs.environment == 'prod') }}"
    )
    assert job["permissions"] == {"contents": "read", "id-token": "write", "actions": "read"}
    called = job["with"]
    assert called["environment"] == "prod"
    assert called["web_artifact_run_id"] == "${{ github.run_id }}"
    assert called["mode"] == "${{ inputs.rollback && 'images-only' || 'full' }}"
    assert called["rollback"] == "${{ inputs.rollback == true }}"
    for image in ("api_image", "ingestion_image"):
        assert called[image] == jobs["deploy-dev"]["with"][image]
    # The called job's environment is the input, so prod's protection rules gate every mode.
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    assert deploy["environment"] == "${{ inputs.environment }}"
    assert deploy["concurrency"]["group"] == "docchat-env-${{ inputs.environment }}"


def test_deploy_dev_passes_the_rollback_on() -> None:
    called = workflow("deploy.yaml")["jobs"]["deploy-dev"]["with"]

    assert called["rollback"] == "${{ inputs.rollback == true }}"
    assert called["mode"] == "${{ inputs.rollback && 'images-only' || 'full' }}"


def test_test_prod_runs_the_prod_smoke_suite_with_deploy_prods_public_values() -> None:
    job = workflow("deploy.yaml")["jobs"]["test-prod"]

    assert job["uses"] == "./.github/workflows/deployed-tests.yaml"
    assert job["needs"] == "deploy-prod"
    # A run the release-ordering check stopped published no endpoints and tests nothing.
    assert job["if"] == "needs.deploy-prod.outputs.api_base_url != ''"
    assert job["with"]["environment"] == "prod"
    assert job["with"]["suite"] == "prod_smoke"
    for key in PROD_INPUTS:
        assert job["with"][key] == f"${{{{ needs.deploy-prod.outputs.{key} }}}}"


def test_tag_prod_writes_the_annotated_tag_through_the_api_with_no_azure_sign_in() -> None:
    job = workflow("deploy.yaml")["jobs"]["tag-prod"]

    assert set(job["needs"]) == {"deploy-prod", "test-prod"}
    assert job["if"] == "needs.deploy-prod.outputs.api_base_url != ''"
    assert job["permissions"] == {"contents": "write"}
    assert job["timeout-minutes"] == 10
    assert job["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert job["env"]["DEPLOYED_COMMIT"] == "${{ inputs.image_commit || github.sha }}"
    assert step(job, "Check out the repository")["with"]["persist-credentials"] is False
    command = script(step(job, "Tag the deployed commit"))
    assert 'tag="deploy-prod-$(date -u +%Y%m%d-%H%M)"' in command
    assert 'gh api "repos/{owner}/{repo}/git/tags"' in command
    assert '-f message="prod deploy of ${DEPLOYED_COMMIT} by run ${GITHUB_RUN_ID}"' in command
    assert "-f type=commit" in command
    assert 'gh api "repos/{owner}/{repo}/git/refs" -f ref="refs/tags/${tag}" -f sha="$object"' in (
        command
    )
    assert not any("azure/login" in str(item.get("uses", "")) for item in steps(job))


def test_deploy_environment_takes_a_rollback_and_publishes_the_public_values() -> None:
    call = triggers("deploy-environment.yaml")["workflow_call"]
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]

    assert call["inputs"]["rollback"]["type"] == "boolean"
    assert call["inputs"]["rollback"]["default"] is False
    assert set(call["outputs"]) == set(PUBLIC_OUTPUTS)
    for key in PUBLIC_OUTPUTS:
        assert call["outputs"][key]["value"] == f"${{{{ jobs.deploy.outputs.{key} }}}}"
        assert deploy["outputs"][key] == f"${{{{ steps.public.outputs.{key} }}}}"
    assert deploy["env"]["ROLLBACK"] == "${{ inputs.rollback == true }}"
    publish = step(deploy, "Publish the environment's public values")
    for variable in (
        "API_BASE_URL",
        "WEB_BASE_URL",
        "WEB_CLIENT_ID",
        "TEST_CLIENT_ID",
        "API_SCOPE",
    ):
        assert f"$(value {variable})" in publish["run"]
    # The dispatch still offers only the recovery modes.
    dispatch = triggers("deploy-environment.yaml")["workflow_dispatch"]["inputs"]
    assert dispatch["mode"]["options"] == ["restart-ingestion", "reset-task-hub"]


def test_the_deploy_job_accepts_dev_and_prod() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    check = step(deploy, "Check the inputs")["run"]

    assert "dev|prod) ;;" in check
    assert 'echo "::error::unknown environment ${DEPLOY_ENV}"' in check


def test_the_release_ordering_check_runs_in_prod_before_provisioning() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    ordering = step(deploy, "Check the release order")
    command = script(ordering)

    assert ordering["id"] == "ordering"
    assert ordering["if"] == "${{ env.DEPLOYS_IMAGES == 'true' }}"
    # dev and a rollback skip it.
    assert 'if [ "$DEPLOY_ENV" != "prod" ] || [ "$ROLLBACK" = "true" ]; then' in command
    assert "--tag azd-service-name=api" in command
    assert "'[0].tags.\"docchat-release-sha\"'" in command
    assert 'git merge-base --is-ancestor "$RELEASE_SHA" "$deployed"' in command
    assert "a newer release is already in prod" in command
    order = ["Run the preflight (T-434)", "Check the release order", "Provision (T-124)"]
    assert [position(deploy, name) for name in order] == sorted(
        position(deploy, name) for name in order
    )


def test_nothing_deploys_when_a_newer_release_is_in_prod() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    gated = [
        "Provision (T-124)",
        "Deploy the API image",
        "Deploy the ingestion image",
        "Refresh the environment's outputs",
        "Publish the environment's public values",
        "Compare each site's image with the release manifest (T-453)",
        "Deploy the web client",
    ]

    for name in gated:
        assert "steps.ordering.outputs.deploy == 'true'" in step(deploy, name)["if"], name
    assert step(deploy, "Provision (T-124)")["if"] == (
        "${{ inputs.mode == 'full' && steps.ordering.outputs.deploy == 'true' }}"
    )


def test_the_post_deploy_checks_run_whatever_an_earlier_check_found() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    for name in (
        "Run the prod post-deploy checks (T-452, T-023)",
        "Check prod's Azure OpenAI deployments and content filters (T-436)",
        "app-settings-check",
    ):
        condition = step(deploy, name)["if"]
        assert condition.startswith("${{ !cancelled() && "), name
        assert "steps.web.outcome == 'success'" in condition, name


def test_deployed_tests_takes_prods_public_values() -> None:
    for trigger in ("workflow_call", "workflow_dispatch"):
        inputs = triggers("deployed-tests.yaml")[trigger]["inputs"]
        for key in PROD_INPUTS:
            assert inputs[key]["type"] == "string", (trigger, key)
            assert inputs[key]["default"] == "", (trigger, key)
    dispatch = triggers("deployed-tests.yaml")["workflow_dispatch"]["inputs"]
    assert dispatch["environment"]["options"] == ["dev", "prod"]
    assert "prod_smoke" in dispatch["suite"]["options"]


def test_prod_smoke_runs_with_no_environment_and_no_token() -> None:
    jobs = workflow("deployed-tests.yaml")["jobs"]
    job = jobs["prod-smoke"]

    assert job["if"] == "inputs.environment == 'prod'"
    assert "environment" not in job
    assert "needs" not in job
    assert job["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] == 15
    for key in PROD_INPUTS:
        assert job["env"][key.upper()] == f"${{{{ inputs.{key} }}}}"
    first = steps(job)[0]
    assert first["name"] == "Check the inputs"
    assert 'if [ "$SUITE" != "prod_smoke" ]; then' in first["run"]
    assert (
        script(steps(job)[-1]) == "uv run --frozen pytest tests/deployed --deployed -m prod_smoke"
    )
    assert not any("azure/login" in str(item.get("uses", "")) for item in steps(job))
    # No job that signs in runs for prod.
    assert jobs["endpoints"]["if"] == "inputs.environment != 'prod'"
    endpoints_check = step(jobs["endpoints"], "Check the inputs")["run"]
    assert 'prod_smoke) echo "::error::prod_smoke runs only in prod"; exit 1 ;;' in endpoints_check


def test_trivy_rescan_scans_dev_and_prod_inside_the_registry_rule() -> None:
    job = workflow("nightly.yaml")["jobs"]["trivy-rescan"]

    assert job["environment"] == "dev"
    assert job["concurrency"] == {
        "group": "docchat-registry",
        "cancel-in-progress": False,
        "queue": "max",
    }
    assert job["timeout-minutes"] == 60
    assert job["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "actions": "read",
        "issues": "write",
    }
    assert step(job, "Check out the repository with its tags")["with"]["fetch-depth"] == 0
    dev = script(step(job, "Find the images dev runs"))
    assert "gh run list --workflow deploy.yaml" in dev
    assert 'select(.name | startswith("deploy-dev")) | .conclusion' in dev
    assert 'gh run download "$run_id" --name release-manifest' in dev
    prod = script(step(job, "Find the images prod runs"))
    assert "'refs/tags/deploy-prod-*'" in prod
    assert '--name "docchat/${image}:${commit}" --query digest' in prod
    # Either set failing still lets the other be scanned.
    for name in ("Find the images dev runs", "Find the images prod runs"):
        assert step(job, name)["continue-on-error"] is True
    order = [
        "Open the registry runner rule",
        "Find the images prod runs",
        "Pull the deployed images",
        "Close the registry runner rule",
        "Scan the deployed images (T-525)",
        "Gate the findings (T-525)",
        "Record each finding as an issue",
        "Fail on a missing set or a finding",
    ]
    assert [position(job, name) for name in order] == sorted(position(job, name) for name in order)
    close = step(job, "Close the registry runner rule")
    assert close["if"] == "${{ always() && steps.open.outcome != 'skipped' }}"
    assert script(close) == (
        "uv run --frozen python scripts/deploy/firewall.py close --target registry --env dev"
    )
    fail = step(job, "Fail on a missing set or a finding")["if"]
    for outcome in ("steps.dev-set.outcome", "steps.prod-set.outcome", "steps.gate.outcome"):
        assert f"{outcome} == 'failure'" in fail


def test_registry_jobs_open_and_close_their_own_rule_and_sweep_nothing() -> None:
    """`83-infrastructure.md` section 4.6 items 1 and 3: no job holds the shared allowlist, so
    `publish`, `resolve` and `trivy-rescan` only open and close their runner's rule, and leftover
    registry and test-vault addresses go to runbook R26."""
    registry_jobs = [
        workflow("deploy.yaml")["jobs"]["publish"],
        workflow("deploy.yaml")["jobs"]["resolve"],
        workflow("nightly.yaml")["jobs"]["trivy-rescan"],
    ]
    for job in registry_jobs:
        commands = [script(item) for item in steps(job) if "firewall.py" in script(item)]
        assert commands == [
            "uv run --frozen python scripts/deploy/firewall.py open --target registry --env dev",
            "uv run --frozen python scripts/deploy/firewall.py close --target registry --env dev",
        ]
        assert step(job, "Open the registry runner rule")["id"] == "open"
        close = step(job, "Close the registry runner rule")
        assert close["if"] == "${{ always() && steps.open.outcome != 'skipped' }}"
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "sweep --target registry" not in text, path.name
        assert "sweep --target vault" not in text, path.name
        assert "DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST" not in text, path.name
