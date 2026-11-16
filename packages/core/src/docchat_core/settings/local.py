"""`LocalSettings`: the settings that exist only on a developer machine or in a CI job
(`81-config.md` sections 2.11 and 6.3).

Neither is in any site's app-setting set. `DOCCHAT_LOCAL_EMULATORS` switches the client factory
and the SQL layer to the `compose.yaml` services, and start-up refuses it inside Azure
(`check_local_emulators`); the SQL `sa` password is read only in that mode.
"""

from __future__ import annotations

import string
from typing import Annotated, Final

from pydantic import AfterValidator, SecretStr

from docchat_core.settings.base import BoolSetting, DocChatSettings

LOCAL_SQL_SA_PASSWORD_LENGTH: Final = 24

_CHARACTER_CLASSES: Final = (string.ascii_uppercase, string.ascii_lowercase, string.digits)


def _meets_complexity(password: str) -> bool:
    # SQL Server's complexity rules as `docchat-ops local init` applies them: at least one
    # upper-case letter, one lower-case letter, one digit and one symbol.
    has_symbol = any(
        not char.isspace() and char not in string.ascii_letters + string.digits for char in password
    )
    return has_symbol and all(
        any(char in characters for char in password) for characters in _CHARACTER_CLASSES
    )


def _sa_password(value: SecretStr) -> SecretStr:
    password = value.get_secret_value()
    if len(password) != LOCAL_SQL_SA_PASSWORD_LENGTH or not _meets_complexity(password):
        # Constant text: the record of a failure never carries the value.
        raise ValueError(
            "the local SQL password has 24 characters with an upper-case letter, a lower-case "
            "letter, a digit and a symbol"
        )
    return value


SqlSaPassword = Annotated[SecretStr, AfterValidator(_sa_password)]


class LocalSettings(DocChatSettings):
    """Read by the API and the ingestion app; tests construct their clients directly and never
    set the flag."""

    local_emulators: BoolSetting = False
    local_sql_sa_password: SqlSaPassword | None = None
