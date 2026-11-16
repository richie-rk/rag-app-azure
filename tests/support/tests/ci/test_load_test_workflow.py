"""The load test's workflows (`84-testing.md` sections 7.4, 7.5 and 10.1; `83-infrastructure.md`
sections 10.2, 10.7, 10.14 and 10.15).

Checks what `load-test.yaml` must keep: `workflow_dispatch` with its four inputs and one run at a
time; each job's GitHub environment, identity, permissions and timeout; the token-holding jobs on
the test runner ending with the vault's always-run close; the config jobs' ops-config values; the
scenario steps in the order of section 7.5; the verdict and teardown jobs. Then the pieces of the
other workflows the load test relies on: `deploy-environment.yaml`'s load path, the smoke suite
in `load`, `nightly.yaml`'s `load-guard`, and the `lint` job's k6 steps.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
WORKFLOWS: Final = REPOSITORY_ROOT / ".github" / "workflows"
TEST_RUNNER: Final = (
    "${{ vars.DOCCHAT_CI_PRIVATE_NETWORKING == 'true' && "
    "fromJSON('{\"group\":\"docchat-ci-private\"}') || 'ubuntu-24.04' }}"
)
PINNED: Final = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")

TOKEN_JOBS: Final = ("seed", "s1", "s2", "s3", "s4-set1", "s4-set2")
DEPLOY_IDENTITY_JOBS: Final = (
    "seed-config",
    "stream-reset",
    "s4-set1-config",
    "s4-set2-config",
    "s4-reset",
    "teardown",
)
TIMEOUTS: Final = {
    "manifest": 10,
    "access-gate": 10,
    "seed-config": 20,
    "seed": 90,
    "stream-reset": 15,
    "s1": 60,
    "s2": 60,
    "s3": 100,
    "s4-set1-config": 15,
    "s4-set1": 350,
    "s4-set2-config": 15,
    "s4-set2": 350,
    "s4-reset": 15,
    "verdict": 10,
    "teardown": 60,
}


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def triggers(name: str) -> dict[str, Any]:
    """The `on:` block, which YAML 1.1 reads as the key `True`."""
    loaded: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    found: dict[str, Any] = loaded[True] if True in loaded else loaded["on"]
    return found


LOAD: Final = workflow("load-test.yaml")
JOBS: Final[dict[str, Any]] = LOAD["jobs"]


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = job.get("steps", [])
    return found


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in steps(job) if item.get("name") == name)


def position(job: dict[str, Any], name: str) -> int:
    return next(index for index, item in enumerate(steps(job)) if item.get("name") == name)


def runs(job: dict[str, Any]) -> str:
    return "\n".join(str(item.get("run", "")) for item in steps(job))


def logins(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in steps(job) if str(item.get("uses", "")).startswith("azure/login@")]


def needs(job: dict[str, Any]) -> list[str]:
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


# --- load-test.yaml ----------------------------------------------------------------------------


def test_the_load_test_is_dispatched_by_hand_one_run_at_a_time() -> None:
    dispatch = triggers("load-test.yaml")
    assert list(dispatch) == ["workflow_dispatch"]
    inputs = dispatch["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["deploy_run_id", "scenarios", "rate", "answer_tokens"]
    assert inputs["scenarios"]["default"] == "all"
    assert inputs["answer_tokens"]["default"] == 80
    assert inputs["rate"]["required"] is False
    assert LOAD["permissions"] == {}
    assert LOAD["concurrency"] == {"group": "docchat-load-test", "cancel-in-progress": False}


def test_the_jobs_are_section_7_4s_with_their_timeouts() -> None:
    assert set(JOBS) == {"create", "smoke"} | set(TIMEOUTS)
    for name, minutes in TIMEOUTS.items():
        assert JOBS[name]["timeout-minutes"] == minutes, name


def test_every_action_is_pinned_and_every_checkout_drops_its_credentials() -> None:
    for name, job in JOBS.items():
        for item in steps(job):
            uses = str(item.get("uses", ""))
            if uses:
                assert PINNED.match(uses), f"{name}: {uses}"
            if uses.startswith("actions/checkout@"):
                assert item["with"]["persist-credentials"] is False


@pytest.mark.parametrize("name", TOKEN_JOBS)
def test_token_jobs_run_in_load_tests_as_the_ci_test_identity(name: str) -> None:
    job = JOBS[name]
    assert job["environment"] == "load-tests"
    assert job["runs-on"] == TEST_RUNNER
    expected = {"contents": "read", "id-token": "write"}
    if name == "s4-set2":
        expected["actions"] = "read"
    assert job["permissions"] == expected
    [login] = logins(job)
    assert login["with"]["client-id"] == "${{ vars.DOCCHAT_CI_TEST_CLIENT_ID }}"
    close = steps(job)[-1]
    assert close["if"] == "${{ always() && vars.DOCCHAT_CI_PRIVATE_NETWORKING != 'true' }}"
    assert close["run"] == (
        "uv run --frozen python scripts/deploy/firewall.py close --target vault --env load"
    )


@pytest.mark.parametrize("name", DEPLOY_IDENTITY_JOBS)
def test_deploy_identity_jobs_run_in_load_and_hold_no_test_token(name: str) -> None:
    job = JOBS[name]
    assert job["environment"] == "load"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    [login] = logins(job)
    assert login["with"]["client-id"] == "${{ vars.AZURE_CLIENT_ID }}"
    assert " docchat-load " not in runs(job)
    assert "DOCCHAT_TEST_VAULT_URI" not in str(job)


def test_every_sign_in_is_followed_by_turning_off_dynamic_installs() -> None:
    for name, job in JOBS.items():
        for index, item in enumerate(steps(job)):
            if str(item.get("uses", "")).startswith("azure/login@"):
                assert steps(job)[index + 1]["run"] == (
                    "az config set extension.use_dynamic_install=no"
                ), name


def test_the_manifest_job_checks_the_deploy_run_without_signing_in() -> None:
    job = JOBS["manifest"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert not logins(job)
    assert 'scripts/ci/check_deploy_run.py "$DEPLOY_RUN_ID"' in runs(job)
    download = step(job, "Download the release manifest")["with"]
    assert download["name"] == "release-manifest"
    assert download["run-id"] == "${{ inputs.deploy_run_id }}"
    assert set(job["outputs"]) >= {"commit", "api_image", "ingestion_image", "model_stub_image"}
    assert "token_delay_ms" in job["outputs"]
    assert "delay=$(( 800 / ANSWER_TOKENS ))" in runs(job)


def test_create_deploys_the_release_with_a_salt_of_the_run() -> None:
    job = JOBS["create"]
    assert job["uses"] == "./.github/workflows/deploy-environment.yaml"
    assert job["with"]["environment"] == "load"
    assert job["with"]["mode"] == "full"
    assert job["with"]["resource_token_salt"] == "run-${{ github.run_id }}"
    assert job["with"]["model_stub_image"] == "${{ needs.manifest.outputs.model_stub_image }}"
    assert job["with"]["web_artifact_run_id"] == "${{ inputs.deploy_run_id }}"
    assert JOBS["access-gate"]["environment"] == "load-access"
    assert needs(JOBS["access-gate"]) == ["create"]


def test_seed_config_sets_the_five_load_values_with_the_run_as_ticket() -> None:
    job = JOBS["seed-config"]
    script = step(job, "Set the load ops config")["run"]
    values = dict(re.findall(r"set_value ([a-z_.]+) (\d+)", script))
    assert values == {
        "ratelimit.max_concurrent_streams": "200",
        "ratelimit.chat_requests_per_minute": "1200",
        "quota.tokens_per_day": "1000000000",
        "quota.tokens_per_month": "100000000000",
        "chat.max_turn_seconds": "220",
    }
    assert "docchat-ops --env load config set" in script
    assert job["env"]["TICKET"] == "${{ github.run_id }}"
    assert "applied_at=" in script
    assert step(job, "Close the SQL runner rule")["if"].startswith("${{ always()")
    assert "firewall.py sweep --target sql --env load" in runs(job)


def test_stream_reset_and_the_s4_configs_set_their_one_value() -> None:
    reset = JOBS["stream-reset"]
    assert reset["if"] == "${{ always() && needs.seed-config.result == 'success' }}"
    command = step(reset, "Reset the turn deadline")["run"]
    assert "--key chat.max_turn_seconds --value 180" in command
    assert '--reason "load stream test reset"' in command
    for name, value, reason in (
        ("s4-set1-config", "1", "load S4 set 1"),
        ("s4-set2-config", "4", "load S4 set 2"),
        ("s4-reset", "1", "load S4 reset"),
    ):
        job = JOBS[name]
        assert (job["env"]["PARALLEL_ITEMS"], job["env"]["REASON"]) == (value, reason)
        assert "--key ingestion.max_parallel_items" in runs(job)
    assert JOBS["s4-reset"]["if"] == "${{ always() && needs.s4-set2-config.result == 'success' }}"


def test_the_smoke_job_runs_the_smoke_suite_in_load() -> None:
    job = JOBS["smoke"]
    assert job["uses"] == "./.github/workflows/deployed-tests.yaml"
    assert job["with"] == {"environment": "load", "suite": "smoke"}


def test_the_seed_job_acquires_tokens_then_seeds_after_the_config() -> None:
    commands = runs(JOBS["seed"])
    assert (
        "docchat-load tokens --env load --aliases load-01..load-20,load-admin "
        '--out "$RUNNER_TEMP/tokens.json"'
    ) in commands
    assert 'docchat-load seed --tokens "$RUNNER_TEMP/tokens.json" --applied-at "$APPLIED_AT"' in (
        commands
    )
    assert JOBS["seed"]["env"]["APPLIED_AT"] == "${{ needs.seed-config.outputs.applied_at }}"


def test_the_scenarios_run_one_after_another_from_stream_reset() -> None:
    assert "stream-reset" in needs(JOBS["s1"])
    assert "s1" in needs(JOBS["s2"])
    assert "s2" in needs(JOBS["s3"])
    for name in ("s1", "s2", "s3"):
        assert f"needs.manifest.outputs.run_{name} == 'true'" in JOBS[name]["if"]
    assert "+ 70" in step(JOBS["s1"], "Wait for the ops config")["run"]
    assert JOBS["s1"]["env"]["APPLIED_AT"] == "${{ needs.stream-reset.outputs.applied_at }}"


@pytest.mark.parametrize(("name", "capacity"), [("s1", "1"), ("s2", "3")])
def test_s1_and_s2_set_their_capacity_and_wait_for_it(name: str, capacity: str) -> None:
    job = JOBS[name]
    setting = step(job, f"Set capacity {capacity}")
    assert setting["env"]["CAPACITY"] == capacity
    assert "az monitor autoscale update" in setting["run"]
    assert "az webapp list-instances" in setting["run"]
    assert "sleep 30" in setting["run"]
    assert "SECONDS + 900" in setting["run"]


def test_the_scenario_jobs_follow_the_run_flow_of_section_7_5() -> None:
    job = JOBS["s1"]
    order = [
        "Install k6 from the lock",
        "Read the environment's outputs",
        "Acquire the load users' tokens",
        "Set capacity 1",
        "Prepare the load generator",
        "Run the load generator",
        "Save the load generator's log",
        "Delete the load generator",
        "Compute the S1 verdict (T-591)",
        "Keep the scenario's results",
        "Restore the load profile",
    ]
    positions = [position(job, name) for name in order]
    assert positions == sorted(positions)
    prepare = step(job, "Prepare the load generator")["run"]
    assert prepare.index('k6" inspect') < prepare.index("docchat-load aci-params")
    for flag in ("--scenario", "--bootstrap tests/load/aci/run.py", "--lib tests/load/k6/lib.js"):
        assert flag in prepare
    generator = step(job, "Run the load generator")["run"]
    assert "--template-file tests/load/aci/loadgen.bicep" in generator
    assert 'rm -f "$RUNNER_TEMP/loadgen.parameters.json"' in generator
    assert "sleep 15" in generator
    assert "LENGTH_SECONDS + 900" in generator
    assert "containers[0].instanceView.currentState.startTime" in generator
    assert step(job, "Delete the load generator")["if"].startswith("${{ always()")
    assert "az container delete" in step(job, "Delete the load generator")["run"]
    restore = step(job, "Restore the load profile")
    assert "--min-count 1 --max-count 6 --count 1" in restore["run"]
    assert restore["if"].startswith("${{ always()")
    k6 = step(job, "Install k6 from the lock")["run"]
    assert "sha256sum --check --strict" in k6
    assert "tests/load/k6.lock.json" in k6


def test_s2_removes_one_instance_at_240_seconds_and_records_t_r() -> None:
    job = JOBS["s2"]
    script = step(job, "Run the load generator and remove one instance")["run"]
    assert "t0 + 240" in script
    assert "--min-count 2 --max-count 2 --count 2" in script
    assert "sleep 5" in script
    assert "REMOVED_AT=" in script
    verdict = step(job, "Compute the S2 verdict (T-592)")["run"]
    assert '--removed-at "$REMOVED_AT"' in verdict
    assert '--tokens "$RUNNER_TEMP/tokens.json"' in verdict


def test_s3_probes_calibrates_rates_and_runs_its_main_phase() -> None:
    job = JOBS["s3"]
    order = [
        "Send the alert probe's turns",
        "Set the load profile with 1 instance",
        "Run the calibration",
        "Set the main phase's rate",
        "Run the main phase",
        "Compute the S3 verdict (T-593)",
        "Check the alert probe (T-596)",
    ]
    positions = [position(job, name) for name in order]
    assert positions == sorted(positions)
    assert step(job, "Set the load profile with 1 instance")["env"] == {
        "CAPACITY": "1",
        "MAX_COUNT": "6",
    }
    rate = step(job, "Set the main phase's rate")["run"]
    assert "docchat-load s3-rate" in rate
    assert '--rate "$RATE_INPUT"' in rate
    main = step(job, "Prepare the main phase")
    assert main["env"]["DOCCHAT_LOAD_RATE"] == "${{ steps.rate.outputs.rate }}"
    assert main["if"] == "${{ !cancelled() && steps.rate.outputs.inconclusive == 'false' }}"
    assert job["env"]["DOCCHAT_LOAD_ANSWER_TOKENS"] == "${{ needs.manifest.outputs.answer_tokens }}"
    assert job["env"]["DOCCHAT_LOAD_TOKEN_DELAY_MS"] == (
        "${{ needs.manifest.outputs.token_delay_ms }}"
    )
    probe = step(job, "Check the alert probe (T-596)")
    assert probe["if"] == "${{ always() && steps.probe-send.outcome == 'success' }}"
    assert "--phase check" in probe["run"]


def test_the_s4_sets_build_their_corpus_and_ingest_after_70_seconds() -> None:
    for name, variant in (("s4-set1", "a"), ("s4-set2", "b")):
        job = JOBS[name]
        assert job["env"]["VARIANT"] == variant
        commands = runs(job)
        assert "+ 70" in commands
        assert 'docchat-load build-corpus --variant "$VARIANT" --count 200' in commands
        assert (
            'docchat-load ingest --env load --variant "$VARIANT" --index "load-ingest-${VARIANT}"'
            in (commands)
        )
        assert " docchat-load tokens" not in commands
    second = JOBS["s4-set2"]
    assert step(second, "Download set 1's results")["with"]["name"] == "load-results-s4-set1"
    assert "--set1-results load-results/s4-set1/" in runs(second)


def test_the_verdict_job_fails_unless_every_selected_verdict_passed() -> None:
    job = JOBS["verdict"]
    assert not logins(job)
    assert job["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert job["if"] == "${{ always() && needs.manifest.result == 'success' }}"
    script = step(job, "Judge the run")["run"]
    assert 'if [ "$status" != "passed" ]' in script
    assert "alert-probe" in script
    assert step(job, "Download the verdicts")["with"]["pattern"] == "load-results-*"


def test_teardown_always_runs_as_the_load_deploy_identity() -> None:
    job = JOBS["teardown"]
    assert job["if"] == "${{ always() && needs.manifest.result == 'success' }}"
    assert job["concurrency"] == {"group": "docchat-env-load", "cancel-in-progress": False}
    assert set(needs(job)) == set(JOBS) - {"teardown"}
    assert steps(job)[-1]["run"] == "uv run --frozen python scripts/deploy/teardown_load.py"


# --- The other workflows -----------------------------------------------------------------------


def test_deploy_environment_has_the_load_path_of_section_10_7() -> None:
    deploy = workflow("deploy-environment.yaml")
    call = triggers("deploy-environment.yaml")["workflow_call"]
    assert {"model_stub_image", "resource_token_salt"} <= set(call["inputs"])
    assert set(call["outputs"]) == {
        "api_base_url",
        "web_base_url",
        "web_client_id",
        "test_client_id",
        "api_scope",
    }
    job = deploy["jobs"]["deploy"]
    tag = step(job, "Tag the load resource group with the run (T-468)")
    assert "docchat-load-run=${GITHUB_RUN_ID}" in tag["run"]
    assert "docchat-load-created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" in tag["run"]
    assert "--operation Merge" in tag["run"]
    assert position(job, "Tag the load resource group with the run (T-468)") < position(
        job, "Provision (T-124)"
    )
    create = step(job, "Create the azd environment")["run"]
    assert 'azd env set DOCCHAT_MODEL_STUB_IMAGE "$MODEL_STUB_IMAGE"' in create
    assert 'azd env set DOCCHAT_RESOURCE_TOKEN_SALT "$RESOURCE_TOKEN_SALT"' in create
    assert "model-stub@sha256" in step(job, "Check the inputs")["run"]


def test_the_smoke_suite_reads_load_as_the_ci_test_identity() -> None:
    deployed = workflow("deployed-tests.yaml")
    endpoints = deployed["jobs"]["endpoints"]
    assert endpoints["environment"] == (
        "${{ inputs.environment == 'load' && 'load-tests' || 'dev' }}"
    )
    [login] = logins(endpoints)
    assert login["with"]["client-id"] == (
        "${{ inputs.environment == 'load' && vars.DOCCHAT_CI_TEST_CLIENT_ID"
        " || vars.AZURE_CLIENT_ID }}"
    )
    assert deployed["jobs"]["pytest"]["environment"] == (
        "${{ inputs.environment == 'load' && 'load-tests' || 'dev-tests' }}"
    )
    check = step(endpoints, "Check the inputs")["run"]
    assert "load runs the smoke suite only" in check
    # The load run ends like every load-tests job; dev's run skips the close.
    close = steps(endpoints)[-1]
    assert "firewall.py close --target vault --env load" in close["run"]
    assert close["if"].startswith("${{ always() && inputs.environment == 'load'")


def test_the_nightly_load_guard_spares_a_live_run_under_24_hours() -> None:
    job = workflow("nightly.yaml")["jobs"]["load-guard"]
    assert job["environment"] == "load"
    assert job["timeout-minutes"] == 60
    assert job["concurrency"] == {"group": "docchat-env-load", "cancel-in-progress": False}
    assert job["permissions"] == {"contents": "read", "id-token": "write", "actions": "read"}
    decide = step(job, "Decide whether the load environment is to be torn down")["run"]
    assert "21600" in decide
    assert "86400" in decide
    assert "queued|waiting|in_progress" in decide
    assert "gh run view" in decide
    teardown = steps(job)[-1]
    assert teardown["if"] == "${{ steps.decide.outputs.teardown == 'true' }}"
    assert teardown["run"] == "uv run --frozen python scripts/deploy/teardown_load.py"


def test_the_lint_job_checks_the_lock_inspects_and_self_tests_k6() -> None:
    job = workflow("ci.yaml")["jobs"]["lint"]
    commands = runs(job)
    assert "python scripts/ci/check_k6_lock.py" in commands
    assert "sha256sum --check --strict" in commands
    for scenario in ("s1-event-loop", "s2-stateless", "s3-calibrate", "s3-scaleout"):
        assert scenario in step(job, "Inspect each load scenario")["run"]
    selftest = step(job, "Run the k6 library self-test (T-581)")["run"]
    assert "scripts/ci/k6_selftest_server.py" in selftest
    assert "tests/load/k6/selftest.js" in selftest
    infra = workflow("ci.yaml")["jobs"]["infra-tests"]
    assert "az bicep build --file tests/load/aci/loadgen.bicep --outdir build/infra/load" in runs(
        infra
    )


# --- The load rows' steps (84-testing.md sections 5.14, 7.4 to 7.6 and 8.4) ---------------------


def named(job: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
    """The job's steps whose name carries `identifier`, in order."""
    return [item for item in steps(job) if identifier in str(item.get("name", ""))]


