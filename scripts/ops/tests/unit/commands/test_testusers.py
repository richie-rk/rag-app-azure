"""`testusers rotate` and `testusers check` (T-544; `86-operations.md` section 2.8.8;
`84-testing.md` sections 6.1 and 6.2; ADR 0019 item 6)."""

from __future__ import annotations

import json
import string
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from docchat_core.sql.engine import SqlUnavailable
from docchat_ops.commands import testusers
from docchat_ops.commands.testusers import (
    ALIASES,
    DEV_ALIASES,
    LOAD_ALIASES,
    KeyVaultSecrets,
    SecretInfo,
    configured_aliases,
    key_vault_secrets,
    new_password,
    user_domain,
    user_name,
    vault_uri,
)
from docchat_ops.framework.exitcodes import ExitCode, UsageError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from ops.tests.unit.conftest import OpsHarness, Outcome

TICKET = ("--ticket", "R08-2026-10", "--reason", "30-day test password rotation")
GRAPH = "https://graph.microsoft.com/v1.0/users/"
LOGIN = "https://login.microsoftonline.com/"


def _ready(ops: OpsHarness) -> None:
    ops.environ.update(
        {"DOCCHAT_TEST_VAULT_URI": ops.vault_uri, "DOCCHAT_TEST_USER_DOMAIN": ops.domain}
    )
    ops.az.on_json("group", "show", value={"owner": "platform"})
    ops.http.answer("PATCH", GRAPH, 204)
    ops.http.answer("POST", LOGIN, 200, {"access_token": "not-kept", "token_type": "Bearer"})


def _rotate(ops: OpsHarness, user_set: str, *extra: str) -> Outcome:
    return ops.run(
        "--env",
        "dev",
        "--json",
        "testusers",
        "rotate",
        "--set",
        user_set,
        *TICKET,
        "--yes",
        *extra,
    )


def _patched_upns(ops: OpsHarness) -> list[str]:
    return [str(request.url).removeprefix(GRAPH) for request in ops.http.sent("PATCH", GRAPH)]


def _sign_in_failure(code: int) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": "invalid_grant",
            "error_description": f"AADSTS{code}: refused. Trace ID: x",
            "error_codes": [code],
        },
    )


# --- Aliases, arguments and passwords ---------------------------------------------------------


def test_alias_catalogue_and_user_names() -> None:
    assert DEV_ALIASES == ("member", "outsider", "norole", "admin")
    assert LOAD_ALIASES[0] == "load-01"
    assert LOAD_ALIASES[-2:] == ("load-20", "load-admin")
    assert len(ALIASES) == 25
    assert user_name("member") == "docchat-test-member"
    assert user_name("load-07") == "docchat-load-07"
    assert user_name("load-admin") == "docchat-load-admin"


def test_configured_aliases_follow_the_rotation_order() -> None:
    document = {"testUserAliases": ["load-02", "admin", "member"]}
    assert configured_aliases(document) == ("member", "admin", "load-02")


@pytest.mark.parametrize(
    "document",
    [{}, [], {"testUserAliases": "admin"}, {"testUserAliases": [1]}, {"testUserAliases": ["boss"]}],
)
def test_configured_aliases_refuse_a_broken_list(document: object) -> None:
    with pytest.raises(UsageError):
        configured_aliases(document)


def test_vault_uri_from_the_argument_or_the_variable() -> None:
    environ = {"DOCCHAT_TEST_VAULT_URI": "https://KV-docchat-tst-abc123.vault.azure.net"}
    assert vault_uri(None, environ) == "https://kv-docchat-tst-abc123.vault.azure.net/"
    assert vault_uri("https://kv-other.vault.azure.net/", environ) == (
        "https://kv-other.vault.azure.net/"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://kv-docchat-tst-abc123.vault.azure.net/",
        "https://example.com/",
        "https://kv-docchat-tst-abc123.vault.azure.net/secrets",
        "https://kv-docchat-tst-abc123.vault.azure.net/?x=1",
        "https://kv.vault.azure.net.evil.example/",
    ],
)
def test_vault_uri_refuses_other_targets(value: str) -> None:
    with pytest.raises(UsageError):
        vault_uri(value, {})


