"""The deployed suites' session (`84-testing.md` sections 6.3 to 6.5).

Run as `uv run pytest tests/deployed --deployed -m <suite>`. Two sources of values:

- the token-holding `pytest` job of `deployed-tests.yaml` (`dev-tests` or `load-tests`, as
  `id-docchat-citest`) passes the `endpoints` job's outputs and the test variables in its
  environment; `deployed_tokens` runs the token step of section 6.4 once per session: the
  passwords inside the vault's read window, one ROPC token per alias, each masked and checked
  against its alias's row, before any test runs;
- the `infra-checks` job (the `dev` deploy identity) has `.azure/dev/.env` from
  `azd env refresh`, read by `azd_environment`; so has the `deploy` job of
  `deploy-environment.yaml` for its own environment (`AZURE_ENV_NAME`), whose deploy checks run
  single tests from here.

`prod_smoke` reads only `API_BASE_URL` and `WEB_BASE_URL` and holds no token.

The token step signs in every suite role of the environment, except in the `resume-start` job,
whose only test is the start of the crash-resume test: it signs in `admin` alone (section 6.7
step 1). `seed_data` is the session fixture's seed data of section 6.6, created once per session
through the admin API before the first test that needs it.

Every test here gets the 900-second timeout of section 1.2 unless it names its own.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio

from docchat_testing.adminapi import AdminApi
from docchat_testing.apiclient import (
    DeployedApiClient,
    DeployedEnvironment,
    EnvironmentIncompleteError,
)
from docchat_testing.fixtures.session import run_on_private_loop
from docchat_testing.seed import SeedData, ensure_seed_data
from docchat_testing.tokens import (
    DeployedTokens,
    TokenStepError,
    acquire_ropc_token,
    load_passwords,
)

DEPLOYED_TEST_TIMEOUT_SECONDS: Final = 900
SEED_TIMEOUT_SECONDS: Final = 840.0
"""The seed data, its ingestion included, inside the first test's 900 s, so a slow first
ingestion fails with the seed's own message rather than the test timeout's."""
HERE: Final = Path(__file__).resolve().parent
REPOSITORY_ROOT: Final = HERE.parents[1]
RESUME_MARKER: Final = "resume"
HTTP_OK: Final = 200
AZD_ENVIRONMENTS: Final = frozenset({"dev", "prod", "load"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.resolve().is_relative_to(HERE) and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(DEPLOYED_TEST_TIMEOUT_SECONDS))


@pytest.fixture(scope="session")
def deployed_environment() -> DeployedEnvironment:
    try:
        return DeployedEnvironment.from_environ(os.environ)
    except EnvironmentIncompleteError as error:
        pytest.fail(f"{error}; the deployed suites run in deployed-tests.yaml", pytrace=False)


def token_roles(items: list[pytest.Item], roles: dict[str, str]) -> dict[str, str]:
    """The suite roles the session signs in: `admin` alone when every selected test is the
    start of the crash-resume test (section 6.7 step 1), every role otherwise."""
    if items and all(item.get_closest_marker(RESUME_MARKER) for item in items):
        return {role: alias for role, alias in roles.items() if role == "admin"}
    return roles


@pytest.fixture(scope="session")
def deployed_tokens(
    request: pytest.FixtureRequest, deployed_environment: DeployedEnvironment
) -> DeployedTokens:
    """The token step (section 6.4): runs on a private loop before the first test that needs a
    token; a failure ends the session with its code, so no test runs on a broken step."""
    aliases = list(token_roles(request.session.items, deployed_environment.roles).values())
    try:
        passwords = run_on_private_loop(
            lambda: load_passwords(
                deployed_environment.vault_uri, aliases, environment=deployed_environment.name
            )
        )
        held = DeployedTokens(
            passwords,
            api_client_id=deployed_environment.api_client_id,
            acquire=lambda alias, password: acquire_ropc_token(
                alias, password=password, request=deployed_environment.ropc_request()
            ),
        )
        run_on_private_loop(held.acquire_all)
    except TokenStepError as error:
        label = f"{error.code}: " if error.code else ""
        pytest.exit(f"token step failed: {label}{error}", returncode=1)
    return held


type ClientFactory = Callable[[str | None], DeployedApiClient]


@pytest_asyncio.fixture
async def api_client(
    deployed_environment: DeployedEnvironment, deployed_tokens: DeployedTokens
) -> AsyncIterator[ClientFactory]:
    """`api_client(role)`: a `DeployedApiClient` of the deployed API as the suite role's alias
    (`None`: anonymous), closed after the test."""
    opened: list[DeployedApiClient] = []

    def factory(role: str | None) -> DeployedApiClient:
        if role is None:
            client = DeployedApiClient(deployed_environment.api_base_url)
        else:
            if role not in deployed_environment.roles:
                pytest.skip(f"no {role} test user in {deployed_environment.name}")
            client = role_client(deployed_environment, deployed_tokens, role)
        opened.append(client)
        return client

    try:
        yield factory
    finally:
        for client in opened:
            await client.aclose()


def role_client(
    environment: DeployedEnvironment, tokens: DeployedTokens, role: str
) -> DeployedApiClient:
    """A client of the deployed API as the suite role's alias; it fetches the alias's current
    token for every call."""
    alias = environment.roles[role]

    async def token() -> str:
        return await tokens.token(alias)

    return DeployedApiClient(environment.api_base_url, token)


@pytest.fixture
def admin_api(api_client: ClientFactory) -> AdminApi:
    """The administrator operations as `admin`, on a client `api_client` closes."""
    return AdminApi(api_client("admin"))


@pytest.fixture(scope="session")
def run_label() -> str:
    """What names a run's uploads: the GitHub run ID in the workflow, or 8 random hex digits
    when the suite runs elsewhere."""
    return os.environ.get("GITHUB_RUN_ID") or secrets.token_hex(4)


@pytest.fixture(scope="session")
def seed_data(
    deployed_environment: DeployedEnvironment, deployed_tokens: DeployedTokens
) -> SeedData:
    """The seed data of section 6.6, through the admin API as `admin`: `member` signs in once
    first (`getCurrentUser` provisions the user), because only signed-in people can be added to
    a project."""
    environment = deployed_environment

    async def seed() -> SeedData:
        async with (
            role_client(environment, deployed_tokens, "member") as member,
            role_client(environment, deployed_tokens, "admin") as admin,
        ):
            me = await member.request("GET", "/v1/me")
            if me.status_code != HTTP_OK:
                pytest.fail(f"getCurrentUser as member answered {me.status_code}", pytrace=False)
            member_id = str(me.json()["userId"])
            return await ensure_seed_data(
                AdminApi(admin), member_id=member_id, within=SEED_TIMEOUT_SECONDS - 60
            )

    return run_on_private_loop(seed, timeout=SEED_TIMEOUT_SECONDS)


def read_azd_values(path: Path) -> dict[str, str]:
    """The `KEY="value"` lines azd writes to `.azure/<env>/.env`."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip('"')
    return values


@pytest.fixture(scope="session")
def azd_environment_name() -> str:
    """The environment of the azd values: `dev` in the `infra-checks` job; the deploy job's own
    environment, named by `AZURE_ENV_NAME`, for the checks it runs from here (T-436 in `prod`,
    T-437 and T-472 after each provision)."""
    name = os.environ.get("AZURE_ENV_NAME") or "dev"
    if name not in AZD_ENVIRONMENTS:
        pytest.fail(f"AZURE_ENV_NAME {name!r} isn't dev, prod or load", pytrace=False)
    return name


@pytest.fixture(scope="session")
def azd_environment(azd_environment_name: str) -> dict[str, str]:
    """`.azure/<env>/.env` of a job that ran `azd env refresh` as that environment's deploy
    identity: `infra-checks` (`dev`) or the `deploy` job of `deploy-environment.yaml`."""
    path = REPOSITORY_ROOT / ".azure" / azd_environment_name / ".env"
    if not path.is_file():
        pytest.fail(f"{path} is missing; the job writes it with azd env refresh")
    return read_azd_values(path)