def verdict_command(job: str, identifier: str) -> str:
    (verdict,) = named(JOBS[job], identifier)
    return " ".join(str(verdict["run"]).split())


def test_t591_the_s1_job_computes_the_s1_verdict() -> None:
    assert "docchat-load verdict --scenario s1 " in verdict_command("s1", "T-591")


def test_t592_the_s2_job_computes_the_s2_verdict() -> None:
    assert "docchat-load verdict --scenario s2 " in verdict_command("s2", "T-592")


def test_t593_the_s3_job_computes_the_s3_verdict() -> None:
    assert "docchat-load verdict --scenario s3 " in verdict_command("s3", "T-593")


def test_t596_the_s3_job_checks_the_alert_probe_after_its_verdict() -> None:
    assert "docchat-load probe-alerts --phase check " in verdict_command("s3", "T-596")
    (s3,) = named(JOBS["s3"], "T-593")
    (probe,) = named(JOBS["s3"], "T-596")
    assert position(JOBS["s3"], str(s3["name"])) < position(JOBS["s3"], str(probe["name"]))


def test_t594_both_ingestion_sets_compute_their_verdict() -> None:
    (first,) = named(JOBS["s4-set1"], "T-594")
    (second,) = named(JOBS["s4-set2"], "T-594")
    assert "verdict --scenario s4 --set 1" in " ".join(str(first["run"]).split())
    second_command = " ".join(str(second["run"]).split())
    assert "verdict --scenario s4 --set 2" in second_command
    assert "--set1-results load-results/s4-set1/" in second_command


