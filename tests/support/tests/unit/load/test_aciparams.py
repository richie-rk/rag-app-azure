from __future__ import annotations

import base64
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from docchat_testing.load.aciparams import (
    DOCKERFILE,
    IMAGE_PREFIX,
    build_parameters,
    check_image,
    container_name,
    final_from,
    read_lock,
    write_parameters,
)
from docchat_testing.load.common import K6_LOCK_FILE, UsageError, load_questions

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DIGEST = "@sha256:" + "a" * 64
ENVIRON = {
    "GITHUB_RUN_NUMBER": "42",
    "AZURE_LOCATION": "eastus2",
    "API_BASE_URL": "https://app-docchat-api-load-abc123.azurewebsites.net/",
    "DOCCHAT_LOAD_ANSWER_TOKENS": "320",
    "DOCCHAT_LOAD_TOKEN_DELAY_MS": "2",
    "DOCCHAT_LOAD_RATE": "",
    "DOCCHAT_TEST_VAULT_URI": "https://kv.example/",
}


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, Path]:
    tokens = tmp_path / "tokens.json"
    tokens.write_text(
        json.dumps({"tokens": {"load-01": "tok-1", "load-02": "tok-2", "load-admin": "tok-a"}}),
        "utf-8",
    )
    return {
        "bootstrap": REPOSITORY_ROOT / "tests" / "load" / "aci" / "run.py",
        "script": REPOSITORY_ROOT / "tests" / "load" / "k6" / "s3-calibrate.js",
        "lib": REPOSITORY_ROOT / "tests" / "load" / "k6" / "lib.js",
        "tokens": tokens,
    }


def values(parameters: dict[str, Any]) -> dict[str, Any]:
    return {name: entry["value"] for name, entry in parameters["parameters"].items()}


def test_the_image_is_the_ingestion_dockerfiles_final_from() -> None:
    reference = final_from(DOCKERFILE)
    assert reference.startswith(IMAGE_PREFIX)
    assert reference == check_image(reference)
    lines = [line for line in DOCKERFILE.read_text("utf-8").splitlines() if line.startswith("FROM")]
    assert lines[-1].split()[1] == reference


@pytest.mark.parametrize(
    "reference",
    [
        "mcr.microsoft.com/azure-functions/python:4.1055.150-0-python3.13",
        "docker.io/grafana/k6:2.3.0" + DIGEST,
        "crdocchatabc.azurecr.io/docchat/ingestion" + DIGEST,
        "mcr.microsoft.com/azure-functions/python:4@sha256:short",
    ],
)
def test_a_reference_without_a_digest_or_off_mcr_is_refused(reference: str) -> None:
    with pytest.raises(UsageError) as raised:
        check_image(reference)
    assert raised.value.exit_code == 2


def test_a_dockerfile_whose_final_from_has_no_digest_is_refused(
    tmp_path: Path, inputs: dict[str, Path]
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        f"FROM {IMAGE_PREFIX}4{DIGEST} AS build\nFROM {IMAGE_PREFIX}4-latest AS runtime\n", "utf-8"
    )
    with pytest.raises(UsageError, match="image"):
        build_parameters(scenario="s1", environ=ENVIRON, dockerfile=dockerfile, **inputs)
    empty = tmp_path / "Empty"
    empty.write_text("# nothing\n", "utf-8")
    with pytest.raises(UsageError, match="no FROM"):
        final_from(empty)


def test_the_parameters_carry_the_lock_the_files_and_the_run_file(
    inputs: dict[str, Path],
) -> None:
    parameters = build_parameters(scenario="s3-calibrate", environ=ENVIRON, **inputs)
    found = values(parameters)
    lock = json.loads(K6_LOCK_FILE.read_text("utf-8"))

    assert parameters["contentVersion"] == "1.0.0.0"
    assert found["name"] == "ci-docchat-loadgen-s3-calibrate-42"
    assert found["location"] == "eastus2"
    assert found["image"] == final_from()
    assert (found["k6Url"], found["k6Sha256"], found["k6Member"]) == (
        lock["url"],
        lock["sha256"],
        lock["member"],
    )
    assert found["scenarioEnv"] == {
        "DOCCHAT_LOAD_ANSWER_TOKENS": "320",
        "DOCCHAT_LOAD_TOKEN_DELAY_MS": "2",
    }
    assert base64.b64decode(found["runPyBase64"]) == inputs["bootstrap"].read_bytes()
    assert base64.b64decode(found["scenarioJsBase64"]) == inputs["script"].read_bytes()
    assert base64.b64decode(found["libJsBase64"]) == inputs["lib"].read_bytes()
    run = json.loads(base64.b64decode(found["tokensJsonBase64"]))
    assert run == {
        "apiBaseUrl": "https://app-docchat-api-load-abc123.azurewebsites.net",
        # The administrator's token never reaches the container.
        "tokens": {"load-01": "tok-1", "load-02": "tok-2"},
        "questions": load_questions(),
    }
    # Nothing else of the job's environment reaches the container.
    assert "kv.example" not in json.dumps(found["scenarioEnv"])


def test_the_parameters_file_is_private(tmp_path: Path, inputs: dict[str, Path]) -> None:
    out = tmp_path / "loadgen.parameters.json"
    write_parameters(out, scenario="s1", environ=ENVIRON, **inputs)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert values(json.loads(out.read_text("utf-8")))["name"] == "ci-docchat-loadgen-s1-42"


def test_names_and_lock_inputs_are_checked(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        container_name("s9", "1")
    with pytest.raises(UsageError):
        container_name("s1", "abc")
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"version": "2.3.0", "url": "u"}), "utf-8")
    with pytest.raises(UsageError, match="lacks"):
        read_lock(lock)
    assert read_lock()["member"] == "k6-v2.3.0-linux-amd64/k6"
