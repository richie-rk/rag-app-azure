"""The load generator's container group (`tests/load/aci/loadgen.bicep`; `84-testing.md` section
7.2; ADR 0135), compiled by the infra-tests job into `<compiled>/load/loadgen.json`.

One Linux container group per scenario, `restartPolicy: 'Never'`, no virtual network, no inbound
address and no identity; one container `k6` of 4 vCPU and 8 GB running `python /work/run.py` from
the image parameter; the environment `K6_URL`, `K6_SHA256`, `K6_MEMBER`,
`DOCCHAT_LOAD_TOKENS_FILE=/work/tokens.json` and the scenario's variables, nothing else; and the
read-only secret volume `work` with the four files from secure parameters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arm_template import Deployment, load_deployment
from compiled_support import REPO_ROOT, load_json

IMAGE = (
    "mcr.microsoft.com/azure-functions/python:4.1055.300-3-python3.13@sha256:"
    "a7f5fb3a2f98c9f883eaa2b6f2d2f602d9fc44f41a91c450623b6784c2d7b379"
)
SECURE = ("runPyBase64", "scenarioJsBase64", "libJsBase64", "tokensJsonBase64")


@pytest.fixture(scope="module")
def template(compiled_dir: Path) -> dict[str, Any]:
    path = compiled_dir / "load" / "loadgen.json"
    if not path.is_file():
        pytest.fail(f"no {path}: build tests/load/aci/loadgen.bicep into <compiled>/load")
    compiled: dict[str, Any] = load_json(path)
    return compiled


def _deploy(template: dict[str, Any], scenario_env: dict[str, str]) -> Deployment:
    lock = json.loads((REPO_ROOT / "tests" / "load" / "k6.lock.json").read_text("utf-8"))
    parameters = {
        "name": "ci-docchat-loadgen-s3-scaleout-42",
        "location": "eastus2",
        "image": IMAGE,
        "k6Url": lock["url"],
        "k6Sha256": lock["sha256"],
        "k6Member": lock["member"],
        "scenarioEnv": scenario_env,
    } | {name: f"{name}-value" for name in SECURE}
    return load_deployment(template, parameters, resource_group="rg-docchat-load")


def test_the_file_parameters_are_secure_and_the_rest_are_not(template: dict[str, Any]) -> None:
    parameters = template["parameters"]
    assert set(parameters) == {
        "name",
        "location",
        "image",
        "k6Url",
        "k6Sha256",
        "k6Member",
        "scenarioEnv",
        *SECURE,
    }
    for name, declaration in parameters.items():
        secure = declaration["type"] in {"securestring", "secureString"}
        assert secure is (name in SECURE), name


def test_one_never_restarted_container_group_without_network_or_identity(
    template: dict[str, Any],
) -> None:
    [group] = _deploy(template, {}).resources()
    assert group.type == "Microsoft.ContainerInstance/containerGroups"
    assert group.api_version == "2026-07-01"
    assert group.name == "ci-docchat-loadgen-s3-scaleout-42"
    assert group.field("location") == "eastus2"
    assert group.field("identity") is None
    properties = group.properties
    assert properties["osType"] == "Linux"
    assert properties["sku"] == "Standard"
    assert properties["restartPolicy"] == "Never"
    assert "ipAddress" not in properties
    assert "subnetIds" not in properties
    assert "imageRegistryCredentials" not in properties


def test_the_k6_container_runs_the_bootstrap_with_exactly_its_variables(
    template: dict[str, Any],
) -> None:
    scenario = {"DOCCHAT_LOAD_RATE": "18", "DOCCHAT_LOAD_ANSWER_TOKENS": "80"}
    [group] = _deploy(template, scenario).resources()
    [container] = group.properties["containers"]
    assert container["name"] == "k6"
    settings = container["properties"]
    assert settings["image"] == IMAGE
    assert settings["command"] == ["python", "/work/run.py"]
    assert settings["resources"]["requests"] == {"cpu": 4, "memoryInGB": 8}
    lock = json.loads((REPO_ROOT / "tests" / "load" / "k6.lock.json").read_text("utf-8"))
    assert {item["name"]: item["value"] for item in settings["environmentVariables"]} == {
        "K6_URL": lock["url"],
        "K6_SHA256": lock["sha256"],
        "K6_MEMBER": lock["member"],
        "DOCCHAT_LOAD_TOKENS_FILE": "/work/tokens.json",
        "DOCCHAT_LOAD_RATE": "18",
        "DOCCHAT_LOAD_ANSWER_TOKENS": "80",
    }
    assert all("secureValue" not in item for item in settings["environmentVariables"])
    assert settings["volumeMounts"] == [{"name": "work", "mountPath": "/work", "readOnly": True}]


def test_the_work_volume_holds_the_four_files_from_secure_parameters(
    template: dict[str, Any],
) -> None:
    [group] = _deploy(template, {}).resources()
    [volume] = group.properties["volumes"]
    assert volume["name"] == "work"
    assert volume["secret"] == {
        "run.py": "runPyBase64-value",
        "scenario.js": "scenarioJsBase64-value",
        "lib.js": "libJsBase64-value",
        "tokens.json": "tokensJsonBase64-value",
    }