def test_user_domain() -> None:
    assert user_domain(None, {"DOCCHAT_TEST_USER_DOMAIN": "Contoso.onmicrosoft.com"}) == (
        "contoso.onmicrosoft.com"
    )
    for value in ("", "contoso", "con toso.com", "a@b.com"):
        with pytest.raises(UsageError):
            user_domain(value, {})


def test_password_is_32_characters_of_the_spec_alphabet() -> None:
    alphabet = set(string.ascii_letters + string.digits + "!#%+-.:=?@_~")
    for _ in range(200):
        password = new_password()
        assert len(password) == 32
        assert set(password) <= alphabet
        assert not any(char.isspace() for char in password)


def test_password_is_redrawn_until_every_class_is_present() -> None:
    draws = iter(["a"] * 32 + ["A", "b", "1", "~"] * 8)
    assert new_password(lambda alphabet: next(draws)) == "Ab1~" * 8


# --- rotate -----------------------------------------------------------------------------------


def test_t544_rotate_dev_changes_writes_proves_and_audits(ops: OpsHarness) -> None:
    _ready(ops)
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert _patched_upns(ops) == [f"docchat-test-{alias}@{ops.domain}" for alias in DEV_ALIASES]
    patches = [json.loads(request.content) for request in ops.http.sent("PATCH", GRAPH)]
    passwords = [body["passwordProfile"]["password"] for body in patches]
    assert all(
        body["passwordProfile"]["forceChangePasswordNextSignIn"] is False for body in patches
    )
    assert all(
        request.headers["Authorization"] == "Bearer token-for-https://graph.microsoft.com/.default"
        for request in ops.http.sent("PATCH", GRAPH)
    )
    expected_expiry = ops.now + timedelta(days=30)
    assert ops.vault.writes == [
        {
            "name": f"test-user-{alias}",
            "value": password,
            "content_type": "text/plain",
            "expires_on": expected_expiry,
            "tags": {"docchat-test-user": alias},
        }
        for alias, password in zip(DEV_ALIASES, passwords, strict=True)
    ]
    sign_ins = ops.http.sent("POST", LOGIN)
    assert [str(request.url) for request in sign_ins] == [
        f"{LOGIN}{ops.tenant_id}/oauth2/v2.0/token"
    ] * 4
    form = parse_qs(sign_ins[0].content.decode())
    assert form == {
        "grant_type": ["password"],
        "client_id": [ops.test_client_id],
        "scope": [f"api://{ops.tenant_id}/docchat-dev-abc123-api/access_as_user openid profile"],
        "username": [f"docchat-test-member@{ops.domain}"],
        "password": [passwords[0]],
    }
    assert ops.sleeps == [30, 30, 30, 30]
    (row,) = ops.database.audit_rows
    assert row["action"] == "test_users.rotated"
    assert row["actor_type"] == "operator"
    assert str(row["actor_object_id"]) == ops.operator_oid
    assert row["target_type"] == "test_users"
    assert row["target_id"] is None
    assert json.loads(row["metadata_json"]) == {"environment": "dev", "aliasCount": 4}
    assert row["correlation_id"] == outcome.correlation_id
    assert ops.database.units[-1] == ("write_audit_event", "none")
    assert ops.database.disposed
    assert ops.vault.closed
    report = outcome.json()
    assert report["aliasCount"] == 4
    assert report["aliases"][0] == {"alias": "member", "expiresOn": "2026-11-29T12:00:00.000Z"}
    for password in passwords:
        assert password not in outcome.stdout
        assert password not in outcome.stderr
    assert "contoso" not in outcome.stdout
    assert ops.vault.uris == [ops.vault_uri]


def test_t544_rotate_all_rotates_25_aliases_and_signs_in_only_the_dev_set(
    ops: OpsHarness,
) -> None:
    _ready(ops)
    outcome = _rotate(ops, "all")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert len(_patched_upns(ops)) == 25
    assert [write["name"] for write in ops.vault.writes] == [f"test-user-{a}" for a in ALIASES]
    assert len(ops.http.sent("POST", LOGIN)) == 4
    assert ops.az.called("group", "show") == [
        ("group", "show", "--name", "rg-docchat-load", "--query", "tags", "-o", "json")
    ]
    (row,) = ops.database.audit_rows
    assert json.loads(row["metadata_json"]) == {"environment": "dev", "aliasCount": 25}


