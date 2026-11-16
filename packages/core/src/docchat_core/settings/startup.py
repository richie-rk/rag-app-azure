"""Start-up validation and refusal (`81-config.md` section 6.1; ADR 0008).

Every process validates its whole configuration before it serves. Each check here raises a
`StartupCheckError` naming the failing settings, never their values; the process decides how it
stops. The API's `gunicorn.conf.py` runs the checks in the order of section 6.1 and calls
`exit_on_startup_failure`, which writes one error record to standard error and exits with code 78
before Gunicorn forks a worker; the ingestion app logs `job_failed` and raises instead
(`14-ingestion.md` section 4.11).

The checks owned here: 1 for any class (`load_settings`), 2 (`check_local_emulators`), 3
(`check_endpoints`), 4 (`check_local_environment`) and 7 (`check_identity_client_id`). The areas
that own the other settings raise `SettingsValidationError` from their own checks, or
`SettingRuleError` from a validator of their class.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, NoReturn, TextIO

from pydantic import ValidationError

from docchat_core.settings.base import SETTINGS_PREFIX, DocChatSettings, SettingRuleError
from docchat_core.settings.core import CoreSettings, EnvironmentName
from docchat_core.settings.endpoints import ENDPOINT_SETTINGS, is_insecure_endpoint
from docchat_core.settings.hosting import running_in_azure
from docchat_core.settings.local import LocalSettings

STARTUP_REFUSAL_EXIT_CODE: Final = 78
"""`EX_CONFIG`: the API's exit code for every start-up failure, refusals included."""

SETTINGS_VALIDATION_RECORD: Final = "settings_validation"
"""The record name of a settings-validation failure, which has no code of its own."""

STARTUP_REFUSALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "local_emulators_in_azure": "Local emulator mode can't run in Azure.",
        "insecure_endpoint": "A configured endpoint isn't a secure remote address.",
    }
)
"""The two start-up refusals shared by both apps, with their messages."""


class StartupCheckError(Exception):
    """A configuration the process must not start with."""

    def __init__(self, code: str | None, message: str, settings: Sequence[str]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.settings = tuple(settings)

    def record(self) -> str:
        """The one-line error record: the code (or `settings_validation`), the message and the
        failing settings' names."""
        return json.dumps(
            {
                "level": "error",
                "name": self.code or SETTINGS_VALIDATION_RECORD,
                "message": self.message,
                "settings": list(self.settings),
            }
        )


class StartupRefusalError(StartupCheckError):
    """`local_emulators_in_azure` or `insecure_endpoint`."""

    def __init__(self, code: str, settings: Sequence[str]) -> None:
        super().__init__(code, STARTUP_REFUSALS[code], settings)


class SettingsValidationError(StartupCheckError):
    """A setting is missing, malformed, out of range or breaks a rule across settings."""

    def __init__(self, settings: Sequence[str]) -> None:
        super().__init__(None, "Settings validation failed.", settings)


def _failing_settings(settings_class: type[DocChatSettings], error: ValidationError) -> list[str]:
    names = settings_class.setting_names()
    known = set(names.values())
    failing: dict[str, None] = {}
    for detail in error.errors(include_url=False, include_input=False, include_context=True):
        rule = detail.get("ctx", {}).get("error")
        if isinstance(rule, SettingRuleError):
            failing.update(dict.fromkeys(rule.settings))
            continue
        location = detail["loc"]
        if location and isinstance(location[0], str):
            key = location[0]
            if key in names:
                failing[names[key]] = None
            elif key in known:
                failing[key] = None
            else:
                failing[f"{SETTINGS_PREFIX}{key}".upper()] = None
        else:
            failing[settings_class.__name__] = None
    return list(failing)


def load_settings[S: DocChatSettings](settings_class: type[S]) -> S:
    """Check 1: read one settings class from the environment.

    Raises `SettingsValidationError` naming each failing setting; the Pydantic error, which
    describes the inputs, is not chained.
    """
    try:
        settings = settings_class()
    except ValidationError as error:
        raise SettingsValidationError(_failing_settings(settings_class, error)) from None
    return settings


def check_local_emulators(local: LocalSettings, environ: Mapping[str, str] | None = None) -> None:
    """Check 2: local-emulator mode inside App Service or Functions is refused."""
    if local.local_emulators and running_in_azure(environ):
        name = LocalSettings.setting_names()["local_emulators"]
        raise StartupRefusalError("local_emulators_in_azure", (name,))


def check_endpoints(local: LocalSettings, *settings: DocChatSettings) -> None:
    """Check 3: outside local-emulator mode, every endpoint setting the given classes read must
    be `https://` and name no loopback host."""
    if local.local_emulators:
        return
    insecure: dict[str, None] = {}
    for instance in settings:
        for field_name, name in type(instance).setting_names().items():
            if name not in ENDPOINT_SETTINGS:
                continue
            value = getattr(instance, field_name)
            if value is not None and is_insecure_endpoint(str(value)):
                insecure[name] = None
    if insecure:
        raise StartupRefusalError("insecure_endpoint", tuple(insecure))


def check_local_environment(core: CoreSettings, environ: Mapping[str, str] | None = None) -> None:
    """Check 4: the environment `local` inside App Service or Functions is refused."""
    if core.core_environment is EnvironmentName.LOCAL and running_in_azure(environ):
        raise SettingsValidationError((CoreSettings.setting_names()["core_environment"],))


def check_identity_client_id(core: CoreSettings) -> None:
    """Check 7: outside `local` the managed-identity client ID is required."""
    if core.core_identity_client_id is None and core.core_environment is not EnvironmentName.LOCAL:
        raise SettingsValidationError((CoreSettings.setting_names()["core_identity_client_id"],))


def exit_on_startup_failure(failure: StartupCheckError, stream: TextIO | None = None) -> NoReturn:
    """Write the failure's one record to standard error and exit with code 78."""
    out = sys.stderr if stream is None else stream
    out.write(failure.record() + "\n")
    out.flush()
    raise SystemExit(STARTUP_REFUSAL_EXIT_CODE)
