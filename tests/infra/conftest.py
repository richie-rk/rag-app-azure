"""Fixtures of the compiled-template tests (84-testing.md section 8.1).

The `infra-tests` job compiles the templates and runs `uv run pytest tests/infra --compiled
build/infra`. The directory holds, from `az bicep build` and `az bicep build-params`:

- `main.json` and `shared.json`, the compiled `infra/main.bicep` and `infra/shared.bicep`;
- `main.<env>.parameters.json` for `dev`, `prod` and `load`, from `infra/main.bicepparam` built
  with `AZURE_ENV_NAME=<env>`;
- `load/loadgen.json`, the compiled load generator `tests/load/aci/loadgen.bicep`.

The tests evaluate those templates offline with `arm_template` and need no Azure sign-in.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# The tests import their helpers as top-level modules: tests/infra is no package.
# The helpers sit in their own directory so that mypy can name them without a second `conftest`.
sys.path.insert(0, str(Path(__file__).parent / "helpers"))

from arm_template import Deployment, load_deployment
from compiled_support import (
    GROUP_IDS,
    SECURITY_WORKSPACE_ID,
    MainFactory,
    SharedFactory,
    load_json,
    with_config,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--compiled",
        default=None,
        help="directory of main.json, shared.json and main.<env>.parameters.json",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--compiled") is None:
        raise pytest.UsageError(
            "tests/infra needs --compiled <directory of the compiled templates>"
        )


@pytest.fixture(scope="session")
def compiled_dir(pytestconfig: pytest.Config) -> Path:
    directory = Path(str(pytestconfig.getoption("--compiled")))
    if not (directory / "main.json").is_file():
        pytest.fail(f"no compiled templates in {directory}: build them with az bicep build first")
    return directory


@pytest.fixture(scope="session")
def main_template(compiled_dir: Path) -> dict[str, Any]:
    template: dict[str, Any] = load_json(compiled_dir / "main.json")
    return template


@pytest.fixture(scope="session")
def shared_template(compiled_dir: Path) -> dict[str, Any]:
    template: dict[str, Any] = load_json(compiled_dir / "shared.json")
    return template


@pytest.fixture(scope="session")
def main_deployment(compiled_dir: Path, main_template: dict[str, Any]) -> MainFactory:
    """Evaluates main.json for one environment with its compiled parameter file."""

    def build(
        environment: str, *, config: dict[str, Any] | None = None, **parameters: Any
    ) -> Deployment:
        parameter_file = load_json(compiled_dir / f"main.{environment}.parameters.json")
        values = {key: entry["value"] for key, entry in parameter_file["parameters"].items()}
        values.update(parameters)
        template = with_config(main_template, environment, {"groups": GROUP_IDS, **(config or {})})
        return load_deployment(template, values, resource_group=f"rg-docchat-{environment}")

    return build


@pytest.fixture(scope="session")
def shared_deployment(shared_template: dict[str, Any]) -> SharedFactory:
    """Evaluates shared.json with the parameters a test names over the shipped defaults."""

    def build(**parameters: Any) -> Deployment:
        values: dict[str, Any] = {
            "sharedOperatorIpAllowlist": [],
            "ciPrivateNetworking": False,
            "sharedRegistryLocation": "eastus2",
            "ciNetworkLocation": "eastus2",
            "securityWorkspaceId": SECURITY_WORKSPACE_ID,
        }
        values.update(parameters)
        return load_deployment(shared_template, values, resource_group="rg-docchat-shared")

    return build
