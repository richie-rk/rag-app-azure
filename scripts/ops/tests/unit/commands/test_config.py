"""`config show`, `config set` and `config reset` (T-541; `86-operations.md` section 2.8.2;
`81-config.md` section 3.3)."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

from docchat_core.chat.models import MODEL_CAPABILITIES
from docchat_core.opsconfig import CATALOGUE, encode_value
from docchat_core.sql.engine import SqlUnavailable
from docchat_ops.commands import chat, config
from docchat_ops.framework.exitcodes import ExitCode

if TYPE_CHECKING:
    from ops.tests.unit.conftest import OpsHarness, Outcome

TICKET = ("--ticket", "CHG-2026-77", "--reason", "raise the write limit for the pilot")
API_KEY = "api.write_requests_per_minute"


def _seed(ops: OpsHarness) -> None:
    database = ops.database
    database.seed(API_KEY, 120)
    database.seed("quota.tokens_per_day", 200_000)
    database.seed("quota.tokens_per_month", 3_000_000)
    database.seed("ratelimit.chat_requests_per_minute", 3)
    database.seed("ratelimit.max_concurrent_streams", 2)
    database.seed("ratelimit.citation_downloads_per_minute", 30)
    database.seed("ratelimit.max_concurrent_downloads", 2)


def _set(ops: OpsHarness, key: str, value: str, *extra: str) -> Outcome:
    return ops.run(
        "--env",
        "dev",
        "--json",
        "config",
        "set",
        "--key",
        key,
        "--value",
        value,
        *TICKET,
        "--yes",
        *extra,
    )


def _stored(ops: OpsHarness, key: str) -> Any:
    return json.loads(ops.database.ops_rows[key]["value_json"])["value"]


def test_key_must_be_in_the_catalogue() -> None:
    assert len(CATALOGUE) == 73
    assert config.key(API_KEY) == API_KEY
    with pytest.raises(argparse.ArgumentTypeError):
        config.key("api.unknown")


@pytest.mark.parametrize("value", ["", "four", "NaN", "Infinity", "{", "'low'"])
def test_value_must_be_json(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        config.json_value(value)


def test_value_is_parsed_as_data() -> None:
    assert config.json_value('{"a": [1, "b", null]}') == {"a": [1, "b", None]}


@pytest.mark.parametrize(
    "argv",
    [
        ("config", "set", "--key", "api.unknown", "--value", "1", *TICKET),
        ("config", "set", "--key", API_KEY, "--value", "NaN", *TICKET),
        ("config", "reset", "--key", "nope", *TICKET),
        ("config", "show", "--key", "nope"),
    ],
)
def test_unknown_keys_and_bad_values_exit_2_before_sign_in(
    ops: OpsHarness, argv: tuple[str, ...]
) -> None:
    outcome = ops.run("--env", "dev", *argv)
    assert outcome.code == ExitCode.USAGE
    assert ops.credentials_made == 0
    assert ops.database.units == []


# --- show -------------------------------------------------------------------------------------


def test_show_prints_every_key(ops: OpsHarness) -> None:
    _seed(ops)
    ops.database.ops_rows[API_KEY].update(
        value_json=encode_value(60), updated_by=UUID(ops.operator_oid.upper())
    )
    outcome = ops.run("--env", "dev", "--json", "config", "show")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    keys = outcome.json()["keys"]
    assert [item["key"] for item in keys] == sorted(ops.database.ops_rows)
    assert keys[0] == {
        "key": API_KEY,
        "value": 60,
        "default": 120,
        "updatedAt": "2026-10-01T08:30:00.000Z",
        "updatedBy": ops.operator_oid,
    }
    assert keys[1]["updatedBy"] is None
    assert ops.database.units == [("ops_config_read", "transient")]
    assert ops.database.disposed
    assert ops.az.calls == []
    assert "raise the write limit" not in outcome.stdout


def test_show_one_key_as_a_table(ops: OpsHarness) -> None:
    _seed(ops)
    outcome = ops.run("--env", "dev", "config", "show", "--key", "quota.tokens_per_day")
    assert outcome.code == ExitCode.SUCCESS
    lines = outcome.stdout.splitlines()
    assert lines[1].split() == list(config.SHOW_COLUMNS)
    assert lines[2].split()[:3] == ["quota.tokens_per_day", "200000", "200000"]
    assert len(lines) == 3


def test_show_of_a_key_without_a_row_exits_4(ops: OpsHarness) -> None:
    assert ops.run("--env", "prod", "config", "show", "--key", API_KEY).code == ExitCode.NOT_FOUND


def test_show_prints_an_unreadable_stored_value_as_null(ops: OpsHarness) -> None:
    _seed(ops)
    ops.database.ops_rows[API_KEY]["value_json"] = '{"value": 1, "value": 2}'
    ops.database.ops_rows[API_KEY]["updated_at"] = None
    outcome = ops.run("--env", "dev", "--json", "config", "show", "--key", API_KEY)
    item = outcome.json()["keys"][0]
    assert item["value"] is None
    assert item["updatedAt"] is None


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SqlUnavailable("token"), ExitCode.ACCESS_DENIED),
        (SqlUnavailable("connection", 18456), ExitCode.ACCESS_DENIED),
        (SqlUnavailable("connection", 40615), ExitCode.UNREACHABLE),
    ],
)
def test_t031_show_without_access_exits_3_or_7(
    ops: OpsHarness, error: BaseException, code: ExitCode
) -> None:
    ops.database.unit_errors["ops_config_read"] = error
    outcome = ops.run("--env", "dev", "config", "show")
    assert outcome.code == code
    assert ops.database.disposed


def test_show_accepts_a_service_principal(ops: OpsHarness) -> None:
    _seed(ops)
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    assert ops.run("--env", "prod", "config", "show").code == ExitCode.SUCCESS


# --- set and reset ----------------------------------------------------------------------------


def test_set_updates_the_row_and_writes_its_audit_row(ops: OpsHarness) -> None:
    _seed(ops)
    outcome = _set(ops, API_KEY, "60")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    row = ops.database.ops_rows[API_KEY]
    assert row["value_json"] == '{"value":60}'
    assert row["change_reason"] == "raise the write limit for the pilot"
    assert row["updated_by"] == UUID(ops.operator_oid)
    (audit,) = ops.database.audit_rows
    assert audit["action"] == "ops_config.set"
    assert audit["actor_type"] == "operator"
    assert str(audit["actor_object_id"]) == ops.operator_oid
    assert audit["target_type"] == "ops_config"
    assert audit["target_id"] == API_KEY
    assert json.loads(audit["metadata_json"]) == {
        "area": "api",
        "previous": 120,
        "new": 60,
        "ticket": "CHG-2026-77",
    }
    assert audit["correlation_id"] == outcome.correlation_id
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("ops_config_read", "transient"),
        ("ops_config_change", "none"),
    ]
    report = outcome.json()
    assert {key: report[key] for key in ("result", "key", "area", "previous", "new")} == {
        "result": "set",
        "key": API_KEY,
        "area": "api",
        "previous": 120,
        "new": 60,
    }
    assert report["liveWithinSeconds"] == 60
    assert "the value is live within 60 seconds" in outcome.stderr
    assert "previous: 120" in outcome.stderr
    assert "new: 60" in outcome.stderr
    assert "raise the write limit" not in outcome.stdout
    assert ops.database.disposed


def test_set_out_of_range_exits_2_quoting_the_range(ops: OpsHarness) -> None:
    _seed(ops)
    outcome = _set(ops, API_KEY, "5000")
    assert outcome.code == ExitCode.USAGE
    assert API_KEY in outcome.failure
    assert "less than or equal to 1200" in outcome.failure
    assert _stored(ops, API_KEY) == 120
    assert ops.database.audit_rows == []
    assert ("ops_config_change", "none") not in ops.database.units


def test_set_of_the_wrong_type_exits_2(ops: OpsHarness) -> None:
    _seed(ops)
    assert _set(ops, API_KEY, '"60"').code == ExitCode.USAGE
    assert _set(ops, API_KEY, "60.5").code == ExitCode.USAGE


def test_set_breaking_a_cross_field_rule_exits_2(ops: OpsHarness) -> None:
    _seed(ops)
    outcome = _set(ops, "quota.tokens_per_month", "100000")
    assert outcome.code == ExitCode.USAGE
    assert "quota.tokens_per_month, quota.tokens_per_day" in outcome.failure
    assert _stored(ops, "quota.tokens_per_month") == 3_000_000


def test_set_validates_against_the_area_current_values(ops: OpsHarness) -> None:
    _seed(ops)
    ops.database.ops_rows["ratelimit.max_concurrent_streams"]["value_json"] = "not json"
    outcome = _set(ops, "ratelimit.chat_requests_per_minute", "4")
    assert outcome.code == ExitCode.USAGE
    assert "ratelimit.max_concurrent_streams" in outcome.failure


def test_set_repairs_an_unreadable_stored_value(ops: OpsHarness) -> None:
    _seed(ops)
    ops.database.ops_rows[API_KEY]["value_json"] = "not json"
    outcome = _set(ops, API_KEY, "100")
    assert outcome.code == ExitCode.SUCCESS
    assert json.loads(ops.database.audit_rows[0]["metadata_json"])["previous"] is None


MEMORY = {
    "memory.enabled": True,
    "memory.max_facts": 50,
    "memory.max_tokens": 1000,
    "memory.prompt_max_tokens": 1300,
    "memory.max_operations_per_turn": 3,
    "memory.max_completion_tokens": 1200,
    "memory.reasoning_effort": "low",
    "memory.timeout_seconds": 20,
    "memory.lease_seconds": 60,
    "memory.max_attempts": 3,
    "memory.retry_window_hours": 24,
}


def _seed_memory(ops: OpsHarness) -> None:
    for key, value in MEMORY.items():
        ops.database.seed(key, value)


def test_an_effort_area_is_validated_against_the_chat_small_row(ops: OpsHarness) -> None:
    _seed_memory(ops)
    outcome = _set(ops, "memory.reasoning_effort", '"none"')
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert _stored(ops, "memory.reasoning_effort") == "none"


def test_an_effort_the_chat_small_row_lacks_exits_2(
    ops: OpsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:

    rows = dict(MODEL_CAPABILITIES)
    row = rows["gpt-5.6-terra"]
    rows["gpt-5.6-terra"] = replace(row, reasoning_efforts=row.reasoning_efforts - {"none"})
    monkeypatch.setattr(chat, "MODEL_CAPABILITIES", rows)
    _seed_memory(ops)
    outcome = _set(ops, "memory.reasoning_effort", '"none"')
    assert outcome.code == ExitCode.USAGE
    assert "memory.reasoning_effort" in outcome.failure
    assert ops.database.audit_rows == []


def test_an_effort_area_needs_the_chat_small_model_of_the_checkout(ops: OpsHarness) -> None:
    _seed_memory(ops)
    ops.write_checkout_json("infra/env/dev.json", {"groups": {}})
    outcome = _set(ops, "memory.max_tokens", "1200")
    assert outcome.code == ExitCode.USAGE
    assert "openAi.deployments.chatSmall.model" in outcome.failure
    assert ops.database.audit_rows == []


def test_t541_a_concurrent_change_exits_6_with_nothing_written(ops: OpsHarness) -> None:
    _seed(ops)
    ops.database.lose_race = True
    outcome = _set(ops, API_KEY, "60")
    assert outcome.code == ExitCode.CONFLICT
    assert "changed by someone else" in outcome.failure
    assert ops.database.audit_rows == []


def test_set_of_a_key_without_a_row_exits_4(ops: OpsHarness) -> None:
    outcome = _set(ops, API_KEY, "60")
    assert outcome.code == ExitCode.NOT_FOUND


def test_a_failed_transaction_maps_to_its_exit_code(ops: OpsHarness) -> None:
    _seed(ops)
    ops.database.unit_errors["ops_config_change"] = SqlUnavailable("connection", 10054)
    assert _set(ops, API_KEY, "60").code == ExitCode.UNREACHABLE


def test_no_sign_in_exits_3_before_any_read(ops: OpsHarness) -> None:
    _seed(ops)
    ops.database.token_error = SqlUnavailable("token")
    outcome = _set(ops, API_KEY, "60")
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.database.units == []


def test_load_deploy_identity_sets_values_in_load(ops: OpsHarness) -> None:
    _seed(ops)
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    outcome = ops.run(
        "--env", "load", "config", "set", "--key", API_KEY, "--value", "1200", *TICKET, "--yes"
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert _stored(ops, API_KEY) == 1200


def test_prod_needs_the_typed_environment_name(ops: OpsHarness) -> None:
    _seed(ops)
    ops.answer("prod\n", terminal=True)
    outcome = ops.run(
        "--env", "prod", "config", "set", "--key", API_KEY, "--value", "90", *TICKET, "--yes"
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "Type the environment name to continue: " in outcome.stderr
    assert _stored(ops, API_KEY) == 90


def test_reset_restores_the_default(ops: OpsHarness) -> None:
    ops.database.seed(API_KEY, 60, default=120)
    outcome = ops.run(
        "--env", "dev", "--json", "config", "reset", "--key", API_KEY, *TICKET, "--yes"
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert _stored(ops, API_KEY) == 120
    (audit,) = ops.database.audit_rows
    assert audit["action"] == "ops_config.reset"
    assert json.loads(audit["metadata_json"]) == {
        "area": "api",
        "previous": 60,
        "ticket": "CHG-2026-77",
    }
    assert outcome.json()["result"] == "reset"


def _rates(count: int, price: str) -> dict[str, dict[str, str]]:
    rate = {
        "inputPerMillion": price,
        "cachedInputPerMillion": price,
        "cacheWritePerMillion": price,
        "outputPerMillion": price,
    }
    return {f"model-{index:02d}-{'x' * 40}": dict(rate) for index in range(count)}


def test_large_values_are_audited_as_hashes(ops: OpsHarness) -> None:
    ops.database.seed("pricing.currency", "USD")
    ops.database.seed("pricing.model_rates", _rates(20, "1.250000"))
    outcome = _set(ops, "pricing.model_rates", json.dumps(_rates(20, "2.500000")))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    metadata = json.loads(ops.database.audit_rows[0]["metadata_json"])
    assert set(metadata) == {"area", "previousSha256", "newSha256", "ticket"}
    assert len(ops.database.audit_rows[0]["metadata_json"]) <= 4000