def test_t544_rotate_load_makes_no_sign_in(ops: OpsHarness) -> None:
    _ready(ops)
    outcome = _rotate(ops, "load")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert _patched_upns(ops)[0] == f"docchat-load-01@{ops.domain}"
    assert ops.http.sent("POST", LOGIN) == []
    assert ops.sleeps == []
    assert ops.database.audit_rows[0]["metadata_json"] == '{"environment":"dev","aliasCount":21}'


def test_t544_graph_403_exits_3_and_writes_no_secret(ops: OpsHarness) -> None:
    _ready(ops)
    ops.http.answer("PATCH", GRAPH, 403, {"error": {"code": "Authorization_RequestDenied"}})
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert "alias member" in outcome.failure
    assert "Password Administrator" in outcome.failure
    assert ops.vault.writes == []
    assert ops.database.audit_rows == []


@pytest.mark.parametrize(
    "configure",
    [
        lambda db: setattr(db, "connect_error", SqlUnavailable("connection", 40615)),
        lambda db: setattr(db, "token_error", SqlUnavailable("token")),
        lambda db: setattr(db, "connect_error", SqlUnavailable("connection", 18456)),
        lambda db: setattr(db, "token", None),
        lambda db: setattr(db, "token", "not-a-jwt"),
    ],
)
def test_t544_no_sql_connection_exits_3_before_anything_changes(
    ops: OpsHarness, configure: Any
) -> None:
    _ready(ops)
    configure(ops.database)
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert "no SQL connection to dev for the audit row" in outcome.failure
    assert ops.http.requests == []
    assert ops.vault.writes == []


@pytest.mark.parametrize(
    "scope", ["https://graph.microsoft.com/.default", "https://vault.azure.net/.default"]
)
def test_t544_a_missing_token_exits_3(ops: OpsHarness, scope: str) -> None:
    _ready(ops)
    ops.credential.refused.add(scope)
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.http.requests == []
    assert ops.database.units == []


@pytest.mark.parametrize("user_set", ["load", "all"])
def test_t544_a_running_load_test_exits_2(ops: OpsHarness, user_set: str) -> None:
    _ready(ops)
    ops.az.on_json("group", "show", value={"docchat-load-run": "123", "docchat-load-created": "x"})
    outcome = _rotate(ops, user_set)
    assert outcome.code == ExitCode.USAGE
    assert "load_run_in_progress" in outcome.failure
    assert ops.http.requests == []
    assert ops.vault.writes == []


def test_the_dev_set_ignores_the_load_tag(ops: OpsHarness) -> None:
    _ready(ops)
    ops.az.on_json("group", "show", value={"docchat-load-run": "123"})
    assert _rotate(ops, "dev").code == ExitCode.SUCCESS
    assert ops.az.called("group", "show") == []


def test_t544_sign_in_still_failing_after_3_attempts_exits_5_naming_the_alias(
    ops: OpsHarness,
) -> None:
    _ready(ops)
    ops.http.route("POST", LOGIN, lambda request: _sign_in_failure(50126))
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.INCOMPLETE
    assert "alias member" in outcome.failure
    assert ops.sleeps == [30, 30, 30]
    assert len(ops.http.sent("POST", LOGIN)) == 3
    assert [write["name"] for write in ops.vault.writes] == ["test-user-member"]
    assert len(_patched_upns(ops)) == 1
    assert ops.database.audit_rows == []


def test_a_late_sign_in_succeeds_on_a_later_attempt(ops: OpsHarness) -> None:
    _ready(ops)
    ops.http.sequence(
        "POST",
        LOGIN,
        [_sign_in_failure(50126), httpx.Response(200, json={"access_token": "x"})],
    )
    assert _rotate(ops, "dev").code == ExitCode.SUCCESS
    assert ops.sleeps[:2] == [30, 30]


@pytest.mark.parametrize(("code", "hint"), [(50076, "Conditional Access"), (50105, "assigned")])
def test_another_sign_in_refusal_stops_at_once(ops: OpsHarness, code: int, hint: str) -> None:
    _ready(ops)
    ops.http.route("POST", LOGIN, lambda request: _sign_in_failure(code))
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.INCOMPLETE
    assert f"AADSTS{code}" in outcome.failure
    assert hint in outcome.failure
    assert len(ops.http.sent("POST", LOGIN)) == 1


