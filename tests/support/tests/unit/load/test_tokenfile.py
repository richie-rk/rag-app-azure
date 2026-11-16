from __future__ import annotations

import json
import stat
import time
from collections.abc import Sequence
from pathlib import Path

import jwt
import pytest

from docchat_testing.load.common import UsageError
from docchat_testing.load.tokenfile import write_token_file

API_CLIENT = "8e3f1a52-4c6d-4b7e-a9f0-2c1d3e4f5a6b"
VAULT = "https://kv-docchat-tst-abc123.vault.azure.net/"
PASSWORD = "p@ss-word-not-a-real-one-0123456789"


def token_for(alias: str) -> str:
    claims = {
        "aud": API_CLIENT,
        "scp": "access_as_user",
        "roles": ["Admin"] if alias == "load-admin" else ["User"],
        "exp": int(time.time()) + 3600,
        "alias": alias,
    }
    return jwt.encode(claims, "secret-for-the-test-only-32-bytes!", algorithm="HS256")


class Vault:
    def __init__(self) -> None:
        self.reads: list[tuple[str, list[str], str]] = []

    async def __call__(self, vault_uri: str, aliases: Sequence[str], env: str) -> dict[str, str]:
        self.reads.append((vault_uri, list(aliases), env))
        return dict.fromkeys(aliases, PASSWORD)


async def acquire(alias: str, password: str) -> str:
    assert password == PASSWORD
    return token_for(alias)


async def test_the_token_file_holds_tokens_only_with_mode_0600(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = Vault()
    out = tmp_path / "tokens.json"
    aliases = ["load-01", "load-02", "load-admin"]

    tokens = await write_token_file(
        "load",
        aliases,
        out,
        environ={"DOCCHAT_TEST_VAULT_URI": VAULT, "API_CLIENT_ID": API_CLIENT},
        passwords=vault,
        acquire=acquire,
    )

    assert vault.reads == [(VAULT, aliases, "load")]
    written = out.read_text(encoding="utf-8")
    assert json.loads(written) == {"tokens": tokens}
    assert set(tokens) == set(aliases)
    assert PASSWORD not in written
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    printed = capsys.readouterr().out
    for token in tokens.values():
        assert f"::add-mask::{token}" in printed


async def test_the_api_client_id_comes_from_the_azd_file_when_the_job_lacks_it(
    tmp_path: Path,
) -> None:
    tokens = await write_token_file(
        "load",
        ["load-01"],
        tmp_path / "t.json",
        environ={"DOCCHAT_TEST_VAULT_URI": VAULT},
        azd={"API_CLIENT_ID": API_CLIENT},
        passwords=Vault(),
        acquire=acquire,
    )
    assert list(tokens) == ["load-01"]


async def test_a_token_for_another_api_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="aud isn't the API client ID"):
        await write_token_file(
            "load",
            ["load-01"],
            tmp_path / "t.json",
            environ={"DOCCHAT_TEST_VAULT_URI": VAULT, "API_CLIENT_ID": "another-client"},
            passwords=Vault(),
            acquire=acquire,
        )
    assert not (tmp_path / "t.json").exists()


async def test_the_vault_uri_is_required(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="DOCCHAT_TEST_VAULT_URI"):
        await write_token_file(
            "load",
            ["load-01"],
            tmp_path / "t.json",
            environ={"API_CLIENT_ID": API_CLIENT},
            passwords=Vault(),
            acquire=acquire,
        )
