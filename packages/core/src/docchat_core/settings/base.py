"""The `DocChatSettings` base and the value types several settings classes share (`81-config.md`
sections 1.2, 2.1 and 6.1; ADR 0008).

Each process reads its app settings once at start through classes derived from `DocChatSettings`,
so a value can't change while the process runs, and an invalid or missing value stops it before
it serves. Every class keeps the prefix `DOCCHAT_` and names its fields with their area
(`core_environment` reads `DOCCHAT_CORE_ENVIRONMENT`), because several classes read more than one
area; a platform-named setting is a field with its own name as `validation_alias`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final

from pydantic import BeforeValidator, StringConstraints
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

SETTINGS_PREFIX: Final = "DOCCHAT_"
BUILD_COMMIT_PATTERN: Final = r"^[0-9a-f]{40}$"


class DocChatSettings(BaseSettings):
    """Base of every app-settings class: prefix `DOCCHAT_`, `extra="forbid"`, frozen."""

    model_config = SettingsConfigDict(
        env_prefix=SETTINGS_PREFIX,
        extra="forbid",
        frozen=True,
        # `.env.example` lists names with empty values; empty means "not set", so a declared
        # default applies and a required setting reports as missing rather than as malformed.
        env_ignore_empty=True,
        # A failure names the setting and never its value (81-config.md section 6.1).
        hide_input_in_errors=True,
    )

    @classmethod
    def setting_names(cls) -> dict[str, str]:
        """Each field's app-setting name, keyed by field name."""
        prefix = cls.model_config.get("env_prefix") or ""
        names: dict[str, str] = {}
        for field_name, field in cls.model_fields.items():
            alias = field.validation_alias
            names[field_name] = alias if isinstance(alias, str) else f"{prefix}{field_name}".upper()
        return names


class SettingRuleError(ValueError):
    """A rule across fields of one settings class is broken.

    Raised from a class's own validator; `settings` are the app-setting names the start-up
    record names. The message is constant text, never a value.
    """

    def __init__(self, settings: Sequence[str], message: str = "a settings rule is broken") -> None:
        super().__init__(message)
        self.settings = tuple(settings)


def _bool_setting(value: object) -> object:
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise PydanticCustomError("bool_parsing", "Input should be true or false")


BoolSetting = Annotated[bool, BeforeValidator(_bool_setting)]
"""A boolean app setting: exactly `true` or `false`, the two values the catalogue allows."""

BuildCommit = Annotated[str, StringConstraints(pattern=BUILD_COMMIT_PATTERN)]
"""`DOCCHAT_BUILD_COMMIT`: the 40 lower-case hexadecimal characters of the image's commit."""
