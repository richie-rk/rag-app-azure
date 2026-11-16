"""The workflow steps that carry the CI-check, nightly and delivery-check rows T-023, T-042, T-124,
T-194, T-348, T-421, T-423, T-431, T-432, T-434, T-436, T-437, T-445, T-452, T-453, T-454, T-472,
T-475, T-512, T-513, T-519, T-520, T-523, T-525, T-526, T-529 and T-545 (`84-testing.md` sections
5.1 item 3, 8.1, 8.3, 9.2 and 10.1; `83-infrastructure.md` sections 10.5 to 10.9 and 10.15). The
load rows' steps are held in `test_load_test_workflow.py`.

A CI step that implements a check names the identifier in its `name:`; these tests hold each
named step to what its row proves, and every identifier a step name carries to a catalogue row of
a workflow level (`CI check`, `nightly`, `deploy check`, `load` and the S5 verdict, or a deployed
row a workflow runs as a step).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]

from docchat_testing.plugin import read_catalogue

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
WORKFLOWS: Final = REPOSITORY_ROOT / ".github" / "workflows"
STEP_IDENTIFIER: Final = re.compile(r"\bT-(\d{3})\b")
WORKFLOW_LEVELS: Final = (
    "CI check",
    "nightly",
    "deploy check",
    "load",
    "integration (release commit)",
    "deployed (each push to `main`)",
    "deployed (deploy check)",
    "deployed (after each deploy)",
    "deployed (after each web deploy)",
    # The deployed rows that steps of the deploy job run (84-testing.md section 8.3).
    "deployed (prod deploy check)",
    "deployed (`prod` deploy check)",
    "deployed (`infra` suite and `prod` deploy)",
    "deployed (after each provision)",
    "deployed (after a second provision)",
)
IMAGES: Final = (
    ("api", "apps/api/Dockerfile"),
    ("ingestion", "apps/ingestion/Dockerfile"),
    ("model-stub", "tests/model-stub/Dockerfile"),
)


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = job.get("steps", [])
    return found


def named(job: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
    """The job's steps whose name carries `identifier`, in order."""
    return [item for item in steps(job) if identifier in str(item.get("name", ""))]


def script(item: dict[str, Any]) -> str:
    return " ".join(str(item.get("run", "")).split())


def test_t042_the_contract_job_regenerates_and_diffs_the_contract() -> None:
    job = workflow("ci.yaml")["jobs"]["contract"]
    export, generate, drift, enums = named(job, "T-042")
    assert script(export) == (
        "uv run --frozen --package docchat-api python -m docchat_api.openapi "
        "--output apps/api/openapi.json"
    )
    assert script(generate) == "npm --prefix apps/web run generate:api"
    assert "git diff --exit-code -- apps/api/openapi.json apps/web/src/api/generated" in (
        script(drift)
    )
    assert script(enums) == "uv run --frozen python scripts/ci/check_contract_enums.py"
    assert job["permissions"] == {"contents": "read"}


def test_t434_the_preflight_runs_in_preflight_whatif_and_in_every_deploy_job() -> None:
    (pull_request,) = named(workflow("ci.yaml")["jobs"]["preflight-whatif"], "T-434")
    assert script(pull_request) == "uv run --frozen python scripts/preflight/preflight.py --env dev"
    assert "DOCCHAT_DEV_OPERATOR_IP_ALLOWLIST" in str(pull_request["env"])
    (deploy,) = named(workflow("deploy-environment.yaml")["jobs"]["deploy"], "T-434")
    command = script(deploy)
    assert 'scripts/preflight/preflight.py --env "$DEPLOY_ENV"' in command
    assert "azd env set DOCCHAT_PREFLIGHT_DONE true" in command
    # Every full and images-only deploy runs it, before anything is provisioned or deployed.
    assert deploy["if"] == "${{ env.DEPLOYS_IMAGES == 'true' }}"
    names = [
        str(item.get("name"))
        for item in steps(workflow("deploy-environment.yaml")["jobs"]["deploy"])
    ]
    assert names.index(str(deploy["name"])) < names.index("Provision (T-124)")