def test_sign_in_without_an_answer_exits_5(ops: OpsHarness) -> None:
    _ready(ops)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    ops.http.route("POST", LOGIN, refuse)
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.INCOMPLETE
    assert "no answer" in outcome.failure


@pytest.mark.parametrize(
    "response", [httpx.Response(500, text="oops"), httpx.Response(400, json=["x"])]
)
def test_sign_in_answers_without_a_code_are_retried(
    ops: OpsHarness, response: httpx.Response
) -> None:
    _ready(ops)
    ops.http.route("POST", LOGIN, lambda request: response)
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.INCOMPLETE
    assert len(ops.http.sent("POST", LOGIN)) == 3


def test_sign_in_code_read_from_the_description(ops: OpsHarness) -> None:
    _ready(ops)
    response = httpx.Response(400, json={"error_description": "AADSTS65001: consent"})
    ops.http.route("POST", LOGIN, lambda request: response)
    outcome = _rotate(ops, "dev")
    assert "AADSTS65001" in outcome.failure
    assert "admin consent" in outcome.failure


def test_a_failed_secret_write_after_the_change_exits_5(ops: OpsHarness) -> None:
    _ready(ops)
    ops.vault.set_errors["test-user-outsider"] = ServiceRequestError("no route")
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.INCOMPLETE
    assert "alias outsider changed but its secret wasn't written" in outcome.failure
    assert len(_patched_upns(ops)) == 2
    assert ops.database.audit_rows == []


def test_a_failed_audit_row_exits_5(ops: OpsHarness) -> None:
    _ready(ops)
    ops.database.write_error = SqlUnavailable("connection", 10054)
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.INCOMPLETE
    assert "audit row wasn't written" in outcome.failure
    assert len(ops.vault.writes) == 4


def test_an_unreachable_vault_stops_before_any_change(ops: OpsHarness) -> None:
    _ready(ops)
    refusal = HttpResponseError(message="refused")
    refusal.status_code = 403
    refusal.error = SimpleNamespace(code="ForbiddenByFirewall")  # type: ignore[assignment]
    ops.vault.list_error = refusal
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.UNREACHABLE
    assert ops.http.requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ExitCode.ACCESS_DENIED),
        (404, ExitCode.NOT_FOUND),
        (429, ExitCode.UNREACHABLE),
        (502, ExitCode.UNREACHABLE),
        (400, ExitCode.UNEXPECTED),
    ],
)
def test_graph_answers_map_to_exit_codes(ops: OpsHarness, status: int, code: ExitCode) -> None:
    _ready(ops)
    ops.http.answer("PATCH", GRAPH, status, {"error": {"message": "x"}})
    outcome = _rotate(ops, "dev")
    assert outcome.code == code
    assert "alias member" in outcome.failure
    assert ops.vault.writes == []


def test_graph_without_an_answer_exits_7(ops: OpsHarness) -> None:
    _ready(ops)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ops.http.route("PATCH", GRAPH, refuse)
    assert _rotate(ops, "dev").code == ExitCode.UNREACHABLE


@pytest.mark.parametrize("environment", ["prod", "load"])
def test_rotate_runs_in_dev_only(ops: OpsHarness, environment: str) -> None:
    _ready(ops)
    outcome = ops.run("--env", environment, "testusers", "rotate", "--set", "dev", *TICKET, "--yes")
    assert outcome.code == ExitCode.USAGE
    assert "use --env dev" in outcome.failure


def test_rotate_needs_the_vault_uri(ops: OpsHarness) -> None:
    _ready(ops)
    del ops.environ["DOCCHAT_TEST_VAULT_URI"]
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.USAGE
    assert ops.az.calls == []


