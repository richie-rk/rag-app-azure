"""Endpoints from `.azure/<env>/.env` and the checkout (`86-operations.md` section 2.2 item 5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from docchat_ops.framework.endpoints import (
    REQUIRED_VALUES,
    AzdEnvironment,
    SqlTarget,
    load_azd_environment,
    object_id,
    parse_dotenv,
    read_checkout_json,
    repository_root,
)
from docchat_ops.framework.exitcodes import UsageError

if TYPE_CHECKING:
    from pathlib import Path

GOOD_SERVER = "sql-docchat-dev-abc123.database.windows.net"


def test_parse_dotenv_reads_the_forms_azd_and_people_write() -> None:
    text = "\n".join(
        [
            "# comment",
            "",
            'AZURE_ENV_NAME="dev"',
            'QUOTED="a \\"b\\" \\\\ \\n end" trailing',
            "SINGLE='x # y'",
            "UNQUOTED=value # comment",
            "HASH=abc#def",
            "export EXPORTED=1",
            "EMPTY=",
            "not a line",
            'UNKNOWN_ESCAPE="\\q"',
            "OPEN_SINGLE='abc",
        ]
    )
    assert parse_dotenv(text) == {
        "AZURE_ENV_NAME": "dev",
        "QUOTED": 'a "b" \\ \n end',
        "SINGLE": "x # y",
        "UNQUOTED": "value",
        "HASH": "abc#def",
        "EXPORTED": "1",
        "EMPTY": "",
        "UNKNOWN_ESCAPE": "\\q",
        "OPEN_SINGLE": "abc",
    }


def test_repository_root_is_the_nearest_azd_project(tmp_path: Path) -> None:
    (tmp_path / "azure.yaml").write_text("name: x\n", encoding="utf-8")
    nested = tmp_path / "scripts" / "ops"
    nested.mkdir(parents=True)
    assert repository_root(nested) == tmp_path


def test_repository_root_outside_a_checkout_exits_2(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        repository_root(tmp_path)


def test_missing_azd_file_names_the_refresh(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as raised:
        load_azd_environment(tmp_path, "prod")
    assert raised.value.next_step == "run azd env refresh -e prod"


def test_azd_file_is_read(tmp_path: Path) -> None:
    (tmp_path / ".azure" / "load").mkdir(parents=True)
    (tmp_path / ".azure" / "load" / ".env").write_text('A="1"\n', encoding="utf-8")
    assert load_azd_environment(tmp_path, "load") == AzdEnvironment("load", {"A": "1"})


def test_require_refuses_missing_and_empty_values() -> None:
    environment = AzdEnvironment("dev", {"A": "1", "EMPTY": ""})
    assert environment.require("A") == "1"
    environment.require_all(("A",))
    for name in ("EMPTY", "MISSING"):
        with pytest.raises(UsageError) as raised:
            environment.require(name)
        assert raised.value.cause == f"{name} is missing from .azure/dev/.env"
        assert raised.value.next_step == "run azd env refresh -e dev"


def test_require_uuid_and_flag() -> None:
    environment = AzdEnvironment(
        "dev",
        {"ID": "AAAAAAAA-0000-4000-8000-00000000000A", "BAD": "x", "ON": "True", "OFF": "false"},
    )
    assert environment.require_uuid("ID") == "aaaaaaaa-0000-4000-8000-00000000000a"
    assert environment.require_flag("ON") is True
    assert environment.require_flag("OFF") is False
    with pytest.raises(UsageError):
        environment.require_uuid("BAD")
    with pytest.raises(UsageError):
        environment.require_flag("BAD")


def test_sql_target() -> None:
    environment = AzdEnvironment(
        "dev", {"DOCCHAT_SQL_SERVER": GOOD_SERVER, "DOCCHAT_SQL_DATABASE": "docchat"}
    )
    assert environment.sql_target() == SqlTarget(GOOD_SERVER, "docchat")


@pytest.mark.parametrize(
    ("server", "database"),
    [("evil.example.com", "docchat"), (GOOD_SERVER, "master"), ("tcp:x,1433", "docchat")],
)
def test_sql_target_refuses_other_databases(server: str, database: str) -> None:
    environment = AzdEnvironment(
        "dev", {"DOCCHAT_SQL_SERVER": server, "DOCCHAT_SQL_DATABASE": database}
    )
    with pytest.raises(UsageError):
        environment.sql_target()


def test_required_values_follow_the_spec_table() -> None:
    sql = ("DOCCHAT_SQL_SERVER", "DOCCHAT_SQL_DATABASE")
    assert REQUIRED_VALUES["config"] == sql
    assert REQUIRED_VALUES["override"] == sql
    assert REQUIRED_VALUES["jit"] == (
        "DOCCHAT_COSMOS_GROUP_DATA_ACCESS",
        "DOCCHAT_COSMOS_ACCOUNT_NAME",
        "AZURE_RESOURCE_GROUP",
    )
    assert set(REQUIRED_VALUES["testusers rotate"]) == {
        "TEST_CLIENT_ID",
        "API_SCOPE",
        "AZURE_TENANT_ID",
        *sql,
    }
    assert REQUIRED_VALUES["testusers check"] == ()
    assert REQUIRED_VALUES["trace"] == ("DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID",)
    assert set(REQUIRED_VALUES["user"]) == {
        *sql,
        "DOCCHAT_COSMOS_ENDPOINT",
        "DOCCHAT_COSMOS_DATABASE",
    }
    ledger = {*sql, "DOCCHAT_SEARCH_MAX_INDEXES", "DOCCHAT_OPENAI_EMBEDDING_MODELS"}
    assert set(REQUIRED_VALUES["reindex"]) == ledger
    assert set(REQUIRED_VALUES["ingestion"]) == ledger
    assert set(REQUIRED_VALUES["ingestion drain-poison"]) == {
        *ledger,
        "DOCCHAT_STORAGE_QUEUE_ENDPOINT",
    }
    assert set(REQUIRED_VALUES["document"]) == {*sql, "DOCCHAT_STORAGE_BLOB_ENDPOINT"}


def test_read_checkout_json(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"x": 1}', encoding="utf-8")
    (tmp_path / "b.json").write_text("{", encoding="utf-8")
    assert read_checkout_json(tmp_path, "a.json") == {"x": 1}
    with pytest.raises(UsageError, match="isn't valid JSON"):
        read_checkout_json(tmp_path, "b.json")
    with pytest.raises(UsageError, match="is missing"):
        read_checkout_json(tmp_path, "c.json")


def test_object_id() -> None:
    assert object_id("AAAAAAAA-0000-4000-8000-00000000000A", "g") == (
        "aaaaaaaa-0000-4000-8000-00000000000a"
    )
    with pytest.raises(UsageError, match="isn't set yet"):
        object_id("00000000-0000-0000-0000-000000000000", "g")
    for value in ("x", None, 3):
        with pytest.raises(UsageError, match="isn't an object ID"):
            object_id(value, "g")