def test_t453_each_image_is_built_once_without_a_token_and_deployed_by_its_attested_digest() -> (
    None
):
    jobs = workflow("deploy.yaml")["jobs"]
    build = jobs["build"]
    assert build["permissions"] == {"contents": "read"}
    (built,) = named(build, "T-453")
    for image, dockerfile in IMAGES:
        assert script(built).count(f"build {image} {dockerfile}") == 1
    assert (
        sum("docker buildx build" in script(item) for job in jobs.values() for item in steps(job))
        == 1
    )

    (verify,) = named(jobs["publish"], "T-453")
    command = script(verify)
    assert "gh attestation verify" in command
    assert '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/deploy.yaml"' in command
    assert "--source-ref refs/heads/main" in command
    assert "image_attestation_invalid" in command
    for reference in ("API_IMAGE", "INGESTION_IMAGE", "MODEL_STUB_IMAGE"):
        assert reference in verify["env"]

    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    names = [str(item.get("name")) for item in steps(deploy)]
    (compare,) = named(deploy, "T-453")
    download = steps(deploy)[names.index("Download the release manifest")]
    assert download["with"]["name"] == "release-manifest"
    assert download["with"]["run-id"] == "${{ inputs.web_artifact_run_id }}"
    order = ["Deploy the API image", "Deploy the ingestion image", str(compare["name"])]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    command = script(compare)
    assert "--query linuxFxVersion" in command
    assert "compare DOCCHAT_API_APP_NAME apiImage" in command
    assert "compare DOCCHAT_INGESTION_APP_NAME ingestionImage" in command


def test_t523_the_images_job_builds_inspects_and_checks_every_image() -> None:
    job = workflow("ci.yaml")["jobs"]["images"]
    assert "id-token" not in job["permissions"]
    built, inspected, contents = named(job, "T-523")
    for image, dockerfile in IMAGES:
        assert f"build {image} {dockerfile}" in script(built)
    assert "--load" in script(built)
    assert "--push" not in script(built)
    inspection = script(inspected)
    assert (
        '\'["gunicorn","--config","/app/gunicorn.conf.py","docchat_api.app:create_app()"]\''
        in inspection
    )
    assert "{{json .Config.Entrypoint}}" in inspection
    assert inspection.count('= "10001:10001" ]') == 2
    assert "PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY" in inspection
    check = script(contents)
    assert "for image in api ingestion model-stub" in check
    assert (
        'docker export "$container" | uv run --frozen python scripts/ci/check_image_contents.py'
        in check
    )
    names = [item.get("name") for item in steps(job)]
    assert names.index(contents["name"]) < names.index("Gate the findings (T-525)")


def test_t454_the_resolve_job_verifies_every_attestation_before_any_deploy() -> None:
    jobs = workflow("deploy.yaml")["jobs"]
    resolve = jobs["resolve"]
    (verify,) = named(resolve, "T-454")
    command = script(verify)
    assert 'gh attestation verify "oci://${reference}"' in command
    assert '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/deploy.yaml"' in command
    assert '--source-ref refs/heads/main --source-digest "$COMMIT"' in command
    assert "image_attestation_invalid" in command
    for reference in ("API_IMAGE", "INGESTION_IMAGE", "MODEL_STUB_IMAGE"):
        assert reference in verify["env"]
    assert resolve["permissions"]["attestations"] == "read"
    names = [str(item.get("name")) for item in steps(resolve)]
    assert names.index(str(verify["name"])) < names.index("Write the release manifest")
    # Every environment deploys only after resolve, and so after the verification.
    for deploy in ("deploy-dev", "deploy-prod"):
        assert "resolve" in jobs[deploy]["needs"]


def test_t525_trivy_scans_every_image_in_the_images_job_and_nightly() -> None:
    images = workflow("ci.yaml")["jobs"]["images"]
    scan, gate = named(images, "T-525")
    command = script(scan)
    assert "for image in api ingestion model-stub" in command
    assert "--scanners vuln,secret --severity HIGH,CRITICAL" in command
    assert script(gate) == "uv run --frozen python scripts/ci/security_gate.py --job images"

    rescan = workflow("nightly.yaml")["jobs"]["trivy-rescan"]
    scan, gate = named(rescan, "T-525")
    command = script(scan)
    assert "for environment in dev prod" in command
    assert "--scanners vuln,secret --severity HIGH,CRITICAL" in command
    assert 'security_gate.py --job build --audit-dir "build/audit/${environment}"' in script(gate)


