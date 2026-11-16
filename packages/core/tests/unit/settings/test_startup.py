from __future__ import annotations

import io
import json
from typing import Annotated, Self

import pytest
from pydantic import Field, ValidationError, model_validator

from docchat_core.settings.base import DocChatSettings, SettingRuleError
from docchat_core.settings.core import CoreSettings, EnvironmentName
from docchat_core.settings.endpoints import EndpointUrl
from docchat_core.settings.local import LocalSettings
from docchat_core.settings.startup import (
    SETTINGS_VALIDATION_RECORD,
    STARTUP_REFUSAL_EXIT_CODE,
    STARTUP_REFUSALS,
    SettingsValidationError,
    StartupCheckError,
    StartupRefusalError,
    _failing_settings,
    check_endpoints,
    check_identity_client_id,
    check_local_emulators,
    check_local_environment,
    exit_on_startup_failure,
    load_settings,
)

pytestmark = pytest.mark.usefixtures("settings_env")

_CLIENT_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_PRINCIPAL_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_AZURE = {"WEBSITE_SITE_NAME": "app-docchat-api-dev"}


class _AreaSettings(DocChatSettings):
    """An area's class with endpoint settings, as the SQL and API classes will have."""

    sql_server: str = "sql-docchat-dev-abc.database.windows.net"
    search_endpoint: EndpointUrl = "https://srch-docchat-dev-abc.search.windows.net"
    openai_endpoint: EndpointUrl | None = None
    search_timeout_seconds: Annotated[int, Field(ge=2, le=30)] = 10
    search_pair_low: int = 1
    search_pair_high: int = 2

    @model_validator(mode="after")
    def _pair_in_order(self) -> Self:
        if self.search_pair_low > self.search_pair_high:
            raise SettingRuleError(("DOCCHAT_SEARCH_PAIR_LOW", "DOCCHAT_SEARCH_PAIR_HIGH"))
        return self


class _ClassRule(DocChatSettings):
    rule_value: int = 1

    @model_validator(mode="after")
    def _plain_rule(self) -> Self:
        if self.rule_value == 0:
            raise ValueError("a rule without names")
        return self


def _core(environment: EnvironmentName, client_id: str | None = _CLIENT_ID) -> CoreSettings:
    values: dict[str, object] = {
        "core_environment": environment,
        "core_workload_principal_id": _PRINCIPAL_ID,
    }
    if client_id is not None:
        values["core_identity_client_id"] = client_id
    return CoreSettings.model_validate(values)


def _local(*, emulators: bool) -> LocalSettings:
    return LocalSettings(local_emulators=emulators)


def test_the_two_shared_refusals() -> None:
    assert dict(STARTUP_REFUSALS) == {
        "local_emulators_in_azure": "Local emulator mode can't run in Azure.",
        "insecure_endpoint": "A configured endpoint isn't a secure remote address.",
    }
    assert STARTUP_REFUSAL_EXIT_CODE == 78


