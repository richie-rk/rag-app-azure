"""`docchat-load aci-params` (`84-testing.md` sections 7.2 and 7.5 step 5; ADR 0135).

Writes the parameters file of `tests/load/aci/loadgen.bicep` for one scenario, with mode 0600:

- non-secret `name` (`ci-docchat-loadgen-<scenario>-<run number>`), `location`, `image`, `k6Url`,
  `k6Sha256`, `k6Member` (all three from `tests/load/k6.lock.json`) and `scenarioEnv`, the
  scenario's variables of `81-config.md` section 4.12 that the job set;
- secure `runPyBase64`, `scenarioJsBase64`, `libJsBase64` and `tokensJsonBase64`.

`image` is the reference on the final `FROM` line of `apps/ingestion/Dockerfile`; the command
exits 2 unless it starts `mcr.microsoft.com/azure-functions/python:` and carries a `@sha256:`
digest, because the generator pulls only that image, which the repository already pins and scans.

The container's `tokens.json` is the run file the scripts read: the API's base URL, the token file's
tokens and the load questions, since the work volume is all the container holds.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from docchat_testing.load.common import (
    K6_LOCK_FILE,
    LOAD_USERS,
    QUESTIONS_FILE,
    UsageError,
    load_questions,
    read_json,
    read_tokens,
    setting,
    write_json,
)
from docchat_testing.tokens import REPOSITORY_ROOT

DOCKERFILE: Final = REPOSITORY_ROOT / "apps" / "ingestion" / "Dockerfile"
IMAGE_PREFIX: Final = "mcr.microsoft.com/azure-functions/python:"
SCENARIOS: Final = ("s1", "s2", "s3-calibrate", "s3-scaleout")
SCENARIO_VARIABLES: Final = (
    "DOCCHAT_LOAD_ANSWER_TOKENS",
    "DOCCHAT_LOAD_TOKEN_DELAY_MS",
    "DOCCHAT_LOAD_RATE",
)
PARAMETERS_SCHEMA: Final = (
    "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
)
_FROM: Final = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE)
_DIGEST: Final = re.compile(r"@sha256:[0-9a-f]{64}$")


def final_from(dockerfile: Path = DOCKERFILE) -> str:
    """The image reference on the Dockerfile's last `FROM` line."""
    references = [
        found.group(1)
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if (found := _FROM.match(line)) is not None
    ]
    if not references:
        raise UsageError("image_invalid", f"{dockerfile} has no FROM line")
    return references[-1]


def check_image(reference: str) -> str:
    if not reference.startswith(IMAGE_PREFIX) or not _DIGEST.search(reference):
        raise UsageError(
            "image_invalid",
            f"the generator image must be {IMAGE_PREFIX}<tag>@sha256:<digest>, not {reference}",
        )
    return reference


def read_lock(path: Path = K6_LOCK_FILE) -> dict[str, str]:
    lock = read_json(path, "the k6 lock")
    if not isinstance(lock, dict) or not all(
        isinstance(lock.get(key), str) and lock[key] for key in ("url", "sha256", "member")
    ):
        raise UsageError("lock_invalid", f"{path} lacks url, sha256 or member")
    return {key: str(lock[key]) for key in ("version", "url", "sha256", "member") if key in lock}


def container_name(scenario: str, run_number: str) -> str:
    if scenario not in SCENARIOS:
        raise UsageError("scenario_invalid", f"unknown scenario {scenario}")
    if not run_number.isdigit():
        raise UsageError("run_number_invalid", "GITHUB_RUN_NUMBER isn't a number")
    return f"ci-docchat-loadgen-{scenario}-{run_number}"


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def run_file(tokens: Mapping[str, str], api_base_url: str, questions: list[str]) -> bytes:
    """The container's `tokens.json`: base URL, the 20 load users' tokens and the questions,
    nothing else; the administrator's token, which no scenario uses, stays on the runner."""
    users = {alias: token for alias, token in tokens.items() if alias in LOAD_USERS}
    return json.dumps(
        {"apiBaseUrl": api_base_url, "tokens": users, "questions": questions},
        separators=(",", ":"),
    ).encode("utf-8")


def build_parameters(
    *,
    scenario: str,
    bootstrap: Path,
    script: Path,
    lib: Path,
    tokens: Path,
    environ: Mapping[str, str] | None = None,
    dockerfile: Path = DOCKERFILE,
    lock: Path = K6_LOCK_FILE,
    questions: Path = QUESTIONS_FILE,
) -> dict[str, Any]:
    """The deployment parameters file's content."""
    env = os.environ if environ is None else environ
    k6 = read_lock(lock)
    values: dict[str, Any] = {
        "name": container_name(scenario, setting("GITHUB_RUN_NUMBER", environ=env)),
        "location": setting("AZURE_LOCATION", environ=env),
        "image": check_image(final_from(dockerfile)),
        "k6Url": k6["url"],
        "k6Sha256": k6["sha256"],
        "k6Member": k6["member"],
        "scenarioEnv": {name: env[name] for name in SCENARIO_VARIABLES if env.get(name)},
        "runPyBase64": _base64(bootstrap.read_bytes()),
        "scenarioJsBase64": _base64(script.read_bytes()),
        "libJsBase64": _base64(lib.read_bytes()),
        "tokensJsonBase64": _base64(
            run_file(
                read_tokens(tokens),
                setting("API_BASE_URL", environ=env).rstrip("/"),
                load_questions(questions),
            )
        ),
    }
    return {
        "$schema": PARAMETERS_SCHEMA,
        "contentVersion": "1.0.0.0",
        "parameters": {name: {"value": value} for name, value in values.items()},
    }


def write_parameters(out: Path, **arguments: Any) -> dict[str, Any]:
    parameters = build_parameters(**arguments)
    write_json(out, parameters, private=True)
    return parameters