def test_t452_and_t023_the_prod_post_deploy_checks_run_in_every_prod_deploy() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    (checks,) = named(deploy, "T-452")
    assert "T-023" in checks["name"]
    assert script(checks) == "uv run --frozen python scripts/deploy/prod_checks.py"
    assert "inputs.environment == 'prod'" in checks["if"]
    # After the web deploy, so the release is deployed and keeps serving when a check fails.
    assert "steps.web.outcome == 'success'" in checks["if"]
    names = [str(item.get("name")) for item in steps(deploy)]
    assert names.index("Deploy the web client") < names.index(str(checks["name"]))
    assert names.index(str(checks["name"])) < names.index("app-settings-check")


def deployed_node(item: dict[str, Any]) -> tuple[Path, str]:
    """The file and test function of the single deployed test a step runs."""
    (node,) = re.findall(r'"(tests/deployed/[^"]+)"', script(item))
    path, function = node.split("::")
    return REPOSITORY_ROOT / path, function


@pytest.mark.parametrize(
    ("identifier", "when"),
    [
        ("T-436", "inputs.environment == 'prod'"),
        ("T-437", "steps.provision.outcome == 'success'"),
        ("T-472", "steps.provision.outcome == 'success'"),
        ("T-445", "steps.grants-open.outcome != 'skipped'"),
    ],
)
def test_the_deploy_job_runs_its_deployed_rows_as_single_tests(identifier: str, when: str) -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    (item,) = named(deploy, identifier)
    path, function = deployed_node(item)
    assert path.is_file()
    assert re.search(rf"^async def {function}\(", path.read_text(encoding="utf-8"), re.MULTILINE)
    assert function.startswith(f"test_t{identifier[2:]}_")
    assert "--deployed" in script(item)
    # The test reads .azure/<env>/.env of the job's own environment.
    assert item["env"]["AZURE_ENV_NAME"] == "${{ inputs.environment }}"
    assert when in item["if"]


