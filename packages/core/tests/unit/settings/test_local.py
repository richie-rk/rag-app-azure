from __future__ import annotations

import pytest
from pydantic import ValidationError

from docchat_core.settings.local import LOCAL_SQL_SA_PASSWORD_LENGTH, LocalSettings

_PASSWORD = "Abcdefghijklmnopqrstuv1!"


def test_setting_names() -> None:
    assert LocalSettings.setting_names() == {
        "local_emulators": "DOCCHAT_LOCAL_EMULATORS",
        "local_sql_sa_password": "DOCCHAT_LOCAL_SQL_SA_PASSWORD",
    }


@pytest.mark.usefixtures("settings_env")
def test_defaults() -> None:
    settings = LocalSettings()

    assert settings.local_emulators is False
    assert settings.local_sql_sa_password is None


def test_the_example_file_values_mean_defaults(settings_env: pytest.MonkeyPatch) -> None:
    # `.env.example` holds both names with empty values.
    settings_env.setenv("DOCCHAT_LOCAL_EMULATORS", "")
    settings_env.setenv("DOCCHAT_LOCAL_SQL_SA_PASSWORD", "")

    settings = LocalSettings()

    assert (settings.local_emulators, settings.local_sql_sa_password) == (False, None)


def test_reads_the_flag_and_the_password(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_LOCAL_EMULATORS", "true")
    settings_env.setenv("DOCCHAT_LOCAL_SQL_SA_PASSWORD", _PASSWORD)

    settings = LocalSettings()

    assert settings.local_emulators is True
    assert settings.local_sql_sa_password is not None
    assert settings.local_sql_sa_password.get_secret_value() == _PASSWORD
    assert _PASSWORD not in repr(settings)


def test_the_flag_is_true_or_false(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_LOCAL_EMULATORS", "1")

    with pytest.raises(ValidationError):
        LocalSettings()


@pytest.mark.parametrize(
    "password",
    [
        _PASSWORD[:-1],
        _PASSWORD + "x",
        "abcdefghijklmnopqrstuv1!",
        "ABCDEFGHIJKLMNOPQRSTUV1!",
        "Abcdefghijklmnopqrstuvw!",
        "Abcdefghijklmnopqrstuvw1",
        "Abcdefghijklmnopqrstuv1 ",
    ],
)
def test_the_password_meets_the_complexity_rules(
    settings_env: pytest.MonkeyPatch, password: str
) -> None:
    settings_env.setenv("DOCCHAT_LOCAL_SQL_SA_PASSWORD", password)

    with pytest.raises(ValidationError) as caught:
        LocalSettings()

    assert password not in str(caught.value)
    assert caught.value.errors()[0]["loc"][0] == "local_sql_sa_password"


def test_the_password_length() -> None:
    assert len(_PASSWORD) == LOCAL_SQL_SA_PASSWORD_LENGTH == 24
