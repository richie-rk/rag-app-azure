"""The operator's identity: caller type, tokens, the actor and the Azure CLI subprocess
(`86-operations.md` section 2.2)."""

from __future__ import annotations

import base64
import json
import stat
from typing import TYPE_CHECKING

import pytest
from azure.identity.aio import AzureCliCredential

from docchat_ops.framework import auth
from docchat_ops.framework.auth import (
    AzResult,
    SubprocessAz,
    acquire_token,
    az_failure,
    az_json,
    az_text,
    caller_type,
    refuse_service_principal,
    signed_in_user,
    token_object_id,
)
from docchat_ops.framework.exitcodes import (
    AccessDenied,
    ExitCode,
    NotFound,
    UnexpectedError,
    Unreachable,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ops.tests.unit.conftest import FakeAz, FakeCredential

OID = "0f1e2d3c-4b5a-4697-8877-665544332211"


def _token(claims: object) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("ERROR: Please run 'az login' to setup account.", ExitCode.ACCESS_DENIED),
        ("AADSTS70043: The refresh token has expired", ExitCode.ACCESS_DENIED),
        ("(AuthorizationFailed) The client 'a@b.example' has no rights", ExitCode.ACCESS_DENIED),
        ("Insufficient privileges to complete the operation.", ExitCode.ACCESS_DENIED),
        ("(ResourceGroupNotFound) Resource group 'x' could not be found.", ExitCode.NOT_FOUND),
        ("Resource 'x' does not exist", ExitCode.NOT_FOUND),
        ("Max retries exceeded with url", ExitCode.UNREACHABLE),
        ("Connection aborted.", ExitCode.UNREACHABLE),
        ("something else entirely", ExitCode.UNEXPECTED),
    ],
)
def test_az_failures_map_to_exit_codes(stderr: str, code: ExitCode) -> None:
    failure = az_failure(stderr, "adding the member")
    assert failure.exit_code == code
    assert "adding the member" in failure.cause
    assert "a@b.example" not in failure.cause


async def test_az_text_returns_the_stripped_output(fake_az: FakeAz) -> None:
    fake_az.on("group", "show", stdout="  value \n")
    assert await az_text(fake_az, ["group", "show"], "reading") == "value"


async def test_az_text_raises_the_mapped_failure(fake_az: FakeAz) -> None:
    fake_az.fail("group", "show", stderr="ResourceGroupNotFound")
    with pytest.raises(NotFound):
        await az_text(fake_az, ["group", "show"], "reading")


async def test_az_json_parses_the_output(fake_az: FakeAz) -> None:
    fake_az.on_json("group", "show", value={"a": 1})
    assert await az_json(fake_az, ["group", "show"], "reading") == {"a": 1}
    fake_az.on("group", "show", stdout="")
    assert await az_json(fake_az, ["group", "show"], "reading") is None
    fake_az.on("group", "show", stdout="not json")
    with pytest.raises(UnexpectedError):
        await az_json(fake_az, ["group", "show"], "reading")


async def test_caller_type_reads_the_account(fake_az: FakeAz) -> None:
    assert await caller_type(fake_az) == "user"
    assert fake_az.calls == [("account", "show", "--query", "user.type", "-o", "tsv")]


@pytest.mark.parametrize("environment", ["dev", "prod"])
@pytest.mark.parametrize("kind", ["servicePrincipal", "managedIdentity", ""])
async def test_non_person_callers_are_refused_outside_load(
    fake_az: FakeAz, environment: str, kind: str
) -> None:
    fake_az.on("account", "show", stdout=f"{kind}\n")
    with pytest.raises(AccessDenied):
        await refuse_service_principal(fake_az, environment)


async def test_a_person_is_accepted(fake_az: FakeAz) -> None:
    await refuse_service_principal(fake_az, "prod")


async def test_load_accepts_any_caller_without_asking(fake_az: FakeAz) -> None:
    await refuse_service_principal(fake_az, "load")
    assert fake_az.calls == []


async def test_signed_in_user_is_a_lower_case_object_id(fake_az: FakeAz) -> None:
    fake_az.on("ad", "signed-in-user", "show", stdout=OID.upper())
    assert await signed_in_user(fake_az) == OID
    fake_az.on("ad", "signed-in-user", "show", stdout="nobody")
    with pytest.raises(AccessDenied):
        await signed_in_user(fake_az)


async def test_acquire_token_returns_the_token(fake_credential: FakeCredential) -> None:
    token = await acquire_token(fake_credential, auth.GRAPH_SCOPE, "Microsoft Graph")
    assert token == "token-for-https://graph.microsoft.com/.default"


async def test_acquire_token_without_sign_in_exits_3(fake_credential: FakeCredential) -> None:
    fake_credential.refused.add(auth.LOGS_SCOPE)
    with pytest.raises(AccessDenied) as raised:
        await acquire_token(fake_credential, auth.LOGS_SCOPE, "the Logs query API")
    assert raised.value.next_step == "sign in with az login"


def test_scopes() -> None:
    assert auth.GRAPH_SCOPE == "https://graph.microsoft.com/.default"
    assert auth.LOGS_SCOPE == "https://api.loganalytics.io/.default"
    assert auth.KEY_VAULT_SCOPE == "https://vault.azure.net/.default"


def test_token_object_id_reads_the_oid_claim() -> None:
    assert token_object_id(_token({"oid": OID.upper(), "tid": "x"})) == OID


@pytest.mark.parametrize(
    "token", ["", "no-dots", _token({"tid": "x"}), _token(["oid"]), _token({"oid": "x"}), "a.!!!.c"]
)
def test_token_object_id_refuses_a_token_without_a_caller(token: str) -> None:
    with pytest.raises(AccessDenied):
        token_object_id(token)


async def test_cli_credential_is_the_azure_cli_one() -> None:
    credential = auth.cli_credential()
    assert isinstance(credential, AzureCliCredential)
    await credential.close()


def _fake_az(directory: Path, script: str) -> None:
    executable = directory / "az"
    executable.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)


async def test_subprocess_az_runs_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_az(tmp_path, 'echo "out:$*"; echo "err" >&2; exit 3')
    monkeypatch.setenv("PATH", str(tmp_path))
    result = await SubprocessAz().run(["account", "show"])
    assert result == AzResult(returncode=3, stdout="out:account show\n", stderr="err\n")


async def test_subprocess_az_without_the_cli_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(AccessDenied):
        await SubprocessAz().run(["account", "show"])


async def test_subprocess_az_that_hangs_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_az(tmp_path, "exec sleep 30")
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(Unreachable):
        await SubprocessAz(timeout=0.2).run(["account", "show"])