def test_load_settings_returns_the_settings(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_SEARCH_TIMEOUT_SECONDS", "30")

    assert load_settings(_AreaSettings).search_timeout_seconds == 30


def test_load_settings_names_each_failing_setting_and_no_value(
    settings_env: pytest.MonkeyPatch,
) -> None:
    settings_env.setenv("DOCCHAT_SEARCH_TIMEOUT_SECONDS", "31")
    settings_env.setenv("DOCCHAT_SEARCH_ENDPOINT", "srch-secret-host")

    with pytest.raises(SettingsValidationError) as caught:
        load_settings(_AreaSettings)

    failure = caught.value
    assert failure.code is None
    assert failure.settings == ("DOCCHAT_SEARCH_ENDPOINT", "DOCCHAT_SEARCH_TIMEOUT_SECONDS")
    assert failure.__cause__ is None
    assert failure.__suppress_context__ is True
    assert "31" not in failure.record()
    assert "srch-secret-host" not in failure.record()


def test_load_settings_names_a_missing_required_setting(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_CORE_ENVIRONMENT", "dev")

    with pytest.raises(SettingsValidationError) as caught:
        load_settings(CoreSettings)

    assert caught.value.settings == ("DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID",)


def test_load_settings_names_the_settings_of_a_broken_rule(
    settings_env: pytest.MonkeyPatch,
) -> None:
    settings_env.setenv("DOCCHAT_SEARCH_PAIR_LOW", "3")

    with pytest.raises(SettingsValidationError) as caught:
        load_settings(_AreaSettings)

    assert caught.value.settings == ("DOCCHAT_SEARCH_PAIR_LOW", "DOCCHAT_SEARCH_PAIR_HIGH")


def test_load_settings_names_the_class_for_a_rule_without_names(
    settings_env: pytest.MonkeyPatch,
) -> None:
    settings_env.setenv("DOCCHAT_RULE_VALUE", "0")

    with pytest.raises(SettingsValidationError) as caught:
        load_settings(_ClassRule)

    assert caught.value.settings == ("_ClassRule",)


def test_failing_settings_maps_aliases_and_unknown_keys() -> None:
    error = ValidationError.from_exception_data(
        "_AreaSettings",
        [
            {"type": "missing", "loc": ("search_endpoint",), "input": None},
            {"type": "missing", "loc": ("APPLICATIONINSIGHTS_CONNECTION_STRING",), "input": None},
            {"type": "missing", "loc": ("DOCCHAT_SQL_SERVER",), "input": None},
            {"type": "extra_forbidden", "loc": ("search_other",), "input": None},
        ],
    )

    assert _failing_settings(_AreaSettings, error) == [
        "DOCCHAT_SEARCH_ENDPOINT",
        "DOCCHAT_APPLICATIONINSIGHTS_CONNECTION_STRING",
        "DOCCHAT_SQL_SERVER",
        "DOCCHAT_SEARCH_OTHER",
    ]


def test_check_local_emulators_refuses_the_mode_inside_azure() -> None:
    with pytest.raises(StartupRefusalError) as caught:
        check_local_emulators(_local(emulators=True), _AZURE)

    assert caught.value.code == "local_emulators_in_azure"
    assert caught.value.settings == ("DOCCHAT_LOCAL_EMULATORS",)
    assert caught.value.message == "Local emulator mode can't run in Azure."


@pytest.mark.parametrize(
    ("emulators", "environ"),
    [(True, {}), (False, _AZURE), (False, {})],
)
def test_check_local_emulators_passes_otherwise(emulators: bool, environ: dict[str, str]) -> None:
    check_local_emulators(_local(emulators=emulators), environ)


def test_check_endpoints_refuses_http_and_loopback_outside_emulator_mode() -> None:
    area = _AreaSettings(
        sql_server="127.0.0.1",
        search_endpoint="http://127.0.0.1:9",
        openai_endpoint="https://aoai.openai.azure.com",
    )

    with pytest.raises(StartupRefusalError) as caught:
        check_endpoints(_local(emulators=False), area)

    assert caught.value.code == "insecure_endpoint"
    assert caught.value.settings == ("DOCCHAT_SQL_SERVER", "DOCCHAT_SEARCH_ENDPOINT")


def test_check_endpoints_names_each_setting_once_across_classes() -> None:
    area = _AreaSettings(openai_endpoint="http://aoai.example")

    with pytest.raises(StartupRefusalError) as caught:
        check_endpoints(_local(emulators=False), area, area)

    assert caught.value.settings == ("DOCCHAT_OPENAI_ENDPOINT",)


def test_check_endpoints_passes_secure_endpoints_and_unset_ones() -> None:
    check_endpoints(_local(emulators=False), _AreaSettings(), _core(EnvironmentName.DEV))


def test_check_endpoints_allows_anything_in_emulator_mode() -> None:
    area = _AreaSettings(search_endpoint="http://127.0.0.1:9", sql_server="localhost")

    check_endpoints(_local(emulators=True), area)


def test_check_local_environment_refuses_local_inside_azure() -> None:
    with pytest.raises(SettingsValidationError) as caught:
        check_local_environment(_core(EnvironmentName.LOCAL), {"WEBSITE_INSTANCE_ID": "1"})

    assert caught.value.settings == ("DOCCHAT_CORE_ENVIRONMENT",)


@pytest.mark.parametrize(
    ("environment", "environ"),
    [(EnvironmentName.LOCAL, {}), (EnvironmentName.DEV, _AZURE), (EnvironmentName.PROD, {})],
)
def test_check_local_environment_passes_otherwise(
    environment: EnvironmentName, environ: dict[str, str]
) -> None:
    check_local_environment(_core(environment), environ)


@pytest.mark.parametrize("environment", [EnvironmentName.DEV, EnvironmentName.PROD])
def test_check_identity_client_id_requires_it_outside_local(environment: EnvironmentName) -> None:
    with pytest.raises(SettingsValidationError) as caught:
        check_identity_client_id(_core(environment, client_id=None))

    assert caught.value.settings == ("DOCCHAT_CORE_IDENTITY_CLIENT_ID",)


def test_check_identity_client_id_passes_when_set_or_local() -> None:
    check_identity_client_id(_core(EnvironmentName.PROD))
    check_identity_client_id(_core(EnvironmentName.LOCAL, client_id=None))


def test_the_record_is_one_json_line_naming_code_and_settings() -> None:
    refusal = StartupRefusalError("insecure_endpoint", ("DOCCHAT_SEARCH_ENDPOINT",))
    validation = SettingsValidationError(("DOCCHAT_CORE_ENVIRONMENT",))

    assert json.loads(refusal.record()) == {
        "level": "error",
        "name": "insecure_endpoint",
        "message": "A configured endpoint isn't a secure remote address.",
        "settings": ["DOCCHAT_SEARCH_ENDPOINT"],
    }
    assert json.loads(validation.record())["name"] == SETTINGS_VALIDATION_RECORD
    assert "\n" not in refusal.record()
    assert isinstance(refusal, StartupCheckError)
    assert isinstance(validation, StartupCheckError)


def test_exit_on_startup_failure_writes_one_record_and_exits_78() -> None:
    stream = io.StringIO()

    with pytest.raises(SystemExit) as caught:
        exit_on_startup_failure(
            StartupRefusalError("local_emulators_in_azure", ("DOCCHAT_LOCAL_EMULATORS",)), stream
        )

    assert caught.value.code == STARTUP_REFUSAL_EXIT_CODE
    [line] = stream.getvalue().splitlines()
    assert json.loads(line)["name"] == "local_emulators_in_azure"


def test_exit_on_startup_failure_writes_to_standard_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        exit_on_startup_failure(SettingsValidationError(("DOCCHAT_CORE_ENVIRONMENT",)))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["settings"] == ["DOCCHAT_CORE_ENVIRONMENT"]


def _start(*area_classes: type[DocChatSettings]) -> None:
    """The shared part of a process's start-up, in the order of 81-config.md section 6.1."""
    try:
        core = load_settings(CoreSettings)
        local = load_settings(LocalSettings)
        areas = [load_settings(area_class) for area_class in area_classes]
        check_local_emulators(local)
        check_endpoints(local, core, *areas)
        check_local_environment(core)
        check_identity_client_id(core)
    except StartupCheckError as failure:
        exit_on_startup_failure(failure)


def _developer_environment(env: pytest.MonkeyPatch) -> None:
    env.setenv("DOCCHAT_CORE_ENVIRONMENT", "local")
    env.setenv("DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID", _PRINCIPAL_ID)


def test_emulator_mode_on_an_azure_host_exits_78_naming_the_refusal(
    settings_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _developer_environment(settings_env)
    settings_env.setenv("DOCCHAT_LOCAL_EMULATORS", "true")
    settings_env.setenv("WEBSITE_SITE_NAME", "app-docchat-api-dev")

    with pytest.raises(SystemExit) as caught:
        _start(_AreaSettings)

    assert caught.value.code == 78
    assert json.loads(capsys.readouterr().err)["name"] == "local_emulators_in_azure"


def test_an_http_loopback_endpoint_exits_78_naming_the_refusal(
    settings_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _developer_environment(settings_env)
    settings_env.setenv("DOCCHAT_LOCAL_EMULATORS", "false")
    settings_env.setenv("DOCCHAT_SEARCH_ENDPOINT", "http://127.0.0.1:9")

    with pytest.raises(SystemExit) as caught:
        _start(_AreaSettings)

    assert caught.value.code == 78
    record = json.loads(capsys.readouterr().err)
    assert (record["name"], record["settings"]) == (
        "insecure_endpoint",
        ["DOCCHAT_SEARCH_ENDPOINT"],
    )


def test_a_valid_developer_configuration_starts(settings_env: pytest.MonkeyPatch) -> None:
    _developer_environment(settings_env)
    settings_env.setenv("DOCCHAT_LOCAL_EMULATORS", "true")
    settings_env.setenv("DOCCHAT_SEARCH_ENDPOINT", "http://127.0.0.1:9")

    _start(_AreaSettings)
