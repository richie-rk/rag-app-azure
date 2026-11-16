"""`docchat-load tokens` (`84-testing.md` sections 6.4 and 7.5 step 3).

`docchat-load tokens --env load --aliases load-01..load-20,load-admin --out <file>` reads the
aliases' passwords from the shared test vault inside the job's read window, exchanges each for an
access token through the run's test client, masks every token in the job log before anything else
is printed, checks each token's claims and writes `{"tokens": {alias: token}}` to the output file
with mode 0600. Passwords stay in memory and never reach the file.

The job's variables give the vault, the tenant, the test client, the API scope and the user
domain; the API's client ID, which the claim check compares `aud` with, comes from the job or from
`.azure/<env>/.env`.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from docchat_testing.load.common import read_azd_environment, setting, write_json
from docchat_testing.tokens import DeployedTokens, load_passwords

type PasswordLoader = Callable[[str, Sequence[str], str], Awaitable[dict[str, str]]]
type TokenAcquirer = Callable[[str, str], Awaitable[str]]

VAULT_VARIABLE = "DOCCHAT_TEST_VAULT_URI"
API_CLIENT_VARIABLE = "API_CLIENT_ID"


async def default_passwords(
    vault_uri: str, aliases: Sequence[str], environment: str
) -> dict[str, str]:
    return await load_passwords(vault_uri, aliases, environment=environment)


async def write_token_file(
    environment: str,
    aliases: Sequence[str],
    out: Path,
    *,
    environ: Mapping[str, str] | None = None,
    azd: Mapping[str, str] | None = None,
    passwords: PasswordLoader = default_passwords,
    acquire: TokenAcquirer | None = None,
) -> dict[str, str]:
    """The token step; returns the tokens it wrote."""
    env = os.environ if environ is None else environ
    azd_values = (
        azd
        if azd is not None or env.get(API_CLIENT_VARIABLE)
        else read_azd_environment(environment)
    )
    vault_uri = setting(VAULT_VARIABLE, environ=env)
    api_client_id = setting(API_CLIENT_VARIABLE, environ=env, azd=azd_values)
    loaded = await passwords(vault_uri, aliases, environment)
    held = DeployedTokens(loaded, api_client_id=api_client_id, acquire=acquire)
    await held.acquire_all()
    tokens = {alias: await held.token(alias) for alias in aliases}
    write_json(out, {"tokens": tokens}, private=True)
    return tokens
