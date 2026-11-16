"""The settings framework: the `DocChatSettings` base, start-up validation and the local-emulator
guard, and the settings classes core owns (`81-config.md` sections 2 and 6; ADR 0008). Each other
area's settings classes sit with that area."""

from docchat_core.settings.base import (
    BUILD_COMMIT_PATTERN,
    SETTINGS_PREFIX,
    BoolSetting,
    BuildCommit,
    DocChatSettings,
    SettingRuleError,
)
from docchat_core.settings.core import CoreSettings, EnvironmentName
from docchat_core.settings.endpoints import (
    ENDPOINT_SETTINGS,
    EndpointUrl,
    is_insecure_endpoint,
    is_loopback_host,
)
from docchat_core.settings.hosting import AZURE_HOST_VARIABLES, running_in_azure
from docchat_core.settings.local import LOCAL_SQL_SA_PASSWORD_LENGTH, LocalSettings, SqlSaPassword
from docchat_core.settings.startup import (
    SETTINGS_VALIDATION_RECORD,
    STARTUP_REFUSAL_EXIT_CODE,
    STARTUP_REFUSALS,
    SettingsValidationError,
    StartupCheckError,
    StartupRefusalError,
    check_endpoints,
    check_identity_client_id,
    check_local_emulators,
    check_local_environment,
    exit_on_startup_failure,
    load_settings,
)

__all__ = [
    "AZURE_HOST_VARIABLES",
    "BUILD_COMMIT_PATTERN",
    "ENDPOINT_SETTINGS",
    "LOCAL_SQL_SA_PASSWORD_LENGTH",
    "SETTINGS_PREFIX",
    "SETTINGS_VALIDATION_RECORD",
    "STARTUP_REFUSALS",
    "STARTUP_REFUSAL_EXIT_CODE",
    "BoolSetting",
    "BuildCommit",
    "CoreSettings",
    "DocChatSettings",
    "EndpointUrl",
    "EnvironmentName",
    "LocalSettings",
    "SettingRuleError",
    "SettingsValidationError",
    "SqlSaPassword",
    "StartupCheckError",
    "StartupRefusalError",
    "check_endpoints",
    "check_identity_client_id",
    "check_local_emulators",
    "check_local_environment",
    "exit_on_startup_failure",
    "is_insecure_endpoint",
    "is_loopback_host",
    "load_settings",
    "running_in_azure",
]