def test_every_identifier_a_step_name_carries_is_a_workflow_row_of_the_catalogue() -> None:
    levels = {row.identifier: row.level for row in read_catalogue(REPOSITORY_ROOT)}
    named_steps = [
        (path.name, str(item["name"]))
        for path in sorted(WORKFLOWS.glob("*.yaml"))
        for job in (yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"] or {}).values()
        for item in steps(job)
        if STEP_IDENTIFIER.search(str(item.get("name", "")))
    ]
    assert named_steps, "no workflow step names an identifier"
    for file, name in named_steps:
        for number in STEP_IDENTIFIER.findall(name):
            level = levels.get(f"T-{number}", "")
            assert level in WORKFLOW_LEVELS, f"{file}: {name!r} names a row of level {level!r}"


# --- ci.yaml: lint, python-tests, web, security, images (84-testing.md sections 9.2 and 10.1) ---

COVERAGE_THRESHOLDS: Final = {
    "docchat_core": ("packages/core/src/docchat_core", 90),
    "docchat_api": ("apps/api/src/docchat_api", 85),
    "docchat_ingestion": ("apps/ingestion/src/docchat_ingestion", 85),
    "docchat_ops": ("scripts/ops/src/docchat_ops", 80),
    "docchat_model_stub": ("tests/model-stub/src/docchat_model_stub", 80),
}
"""Section 10.4's per-package thresholds, line and branch combined."""


def test_t512_the_lint_job_installs_from_the_lock_and_runs_every_hook() -> None:
    job = workflow("ci.yaml")["jobs"]["lint"]
    install, hooks = named(job, "T-512")
    assert script(install) == "uv sync --locked --all-packages --all-groups"
    assert script(hooks) == "uv run --frozen pre-commit run --all-files --show-diff-on-failure"
    config = yaml.safe_load((REPOSITORY_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks_by_id = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    # uv lock --locked, Ruff (whose configuration selects the S rules, S608 among them) and mypy
    # run as hooks.
    assert hooks_by_id["uv-lock"]["args"] == ["--locked"]
    assert {"ruff-check", "ruff-format", "mypy"} <= set(hooks_by_id)
    root = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^select = \[[^\]]*"S"', root, re.MULTILINE)


def test_t513_the_lint_job_checks_the_import_contracts() -> None:
    (contracts,) = named(workflow("ci.yaml")["jobs"]["lint"], "T-513")
    assert script(contracts) == "uv run --frozen lint-imports"


def test_t519_each_package_below_its_threshold_is_named() -> None:
    jobs = workflow("ci.yaml")["jobs"]
    (python,) = named(jobs["python-tests"], "T-519")
    command = script(python)
    for package, (source, threshold) in COVERAGE_THRESHOLDS.items():
        assert f"check {package} {source} {threshold}" in command
    assert '--include="$2/*" --fail-under="$3"' in command
    assert "::error::$1 coverage is below its threshold of $3%" in command
    assert command.endswith('exit "$failed"')
    names = [str(item.get("name")) for item in steps(jobs["python-tests"])]
    (suites,) = named(jobs["python-tests"], "Run the unit and integration")
    assert names.index(str(suites["name"])) < names.index(str(python["name"]))
    (web,) = named(jobs["web"], "T-519")
    # The web package's test script is `vitest run --coverage`.
    assert script(web) == "npm test"


def test_t520_the_python_tests_job_starts_the_services_and_runs_the_suites() -> None:
    job = workflow("ci.yaml")["jobs"]["python-tests"]
    services, suites = named(job, "T-520")
    assert script(services) == "docker compose up -d --wait"
    assert script(suites) == (
        "uv run --frozen pytest --cov=docchat_core --cov=docchat_api --cov=docchat_ingestion "
        "--cov=docchat_ops --cov=docchat_model_stub --cov-branch --junitxml=reports/pytest.xml"
    )
    (stop,) = [item for item in steps(job) if script(item) == "docker compose down -v"]
    assert stop["if"] == "always()"
    assert job["timeout-minutes"] == 45


def test_t526_gitleaks_scans_each_pull_requests_commits_and_nightly_the_full_history() -> None:
    (pull_request,) = named(workflow("ci.yaml")["jobs"]["security"], "T-526")
    command = script(pull_request)
    assert '"$GITLEAKS_IMAGE" git --redact' in command
    assert '--log-opts="${BASE_SHA}..${HEAD_SHA}"' in command
    nightly = workflow("nightly.yaml")["jobs"]["gitleaks"]
    (history,) = named(nightly, "T-526")
    assert '"$GITLEAKS_IMAGE" git --redact' in script(history)
    assert "--log-opts" not in script(history)
    assert steps(nightly)[0]["with"]["fetch-depth"] == 0


def test_t529_zizmor_reports_medium_and_high_findings_to_the_gate() -> None:
    job = workflow("ci.yaml")["jobs"]["security"]
    (zizmor,) = named(job, "T-529")
    assert script(zizmor) == (
        "uv run --frozen zizmor --format json --min-severity medium --no-exit-codes "
        ".github/workflows > build/audit/zizmor.json"
    )
    names = [str(item.get("name")) for item in steps(job)]
    assert names.index(str(zizmor["name"])) < names.index("Gate the findings")
    assert script(steps(job)[-1]) == (
        "uv run --frozen python scripts/ci/security_gate.py --job security"
    )


def test_t194_the_images_job_reads_the_ingestion_image_back() -> None:
    job = workflow("ci.yaml")["jobs"]["images"]
    (check,) = named(job, "T-194")
    command = str(check["run"])
    assert "--network none --entrypoint /bin/bash" in command
    assert '"docchat/ingestion:${COMMIT}" /check-ingestion.sh' in command
    for expected in (
        "msodbcsql18 libseccomp2 procps",
        "libreoffice-core-nogui libreoffice-writer-nogui libreoffice-impress-nogui",
        "install ok installed 4:7.4.7-1+deb12u14",
        "fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation2",
        "for n in $(seq 0 15)",
        "id=$((10002 + n))",
        '"root root 600"',
        '"root 711"',
        "-perm -0002 -not -path /tmp -not -path /var/tmp",
        "/var/cache/fontconfig/",
        "libupdchklo.so",
        "(4, 39, 1) <= version < (5, 0, 0)",
        "gcc cc c++ g++ clang",
        # gcc-12-base is the one package with a compiler's name that the image may keep.
        r"grep -E '^(gcc|g\+\+|cpp|clang)[^ ]* install ok installed' | grep -v '^gcc-12-base ')",
        "nvidia*",
    ):
        assert expected in command, expected
    names = [str(item.get("name")) for item in steps(job)]
    (build,) = named(job, "Build the images without pushing")
    assert names.index(str(build["name"])) < names.index(str(check["name"]))
    assert names.index(str(check["name"])) < names.index("Gate the findings (T-525)")


# --- ci.yaml: infra-validate and preflight-whatif (83-infrastructure.md section 10.5) ----------

PSRULE_RULES: Final = REPOSITORY_ROOT / ".ps-rule" / "docchat.Rule.yaml"
PSRULE_SUPPRESSIONS: Final = REPOSITORY_ROOT / ".ps-rule" / "docchat-suppressions.Rule.yaml"
PSRULE_QUANTIFIERS: Final = frozenset(
    {"count", "less", "lessOrEquals", "greater", "greaterOrEquals"}
)


def psrule_documents(path: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    """Each rule, selector or suppression group of a `.ps-rule/` file by name: its text, loaded."""
    text = path.read_text(encoding="utf-8")
    parts = [part for part in text.split("\n---\n")[1:] if part.strip()]
    loaded = [(part, yaml.safe_load(part)) for part in parts]
    return {document["metadata"]["name"]: (part, document) for part, document in loaded}


def test_t421_bicep_build_build_params_and_lint_run_for_every_environment() -> None:
    job = workflow("ci.yaml")["jobs"]["infra-validate"]
    build, lint = named(job, "T-421")
    command = script(build)
    assert "az bicep build --file infra/main.bicep" in command
    assert "az bicep build --file infra/shared.bicep" in command
    for environment in ("dev", "prod", "load"):
        assert f"AZURE_ENV_NAME={environment}" in command
    assert command.count("az bicep build-params --file infra/main.bicepparam") == 3
    assert script(lint) == "git ls-files -z -- '*.bicep' | xargs -0 -n1 az bicep lint --file"


def test_t423_psrule_runs_for_each_environment_and_uploads_its_sarif() -> None:
    job = workflow("ci.yaml")["jobs"]["infra-validate"]
    runs = [item for item in named(job, "T-423") if "ps-rule@" in str(item.get("uses"))]
    uploads = [item for item in named(job, "T-423") if "upload-sarif@" in str(item.get("uses"))]
    assert [item["env"]["AZURE_ENV_NAME"] for item in runs] == ["dev", "prod", "load"]
    for item in runs:
        assert item["with"]["baseline"] == "Azure.GA_2025_12"
        assert item["with"]["outputFormat"] == "Sarif"
    assert [item["with"]["category"] for item in uploads] == [
        "ps-rule-dev",
        "ps-rule-prod",
        "ps-rule-load",
    ]
    settings = yaml.safe_load((REPOSITORY_ROOT / "ps-rule.yaml").read_text(encoding="utf-8"))
    assert settings["requires"]["PSRule.Rules.Azure"] == ">=1.47.0 <1.48.0"
    assert settings["execution"]["suppressionGroupExpired"] == "Error"


def test_t423_psrule_evaluates_the_custom_rules_and_the_suppressions() -> None:
    # 83-infrastructure.md section 13.2: without the binding, `type` and `name` in .ps-rule/ never
    # match a resource; without includeLocal, the baseline's tag filter drops the custom rules.
    settings = yaml.safe_load((REPOSITORY_ROOT / "ps-rule.yaml").read_text(encoding="utf-8"))
    assert settings["binding"] == {
        "preferTargetInfo": True,
        "targetName": ["ResourceName", "name"],
        "targetType": ["ResourceType", "type"],
    }
    assert settings["rule"]["includeLocal"] is True


def test_t423_templates_with_required_parameters_expand_through_their_parameter_files() -> None:
    settings = yaml.safe_load((REPOSITORY_ROOT / "ps-rule.yaml").read_text(encoding="utf-8"))
    ignored = settings["input"]["pathIgnore"]
    assert {"infra/main.bicep", "infra/shared.bicep"} <= set(ignored)
    # A PSRule path pattern also matches longer names, so the parameter files come back after.
    assert ignored.index("!infra/*.bicepparam") > ignored.index("infra/shared.bicep")
    assert ignored.index("!infra/*.bicepparam") > ignored.index("infra/main.bicep")
    assert {"infra/modules/**", "infra/bootstrap/modules/**"} <= set(ignored)


def test_t423_the_yaml_hook_reads_the_multi_document_rule_files() -> None:
    # Each rule, selector and suppression group is its own YAML document (sections 13.3, 13.4).
    config = yaml.safe_load((REPOSITORY_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks_by_id = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert hooks_by_id["check-yaml"]["args"] == ["--allow-multiple-documents"]


def test_t423_every_suppression_group_carries_an_expiry_and_a_synopsis() -> None:
    text = PSRULE_SUPPRESSIONS.read_text(encoding="utf-8")
    documents = [part for part in text.split("\n---\n")[1:] if part.strip()]
    assert documents, "no suppression group"
    for document in documents:
        loaded = yaml.safe_load(document)
        name = loaded["metadata"]["name"]
        assert loaded["kind"] == "SuppressionGroup", name
        assert loaded["spec"]["expiresOn"] == "2027-10-31T00:00:00Z", f"{name}: spec.expiresOn"
        # PSRule prints only the first comment line as the synopsis, so the reason, with the ADR
        # it rests on, is on that line and the group follows it directly.
        synopsis = re.search(r"^# Synopsis: (\S.*)\napiVersion: ", document, re.MULTILINE)
        assert synopsis, f"{name} has no one-line synopsis"
        assert re.search(r"\bADRs? \d{4}", synopsis.group(1)), f"{name}'s synopsis names no ADR"
        assert loaded["spec"]["rule"], f"{name} names no rule"


def test_t423_sql_auditing_reads_the_privileged_audit_nested_under_each_server() -> None:
    # 83-infrastructure.md section 13.3: Azure.SQL.Auditing reads only servers/auditingSettings,
    # so DocChat.SQL.Auditing judges the extendedAuditingSettings of section 2.9, which PSRule for
    # Azure nests under its server's `resources`.
    documents = psrule_documents(PSRULE_RULES)
    _, rule = documents["DocChat.SQL.Auditing"]
    assert rule["kind"] == "Rule"
    (selector_name,) = rule["spec"]["with"]
    _, selector = documents[selector_name]
    assert selector["kind"] == "Selector"
    assert selector["spec"]["if"] == {
        "allOf": [
            {"type": ".", "equals": "Microsoft.Sql/servers"},
            {"name": ".", "like": "sql-docchat-*"},
        ]
    }
    settings = {"field": "type", "equals": "Microsoft.Sql/servers/extendedAuditingSettings"}
    exactly_one, every_one = rule["spec"]["condition"]["allOf"]
    # A quantifier counts the items that match both the filter and its allOf: one `default`.
    assert exactly_one == {
        "field": "resources",
        "where": settings,
        "count": 1,
        "allOf": [{"field": "name", "endsWith": "/default"}],
    }
    # Without a quantifier every setting of the type must match, so a second one fails too.
    assert {key: every_one[key] for key in ("field", "where")} == {
        "field": "resources",
        "where": settings,
    }
    assert not PSRULE_QUANTIFIERS & every_one.keys()
    assert every_one["allOf"] == [
        {"field": "name", "endsWith": "/default"},
        {"field": "properties.state", "equals": "Enabled"},
        {"field": "properties.isAzureMonitorTargetEnabled", "equals": True},
        {"field": "properties.predicateExpression", "hasValue": True},
    ]


def test_t423_azure_sql_auditing_is_suppressed_only_where_the_custom_rule_applies() -> None:
    # Section 13.4: the suppression covers exactly the servers DocChat.SQL.Auditing judges, so no
    # SQL server escapes both rules, and its synopsis names ADR 0108.
    groups = [
        (text, group)
        for text, group in psrule_documents(PSRULE_SUPPRESSIONS).values()
        if "Azure.SQL.Auditing" in group["spec"]["rule"]
    ]
    ((text, group),) = groups
    assert group["spec"]["rule"] == ["Azure.SQL.Auditing"]
    rules = psrule_documents(PSRULE_RULES)
    (selector_name,) = rules["DocChat.SQL.Auditing"][1]["spec"]["with"]
    assert group["spec"]["if"] == rules[selector_name][1]["spec"]["if"]
    synopsis = re.search(r"^# Synopsis: (.*)$", text, re.MULTILINE)
    assert synopsis
    assert "ADR 0108" in synopsis.group(1)
    assert "DocChat.SQL.Auditing" in synopsis.group(1)


def test_t431_what_if_runs_against_dev_and_shared_as_the_pull_request_identity() -> None:
    job = workflow("ci.yaml")["jobs"]["preflight-whatif"]
    (what_if,) = named(job, "T-431")
    command = script(what_if)
    assert command.count("--validation-level ProviderNoRbac") == 2
    assert "what-if --resource-group rg-docchat-dev" in command
    assert "what-if --resource-group rg-docchat-shared" in command
    (login,) = [item for item in steps(job) if "azure/login@" in str(item.get("uses"))]
    assert login["with"]["client-id"] == "${{ vars.DOCCHAT_PR_CLIENT_ID }}"
    assert job["if"] == "github.event.pull_request.head.repo.full_name == github.repository"


def test_t432_pr_write_refused_requires_authorization_failed_in_both_groups() -> None:
    job = workflow("ci.yaml")["jobs"]["preflight-whatif"]
    (refused,) = named(job, "T-432")
    assert refused["id"] == "pr-write-refused"
    command = str(refused["run"])
    assert '"resources": []' in command
    assert "for group in rg-docchat-dev rg-docchat-shared" in command
    assert '--name "pr-write-refused-${GITHUB_RUN_ID}"' in command
    assert "grep -q 'AuthorizationFailed'" in command
    assert 'exit "$failed"' in command


# --- nightly.yaml, deployed-tests.yaml and the deploy job ---------------------------------------


def test_t545_the_expiry_check_runs_between_the_vault_window_steps() -> None:
    job = workflow("nightly.yaml")["jobs"]["test-credential-expiry"]
    assert job["environment"] == "dev-tests"
    (check,) = named(job, "T-545")
    assert script(check) == "uv run --frozen docchat-ops --env dev testusers check --set all"
    names = [str(item.get("name")) for item in steps(job)]
    position = names.index(str(check["name"]))
    assert names[position - 1] == "Open the test vault's read window"
    assert names[position + 1] == "Close the test vault's read window"


def test_t348_the_web_deployed_job_runs_the_deployed_specs() -> None:
    job = workflow("deployed-tests.yaml")["jobs"]["web-deployed"]
    (specs,) = named(job, "T-348")
    assert script(specs) == (
        "npx --no-install playwright test specs/web-deployed/ --project chromium"
    )
    assert "id-token" not in job["permissions"]


def test_t124_every_provision_runs_the_capacity_check_in_its_postprovision_hook() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    (provision,) = named(deploy, "T-124")
    assert script(provision) == "azd provision --no-prompt"
    hooks = yaml.safe_load((REPOSITORY_ROOT / "azure.yaml").read_text(encoding="utf-8"))["hooks"]
    assert "scripts/deploy/hooks/postprovision.py" in str(hooks["postprovision"])
    hook = (REPOSITORY_ROOT / "scripts" / "deploy" / "hooks" / "postprovision.py").read_text(
        encoding="utf-8"
    )
    assert '"chat", "capacity-check"' in hook
    assert "chat_capacity_exceeded" in hook


def test_t475_the_static_site_check_runs_after_each_provision() -> None:
    deploy = workflow("deploy-environment.yaml")["jobs"]["deploy"]
    (check,) = named(deploy, "T-475")
    assert script(check) == (
        'uv run --frozen python scripts/deploy/deploy_checks.py static-site --env "$DEPLOY_ENV"'
    )
    assert "steps.provision.outcome == 'success'" in check["if"]
    names = [str(item.get("name")) for item in steps(deploy)]
    assert names.index("Provision (T-124)") < names.index(str(check["name"]))
