"""`AuthSettings`: the four `DOCCHAT_AUTH_*` settings of token validation, and the identity rules
that start-up checks with them (`81-config.md` sections 2.4 and 6.1 checks 5 and 6; `11-api.md`
section 1.2; ADRs 0017 and 0019).

Bicep writes all four from the tenant and the Microsoft Graph registrations, so none has a code
default except the test client's, which is empty in `prod`. The test client is a non-production
registration: its tokens are accepted only in `dev`, `load` and `local`, and start-up refuses a
`prod` process that names one, so a test token can never reach production data.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import model_validator

from docchat_core.models.identifiers import CanonicalUuid, LowerCaseUuid
from docchat_core.settings import (
    CoreSettings,
    DocChatSettings,
    EnvironmentName,
    SettingRuleError,
    SettingsValidationError,
)

type Client = Literal["web", "test"]
"""`Principal.client`: the registration whose token the caller holds."""

LOGIN_HOST: Final = "https://login.microsoftonline.com"

TEST_CLIENT_ENVIRONMENTS: Final = frozenset(
    {EnvironmentName.DEV, EnvironmentName.LOAD, EnvironmentName.LOCAL}
)
"""The environments that accept the test client's tokens; `local` follows `dev`'s rules."""


class AuthSettings(DocChatSettings):
    """Read once at start-up by the API.

    `auth_tenant_id` is the expected `tid` and the only source of the issuer and signing-key
    URLs; `auth_api_client_id` the expected `aud`; `auth_web_client_id` the allowed `azp`, and
    `auth_test_client_id` the test client's, absent (empty) in `prod`. The three client IDs must
    be distinct (check 6), because a shared ID would let one registration's tokens pass as
    another's.
    """

    auth_tenant_id: LowerCaseUuid
    auth_api_client_id: CanonicalUuid
    auth_web_client_id: CanonicalUuid
    auth_test_client_id: CanonicalUuid | None = None

    @model_validator(mode="after")
    def _client_ids_are_distinct(self) -> Self:
        names = type(self).setting_names()
        ids = {
            names["auth_api_client_id"]: self.auth_api_client_id,
            names["auth_web_client_id"]: self.auth_web_client_id,
        }
        if self.auth_test_client_id is not None:
            ids[names["auth_test_client_id"]] = self.auth_test_client_id
        values = list(ids.values())
        shared = tuple(name for name, value in ids.items() if values.count(value) > 1)
        if shared:
            raise SettingRuleError(shared, "the API, web and test client IDs aren't distinct")
        return self

    @property
    def issuer(self) -> str:
        """The v2 issuer every accepted token names."""
        return issuer_for(self.auth_tenant_id)


def issuer_for(tenant_id: UUID) -> str:
    """`https://login.microsoftonline.com/<tenant ID>/v2.0`."""
    return f"{LOGIN_HOST}/{tenant_id}/v2.0"


def check_test_client(core: CoreSettings, auth: AuthSettings) -> None:
    """Check 5 of `81-config.md` section 6.1: a `prod` process with a test client ID is refused,
    naming the setting and never its value."""
    if core.core_environment is EnvironmentName.PROD and auth.auth_test_client_id is not None:
        raise SettingsValidationError((AuthSettings.setting_names()["auth_test_client_id"],))


def allowed_clients(auth: AuthSettings, environment: EnvironmentName) -> Mapping[UUID, Client]:
    """The allowed `azp` set with each member's client: the web client, plus the test client when
    its setting is present and the environment is `dev`, `load` or `local`."""
    clients: dict[UUID, Client] = {auth.auth_web_client_id: "web"}
    if auth.auth_test_client_id is not None and environment in TEST_CLIENT_ENVIRONMENTS:
        clients[auth.auth_test_client_id] = "test"
    return MappingProxyType(clients)
