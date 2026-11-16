"""Unit tests of `SqlSettings` (`81-config.md` section 2.3)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import SecretStr, ValidationError

from docchat_core.settings import LocalSettings, SettingsValidationError, load_settings
from docchat_core.sql.settings import SqlSettings, check_sql_server

SERVER = "sql-docchat-dev-abc123.database.windows.net"

type SpecTable = Callable[[str, str], list[list[str]]]

RANGES = {
    "DOCCHAT_SQL_THREADS": (2, 32),
    "DOCCHAT_SQL_POOL_TIMEOUT_SECONDS": (1, 60),
    "DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS": (5, 60),
    "DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS": (5, 300),
    "DOCCHAT_SQL_IDLE_RELEASE_MINUTES": (1, 60),
}


def test_defaults(settings_env: pytest.MonkeyPatch) -> None:
    del settings_env
    settings = SqlSettings()
    assert settings.sql_server is None
    assert settings.sql_database == "docchat"
    assert settings.sql_threads == 8
    assert settings.sql_pool_timeout_seconds == 10
    assert settings.sql_connect_timeout_seconds == 30
    assert settings.sql_command_timeout_seconds == 30
    assert settings.sql_idle_release_minutes == 10


def test_reads_the_seven_settings(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_SQL_SERVER", SERVER)
    settings_env.setenv("DOCCHAT_SQL_DATABASE", "docchat")
    settings_env.setenv("DOCCHAT_SQL_THREADS", "4")
    settings_env.setenv("DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS", "120")
    settings = load_settings(SqlSettings)
    assert settings.sql_server == SERVER
    assert settings.sql_threads == 4
    assert settings.sql_command_timeout_seconds == 120
    assert set(SqlSettings.setting_names().values()) == {
        "DOCCHAT_SQL_SERVER",
        "DOCCHAT_SQL_DATABASE",
        *RANGES,
    }


@pytest.mark.parametrize(("name", "bounds"), sorted(RANGES.items()))
def test_ranges(settings_env: pytest.MonkeyPatch, name: str, bounds: tuple[int, int]) -> None:
    low, high = bounds
    for value in (low, high):
        settings_env.setenv(name, str(value))
        load_settings(SqlSettings)
    for value in (low - 1, high + 1):
        settings_env.setenv(name, str(value))
        with pytest.raises(SettingsValidationError) as raised:
            load_settings(SqlSettings)
        assert raised.value.settings == (name,)


def test_ranges_equal_the_catalogue(spec_table: SpecTable) -> None:
    rows = spec_table("81-config.md", "### 2.3 Settings on both the API and the ingestion app")
    allowed = {row[0]: row[4] for row in rows if row[0].startswith("DOCCHAT_SQL_")}
    assert set(allowed) == {"DOCCHAT_SQL_SERVER", "DOCCHAT_SQL_DATABASE", *RANGES}
    for name, (low, high) in RANGES.items():
        assert allowed[name] == f"{low} to {high}"


@pytest.mark.parametrize(
    "server",
    [
        "127.0.0.1",
        "localhost",
        "evil.example.com",
        "UPPER.database.windows.net",
        "-a.database.windows.net",
    ],
)
def test_server_is_an_azure_sql_name(settings_env: pytest.MonkeyPatch, server: str) -> None:
    settings_env.setenv("DOCCHAT_SQL_SERVER", server)
    with pytest.raises(SettingsValidationError):
        load_settings(SqlSettings)


def test_database_is_docchat() -> None:
    with pytest.raises(ValidationError):
        SqlSettings(sql_database="master")  # type: ignore[arg-type]


def test_server_required_outside_emulator_mode() -> None:
    with pytest.raises(SettingsValidationError) as raised:
        check_sql_server(SqlSettings(), LocalSettings(local_emulators=False))
    assert raised.value.settings == ("DOCCHAT_SQL_SERVER",)
    check_sql_server(SqlSettings(sql_server=SERVER), LocalSettings(local_emulators=False))


def test_emulator_mode_needs_the_password() -> None:
    with pytest.raises(SettingsValidationError) as raised:
        check_sql_server(SqlSettings(), LocalSettings(local_emulators=True))
    assert raised.value.settings == ("DOCCHAT_LOCAL_SQL_SA_PASSWORD",)
    password = SecretStr("Aa1!aaaaaaaaaaaaaaaaaaaa")
    check_sql_server(
        SqlSettings(), LocalSettings(local_emulators=True, local_sql_sa_password=password)
    )
