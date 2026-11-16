from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest

from docchat_core.auth.settings import (
    TEST_CLIENT_ENVIRONMENTS,
    AuthSettings,
    allowed_clients,
    check_test_client,
    issuer_for,
)
from docchat_core.settings import (
    CoreSettings,
    EnvironmentName,
    SettingsValidationError,
    load_settings,
)

type SpecTable = Callable[[str, str], list[list[str]]]

TENANT = "5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e"
API = "8e3f1a52-4c6d-4b7e-a9f0-2c1d3e4f5a6b"
WEB = "1d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6"
TEST = "9a8b7c6d-5e4f-4a3b-b2c1-d0e9f8a7b6c5"
PRINCIPAL = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _set(env: pytest.MonkeyPatch, **values: str) -> None:
    defaults = {
        "DOCCHAT_AUTH_TENANT_ID": TENANT,
        "DOCCHAT_AUTH_API_CLIENT_ID": API,
        "DOCCHAT_AUTH_WEB_CLIENT_ID": WEB,
        "DOCCHAT_AUTH_TEST_CLIENT_ID": TEST,
    }
    for name, value in (defaults | values).items():
        env.setenv(name, value)


def _settings(test_client: str | None = TEST) -> AuthSettings:
    return AuthSettings(
        auth_tenant_id=UUID(TENANT),
        auth_api_client_id=UUID(API),
        auth_web_client_id=UUID(WEB),
        auth_test_client_id=None if test_client is None else UUID(test_client),
    )


def _core(environment: EnvironmentName) -> CoreSettings:
    return CoreSettings.model_construct(
        core_environment=environment, core_workload_principal_id=UUID(PRINCIPAL)
    )


def test_setting_names() -> None:
    assert set(AuthSettings.setting_names().values()) == {
        "DOCCHAT_AUTH_TENANT_ID",
        "DOCCHAT_AUTH_API_CLIENT_ID",
        "DOCCHAT_AUTH_WEB_CLIENT_ID",
        "DOCCHAT_AUTH_TEST_CLIENT_ID",
    }


def test_the_names_are_the_catalogue_rows(spec_table: SpecTable) -> None:
    rows = spec_table("81-config.md", "### 2.4 Settings on the API only")
    catalogued = {row[0] for row in rows if row[0].startswith("DOCCHAT_AUTH_")}
    assert catalogued == set(AuthSettings.setting_names().values())


def test_reads_the_four_settings(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env)

    settings = load_settings(AuthSettings)

    assert settings.auth_tenant_id == UUID(TENANT)
    assert settings.auth_api_client_id == UUID(API)
    assert settings.auth_web_client_id == UUID(WEB)
    assert settings.auth_test_client_id == UUID(TEST)
    assert settings.issuer == f"https://login.microsoftonline.com/{TENANT}/v2.0"


def test_an_empty_test_client_is_absent(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env, DOCCHAT_AUTH_TEST_CLIENT_ID="")

    assert load_settings(AuthSettings).auth_test_client_id is None


def test_the_client_ids_may_be_upper_case(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env, DOCCHAT_AUTH_WEB_CLIENT_ID=WEB.upper())

    assert str(load_settings(AuthSettings).auth_web_client_id) == WEB


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCCHAT_AUTH_TENANT_ID", TENANT.upper()),
        ("DOCCHAT_AUTH_TENANT_ID", ""),
        ("DOCCHAT_AUTH_API_CLIENT_ID", "{" + API + "}"),
        ("DOCCHAT_AUTH_WEB_CLIENT_ID", "not-a-uuid"),
        ("DOCCHAT_AUTH_TEST_CLIENT_ID", TEST.replace("-", "")),
    ],
)
def test_refuses_a_missing_or_malformed_id(
    settings_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _set(settings_env, **{name: value})

    with pytest.raises(SettingsValidationError) as caught:
        load_settings(AuthSettings)

    assert caught.value.settings == (name,)


@pytest.mark.parametrize(
    ("values", "named"),
    [
        (
            {"DOCCHAT_AUTH_WEB_CLIENT_ID": API},
            {"DOCCHAT_AUTH_API_CLIENT_ID", "DOCCHAT_AUTH_WEB_CLIENT_ID"},
        ),
        (
            {"DOCCHAT_AUTH_TEST_CLIENT_ID": WEB},
            {"DOCCHAT_AUTH_WEB_CLIENT_ID", "DOCCHAT_AUTH_TEST_CLIENT_ID"},
        ),
        (
            {"DOCCHAT_AUTH_TEST_CLIENT_ID": API.upper()},
            {"DOCCHAT_AUTH_API_CLIENT_ID", "DOCCHAT_AUTH_TEST_CLIENT_ID"},
        ),
    ],
)
def test_start_up_refuses_equal_client_ids(
    settings_env: pytest.MonkeyPatch, values: dict[str, str], named: set[str]
) -> None:
    # Check 6 of 81-config.md section 6.1: the failure names the settings, never the values.
    _set(settings_env, **values)

    with pytest.raises(SettingsValidationError) as caught:
        load_settings(AuthSettings)

    assert set(caught.value.settings) == named
    assert API not in caught.value.record()


def test_distinct_ids_pass_without_a_test_client() -> None:
    assert _settings(test_client=None).auth_test_client_id is None


def test_t005_start_up_refuses_prod_with_a_test_client() -> None:
    with pytest.raises(SettingsValidationError) as caught:
        check_test_client(_core(EnvironmentName.PROD), _settings())

    assert caught.value.settings == ("DOCCHAT_AUTH_TEST_CLIENT_ID",)


def test_prod_without_a_test_client_starts() -> None:
    check_test_client(_core(EnvironmentName.PROD), _settings(test_client=None))


@pytest.mark.parametrize("environment", [EnvironmentName.DEV, EnvironmentName.LOAD])
def test_non_production_environments_may_name_a_test_client(environment: EnvironmentName) -> None:
    check_test_client(_core(environment), _settings())


@pytest.mark.parametrize("environment", list(EnvironmentName))
def test_t005_the_test_client_is_allowed_only_in_dev_load_and_local(
    environment: EnvironmentName,
) -> None:
    clients = allowed_clients(_settings(), environment)

    if environment in {EnvironmentName.DEV, EnvironmentName.LOAD, EnvironmentName.LOCAL}:
        assert dict(clients) == {UUID(WEB): "web", UUID(TEST): "test"}
    else:
        assert dict(clients) == {UUID(WEB): "web"}


@pytest.mark.parametrize("environment", list(EnvironmentName))
def test_t005_an_empty_test_client_is_never_allowed(environment: EnvironmentName) -> None:
    assert dict(allowed_clients(_settings(test_client=None), environment)) == {UUID(WEB): "web"}


def test_the_test_client_environments() -> None:
    assert {env.value for env in TEST_CLIENT_ENVIRONMENTS} == {"dev", "load", "local"}


def test_the_issuer_is_built_from_the_tenant_alone() -> None:
    assert issuer_for(UUID(TENANT)) == f"https://login.microsoftonline.com/{TENANT}/v2.0"