def test_t595_the_verdict_job_reads_s5_from_the_release_commits_python_tests_job() -> None:
    job = JOBS["verdict"]
    (s5,) = named(job, "T-595")
    command = str(s5["run"])
    assert s5["env"]["COMMIT"] == "${{ needs.manifest.outputs.commit }}"
    assert "commits/${COMMIT}/pulls" in command
    assert "actions/workflows/ci.yaml/runs?head_sha=${head}&event=pull_request" in command
    assert 'select(.name == "python-tests")' in command
    assert '[ "$conclusion" = "success" ]' in command
    assert "load-results/load-results-s5" in command
    names = [str(item.get("name")) for item in steps(job)]
    assert names.index(str(s5["name"])) < names.index("Judge the run")
    assert "expected=(s5)" in step(job, "Judge the run")["run"]


def test_t468_the_load_environment_is_tagged_before_provisioning_and_torn_down() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    (tag,) = named(deploy, "T-468")
    assert "inputs.environment == 'load'" in tag["if"]
    assert position(deploy, str(tag["name"])) < position(deploy, "Provision (T-124)")
    load = yaml.safe_load((REPOSITORY_ROOT / "infra" / "env" / "load.json").read_text("utf-8"))
    assert load["openAi"]["enabled"] is False
    (teardown,) = named(JOBS["teardown"], "T-468")
    assert teardown["run"] == "uv run --frozen python scripts/deploy/teardown_load.py"
    assert JOBS["teardown"]["if"] == "${{ always() && needs.manifest.result == 'success' }}"
