"""Authentication: `AuthSettings`, `TokenValidator`, the signing-key providers, `Principal` and
the authentication refusals (`11-api.md` section 1.2; `01-repository.md` section 2.2; ADR 0017).
"""

from docchat_core.auth.exceptions import (
    WWW_AUTHENTICATE,
    WWW_AUTHENTICATE_INVALID_TOKEN,
    AuthenticationFailure,
    AuthenticationRequired,
    AuthenticationUnavailable,
    AuthOutcome,
    AuthReason,
    InvalidToken,
    RoleNotAssigned,
)
from docchat_core.auth.keys import (
    EntraSigningKeyProvider,
    SigningKeyProvider,
    StaticSigningKeyProvider,
    jwks_uri,
    parse_key_set,
)
from docchat_core.auth.principal import ROLES, Principal, Role
from docchat_core.auth.settings import (
    TEST_CLIENT_ENVIRONMENTS,
    AuthSettings,
    Client,
    allowed_clients,
    check_test_client,
    issuer_for,
)
from docchat_core.auth.validator import AuthorizationHeader, TokenValidator

__all__ = [
    "ROLES",
    "TEST_CLIENT_ENVIRONMENTS",
    "WWW_AUTHENTICATE",
    "WWW_AUTHENTICATE_INVALID_TOKEN",
    "AuthOutcome",
    "AuthReason",
    "AuthSettings",
    "AuthenticationFailure",
    "AuthenticationRequired",
    "AuthenticationUnavailable",
    "AuthorizationHeader",
    "Client",
    "EntraSigningKeyProvider",
    "InvalidToken",
    "Principal",
    "Role",
    "RoleNotAssigned",
    "SigningKeyProvider",
    "StaticSigningKeyProvider",
    "TokenValidator",
    "allowed_clients",
    "check_test_client",
    "issuer_for",
    "jwks_uri",
    "parse_key_set",
]
