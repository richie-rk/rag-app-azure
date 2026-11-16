from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import Field, ValidationError

from docchat_core.settings.base import (
    SETTINGS_PREFIX,
    BoolSetting,
    BuildCommit,
    DocChatSettings,
    SettingRuleError,
)

_COMMIT = "0123456789abcdef0123456789abcdef01234567"


class _Sample(DocChatSettings):
    sample_count: Annotated[int, Field(ge=1, le=5)] = 3
    sample_flag: BoolSetting = False
    sample_commit: BuildCommit | None = None
    platform_value: str | None = Field(
        default=None, validation_alias="APPLICATIONINSIGHTS_CONNECTION_STRING"
    )


def test_the_base_configuration() -> None:
    config = DocChatSettings.model_config

    assert SETTINGS_PREFIX == "DOCCHAT_"
    assert config["env_prefix"] == "DOCCHAT_"
    assert config["extra"] == "forbid"
    assert config["frozen"] is True


def test_setting_names_use_the_prefix_or_the_platform_name() -> None:
    assert _Sample.setting_names() == {
        "sample_count": "DOCCHAT_SAMPLE_COUNT",
        "sample_flag": "DOCCHAT_SAMPLE_FLAG",
        "sample_commit": "DOCCHAT_SAMPLE_COMMIT",
        "platform_value": "APPLICATIONINSIGHTS_CONNECTION_STRING",
    }


def test_values_come_from_the_environment(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_SAMPLE_COUNT", "5")
    settings_env.setenv("DOCCHAT_SAMPLE_FLAG", "true")
    settings_env.setenv("DOCCHAT_SAMPLE_COMMIT", _COMMIT)
    settings_env.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")

    settings = _Sample()

    assert (settings.sample_count, settings.sample_flag, settings.sample_commit) == (
        5,
        True,
        _COMMIT,
    )
    assert settings.platform_value == "InstrumentationKey=x"


def test_an_empty_value_counts_as_not_set(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_SAMPLE_COUNT", "")
    settings_env.setenv("DOCCHAT_SAMPLE_FLAG", "")

    settings = _Sample()

    assert (settings.sample_count, settings.sample_flag) == (3, False)


@pytest.mark.usefixtures("settings_env")
def test_settings_are_frozen() -> None:
    settings = _Sample()

    with pytest.raises(ValidationError):
        settings.sample_count = 4  # type: ignore[misc]


@pytest.mark.usefixtures("settings_env")
def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        _Sample(sample_other=1)  # type: ignore[call-arg]

    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_a_failure_never_shows_the_value(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_SAMPLE_COUNT", "987654")

    with pytest.raises(ValidationError) as caught:
        _Sample()

    assert "987654" not in str(caught.value)


@pytest.mark.parametrize(("text", "value"), [("true", True), ("false", False)])
def test_bool_setting_reads_true_and_false(
    settings_env: pytest.MonkeyPatch, text: str, value: bool
) -> None:
    settings_env.setenv("DOCCHAT_SAMPLE_FLAG", text)

    assert _Sample().sample_flag is value


@pytest.mark.parametrize("text", ["True", "1", "yes", "on", "false "])
def test_bool_setting_refuses_other_spellings(settings_env: pytest.MonkeyPatch, text: str) -> None:
    settings_env.setenv("DOCCHAT_SAMPLE_FLAG", text)

    with pytest.raises(ValidationError) as caught:
        _Sample()

    assert caught.value.errors()[0]["type"] == "bool_parsing"


@pytest.mark.usefixtures("settings_env")
def test_bool_setting_accepts_a_bool() -> None:
    assert _Sample(sample_flag=True).sample_flag is True


@pytest.mark.parametrize("text", [_COMMIT.upper(), _COMMIT[:-1], _COMMIT + "0", "g" * 40])
def test_build_commit_is_40_lower_case_hex(settings_env: pytest.MonkeyPatch, text: str) -> None:
    settings_env.setenv("DOCCHAT_SAMPLE_COMMIT", text)

    with pytest.raises(ValidationError):
        _Sample()


def test_setting_rule_error_carries_names_and_constant_text() -> None:
    error = SettingRuleError(["DOCCHAT_A", "DOCCHAT_B"], "the client IDs must be distinct")

    assert isinstance(error, ValueError)
    assert error.settings == ("DOCCHAT_A", "DOCCHAT_B")
    assert str(error) == "the client IDs must be distinct"
    assert str(SettingRuleError(["DOCCHAT_A"])) == "a settings rule is broken"