def test_rotate_takes_the_arguments_over_the_variables(ops: OpsHarness) -> None:
    _ready(ops)
    ops.environ.clear()
    outcome = _rotate(
        ops, "dev", "--vault-uri", "https://kv-other.vault.azure.net/", "--domain", "fabrikam.com"
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert ops.vault.uris == ["https://kv-other.vault.azure.net/"]
    assert _patched_upns(ops)[0] == "docchat-test-member@fabrikam.com"


def test_rotate_needs_the_dev_azd_values(ops: OpsHarness) -> None:
    _ready(ops)
    ops.write_azd("dev", TEST_CLIENT_ID="")
    outcome = _rotate(ops, "dev")
    assert outcome.code == ExitCode.USAGE
    assert "TEST_CLIENT_ID is missing" in outcome.failure


def test_rotate_refuses_a_foreign_sql_server(ops: OpsHarness) -> None:
    _ready(ops)
    ops.write_azd("dev", DOCCHAT_SQL_SERVER="evil.example.com")
    assert _rotate(ops, "dev").code == ExitCode.USAGE


def test_rotate_asks_before_changing(ops: OpsHarness) -> None:
    _ready(ops)
    ops.answer("no\n")
    outcome = ops.run("--env", "dev", "testusers", "rotate", "--set", "dev", *TICKET)
    assert outcome.code == ExitCode.USAGE
    assert "set: dev (4 aliases: member, outsider, norole, admin)" in outcome.stderr
    assert "vault: kv-docchat-tst-abc123.vault.azure.net" in outcome.stderr
    assert ops.http.requests == []


def test_rotate_needs_an_alias_of_the_set(ops: OpsHarness) -> None:
    _ready(ops)
    ops.write_bootstrap(["admin"])
    assert _rotate(ops, "load").code == ExitCode.USAGE


def test_rotate_with_a_table_report(ops: OpsHarness) -> None:
    _ready(ops)
    outcome = ops.run("--env", "dev", "testusers", "rotate", "--set", "dev", *TICKET, "--yes")
    assert outcome.code == ExitCode.SUCCESS
    assert outcome.stdout.splitlines()[:2] == ["result: rotated", "aliasCount: 4"]


# --- check ------------------------------------------------------------------------------------


def _all_valid(ops: OpsHarness, days: int = 20) -> None:
    for alias in ops.all_aliases:
        ops.vault.expire(alias, ops.now + timedelta(days=days))


def _check(ops: OpsHarness, *extra: str) -> Outcome:
    return ops.run(
        "--env",
        "dev",
        "--json",
        "testusers",
        "check",
        "--set",
        "all",
        "--vault-uri",
        ops.vault_uri,
        *extra,
    )


def test_t544_check_passes_with_no_warning(ops: OpsHarness) -> None:
    _all_valid(ops)
    outcome = _check(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    document = outcome.json()
    assert document["result"] == "passed"
    assert document["warnings"] == []
    assert [item["alias"] for item in document["secrets"]] == list(ALIASES)
    assert document["secrets"][0] == {
        "alias": "member",
        "expiresOn": "2026-11-19T12:00:00.000Z",
        "status": "ok",
    }
    assert ops.vault.writes == []
    assert ops.credential.closed
    assert ops.vault.closed


def test_t544_check_lists_warnings_within_7_days(ops: OpsHarness) -> None:
    _all_valid(ops)
    ops.vault.expire("load-03", ops.now + timedelta(days=6))
    outcome = _check(ops)
    assert outcome.code == ExitCode.SUCCESS
    assert outcome.json()["warnings"] == ["load-03"]
    assert "::warning::" not in outcome.stderr


def test_check_annotates_warnings_in_github_actions(ops: OpsHarness) -> None:
    _all_valid(ops)
    ops.vault.expire("admin", ops.now + timedelta(days=2))
    ops.environ["GITHUB_ACTIONS"] = "true"
    outcome = ops.run(
        "--env", "dev", "testusers", "check", "--set", "dev", "--vault-uri", ops.vault_uri
    )
    assert outcome.code == ExitCode.SUCCESS
    assert "::warning::test-user-admin expires within 7 days" in outcome.stderr
    assert "warnings" not in outcome.stdout
    assert "admin" in outcome.stdout


def test_t544_check_exits_5_for_an_expired_secret(ops: OpsHarness) -> None:
    _all_valid(ops)
    ops.vault.expire("norole", ops.now - timedelta(minutes=1))
    outcome = _check(ops)
    assert outcome.code == ExitCode.INCOMPLETE
    assert "norole" in outcome.failure
    assert outcome.json()["result"] == "failed"


def test_t544_check_exits_5_within_min_hours(ops: OpsHarness) -> None:
    _all_valid(ops)
    ops.vault.expire("load-01", ops.now + timedelta(hours=23))
    assert _check(ops, "--min-hours", "24").code == ExitCode.INCOMPLETE
    assert _check(ops, "--min-hours", "22").code == ExitCode.SUCCESS


def test_check_refuses_a_malformed_min_hours(ops: OpsHarness) -> None:
    assert _check(ops, "--min-hours", "-3").code == ExitCode.USAGE


def test_check_fails_a_secret_without_expiry(ops: OpsHarness) -> None:
    _all_valid(ops)
    ops.vault.expire("member", None)
    outcome = _check(ops)
    assert outcome.code == ExitCode.INCOMPLETE
    assert outcome.json()["secrets"][0]["status"] == "no_expiry"


def test_check_exits_4_for_a_missing_secret(ops: OpsHarness) -> None:
    _all_valid(ops)
    del ops.vault.secrets["test-user-load-20"]
    ops.vault.expire("member", ops.now - timedelta(days=1))
    outcome = _check(ops)
    assert outcome.code == ExitCode.NOT_FOUND
    assert "load-20" in outcome.failure


def test_check_reads_only_the_selected_set(ops: OpsHarness) -> None:
    ops.vault.expire("member", ops.now + timedelta(days=20))
    for alias in DEV_ALIASES:
        ops.vault.expire(alias, ops.now + timedelta(days=20))
    ops.vault.secrets["unrelated"] = SecretInfo("unrelated", None)
    outcome = ops.run(
        "--env",
        "load",
        "--json",
        "testusers",
        "check",
        "--set",
        "dev",
        "--vault-uri",
        ops.vault_uri,
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["environment"] == "load"


@pytest.mark.parametrize("environment", ["prod", "local"])
def test_check_runs_in_dev_and_load_only(ops: OpsHarness, environment: str) -> None:
    outcome = ops.run(
        "--env", environment, "testusers", "check", "--set", "all", "--vault-uri", ops.vault_uri
    )
    assert outcome.code == ExitCode.USAGE


def test_check_reads_no_azd_file(ops: OpsHarness) -> None:
    _all_valid(ops)
    (ops.root / ".azure" / "dev" / ".env").unlink()
    ops.environ["DOCCHAT_TEST_VAULT_URI"] = ops.vault_uri
    outcome = ops.run("--env", "dev", "testusers", "check", "--set", "all")
    assert outcome.code == ExitCode.SUCCESS


def test_check_closes_the_vault_when_listing_fails(ops: OpsHarness) -> None:
    ops.vault.list_error = ServiceRequestError("no route")
    assert _check(ops).code == ExitCode.UNREACHABLE
    assert ops.vault.closed


# --- The Key Vault adapter --------------------------------------------------------------------


class _Client:
    def __init__(self) -> None:
        self.set_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.closed = False

    async def _items(self) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(name="test-user-member", expires_on=None)
        yield SimpleNamespace(name=None, expires_on=None)

    def list_properties_of_secrets(self) -> AsyncIterator[SimpleNamespace]:
        return self._items()

    async def set_secret(self, *args: Any, **kwargs: Any) -> None:
        self.set_calls.append((args, kwargs))

    async def close(self) -> None:
        self.closed = True


async def test_key_vault_adapter_maps_the_client_calls(ops: OpsHarness) -> None:
    client = _Client()
    adapter = KeyVaultSecrets(client)  # type: ignore[arg-type]
    found = [info async for info in adapter.list_properties()]
    assert found == [SecretInfo("test-user-member", None)]
    expires: datetime = ops.now
    await adapter.set_secret(
        "test-user-member", "pw", content_type="text/plain", expires_on=expires, tags={"a": "b"}
    )
    assert client.set_calls == [
        (
            ("test-user-member", "pw"),
            {"content_type": "text/plain", "expires_on": expires, "tags": {"a": "b"}},
        )
    ]
    await adapter.close()
    assert client.closed


async def test_key_vault_client_has_the_tools_timeouts(ops: OpsHarness) -> None:
    secrets = key_vault_secrets(ops.vault_uri, ops.credential)
    assert isinstance(secrets, KeyVaultSecrets)
    client = secrets._client
    assert client.vault_url == ops.vault_uri.rstrip("/")
    transport = client._client._client._pipeline._transport
    assert transport.connection_config.timeout == testusers.KEY_VAULT_CONNECTION_TIMEOUT
    assert transport.connection_config.read_timeout == testusers.KEY_VAULT_READ_TIMEOUT
    await secrets.close()
