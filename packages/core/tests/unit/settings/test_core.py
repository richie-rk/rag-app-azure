from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from docchat_core.settings.core import CoreSettings, EnvironmentName

_CLIENT_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_PRINCIPAL_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _set(env: pytest.MonkeyPatch, **values: str) -> None:
    defaults = {
        "DOCCHAT_CORE_ENVIRONMENT": "dev",
        "DOCCHAT_CORE_IDENTITY_CLIENT_ID": _CLIENT_ID,
        "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": _PRINCIPAL_ID,
    }
    for name, value in (defaults | values).items():
        env.setenv(name, value)


def test_the_environment_names() -> None:
    assert [name.value for name in EnvironmentName] == ["dev", "prod", "load", "local"]


def test_setting_names() -> None:
    assert set(CoreSettings.setting_names().values()) == {
        "DOCCHAT_CORE_ENVIRONMENT",
        "DOCCHAT_CORE_IDENTITY_CLIENT_ID",
        "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID",
    }


def test_reads_the_three_settings(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env)

    settings = CoreSettings()

    assert settings.core_environment is EnvironmentName.DEV
    assert settings.core_identity_client_id == UUID(_CLIENT_ID)
    assert settings.core_workload_principal_id == UUID(_PRINCIPAL_ID)


def test_the_client_id_may_be_absent(settings_env: pytest.MonkeyPatch) -> None:
    # Start-up, not the class, requires it outside `local` (check 7).
    _set(settings_env, DOCCHAT_CORE_ENVIRONMENT="local", DOCCHAT_CORE_IDENTITY_CLIENT_ID="")

    assert CoreSettings().core_identity_client_id is None


def test_the_client_id_must_be_lower_case(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env, DOCCHAT_CORE_IDENTITY_CLIENT_ID=_CLIENT_ID.upper())

    with pytest.raises(ValidationError) as caught:
        CoreSettings()

    assert {error["loc"][0] for error in caught.value.errors()} == {"core_identity_client_id"}


def test_the_principal_id_is_canonical_in_either_case(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env, DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID=_PRINCIPAL_ID.upper())

    assert str(CoreSettings().core_workload_principal_id) == _PRINCIPAL_ID


@pytest.mark.parametrize(
    "values",
    [
        {"DOCCHAT_CORE_ENVIRONMENT": "Dev"},
        {"DOCCHAT_CORE_ENVIRONMENT": "staging"},
        {"DOCCHAT_CORE_ENVIRONMENT": ""},
        {"DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": ""},
        {"DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": "{" + _PRINCIPAL_ID + "}"},
    ],
)
def test_refuses_a_missing_or_invalid_value(
    settings_env: pytest.MonkeyPatch, values: dict[str, str]
) -> None:
    _set(settings_env, **values)

    with pytest.raises(ValidationError):
        CoreSettings()


def test_core_settings_are_frozen(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env)
    settings = CoreSettings()

    with pytest.raises(ValidationError):
        settings.core_environment = EnvironmentName.PROD  # type: ignore[misc]
