"""`SqlSettings`: the seven `DOCCHAT_SQL_*` settings of the data-access layer (`81-config.md`
section 2.3; `01-repository.md` section 6.2; ADR 0026).

Both apps, the migrations and `docchat-ops` read them. Bicep writes `DOCCHAT_SQL_SERVER` from the
server's output and the other five numbers from `infra/config/app-settings.json`, which holds a
value for every site, so the class defaults are only what a process that is given none (a
developer's `docchat-ops` run, a test) uses: the API's values.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from docchat_core.settings import DocChatSettings, LocalSettings, SettingsValidationError

SQL_DATABASE_NAME: Final = "docchat"

SQL_SERVER_PATTERN: Final = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.database\.windows\.net$"
"""`<server>.database.windows.net`, the server's fully qualified name as Bicep outputs it."""

SqlServerName = Annotated[str, StringConstraints(pattern=SQL_SERVER_PATTERN)]


class SqlSettings(DocChatSettings):
    """Read once at start-up; an invalid value stops the process before it serves.

    `sql_server` is absent in local-emulator mode, where the layer reaches the `compose.yaml`
    SQL Server instead; `check_sql_server` requires it everywhere else.
    """

    sql_server: SqlServerName | None = None
    sql_database: Literal["docchat"] = SQL_DATABASE_NAME
    sql_threads: Annotated[int, Field(ge=2, le=32)] = 8
    sql_pool_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 10
    sql_connect_timeout_seconds: Annotated[int, Field(ge=5, le=60)] = 30
    sql_command_timeout_seconds: Annotated[int, Field(ge=5, le=300)] = 30
    sql_idle_release_minutes: Annotated[int, Field(ge=1, le=60)] = 10


def check_sql_server(sql: SqlSettings, local: LocalSettings) -> None:
    """Outside local-emulator mode the server name is required (`81-config.md` section 6.1
    check 1: the setting has no default in Azure)."""
    if sql.sql_server is None and not local.local_emulators:
        raise SettingsValidationError((SqlSettings.setting_names()["sql_server"],))
    if local.local_emulators and local.local_sql_sa_password is None:
        raise SettingsValidationError((LocalSettings.setting_names()["local_sql_sa_password"],))
