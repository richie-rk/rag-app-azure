"""JWT creation/validation and magic link helpers.

Two token types are accepted: HS256 JWTs minted by create_jwt for magic-link
guests, and RS256 access tokens issued by Azure AD for SSO users. validate_jwt
branches on the alg header and returns normalized claims in either case.
See ADR-0003.
"""

import secrets
from datetime import datetime, timedelta, timezone

import jwt
from jwt import PyJWKClient

from .config import get_settings

_HS256 = "HS256"
_RS256 = "RS256"
_TOKEN_TTL_HOURS = 24
_MAGIC_LINK_TTL_MINUTES = 15
_LEEWAY_SECONDS = 30
_JWKS_LIFESPAN_SECONDS = 86400  # one day; AAD rotates signing keys rarely

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    """Return a cached JWKS client for the configured tenant."""
    global _jwks_client
    if _jwks_client is None:
        settings = get_settings()
        url = (
            f"https://login.microsoftonline.com/"
            f"{settings.msal_tenant_id}/discovery/v2.0/keys"
        )
        _jwks_client = PyJWKClient(
            url, cache_jwk_set=True, lifespan=_JWKS_LIFESPAN_SECONDS
        )
    return _jwks_client


def create_jwt(
    email: str,
    role: str = "user",
    auth_type: str = "sso",
    display_name: str = "",
) -> str:
    """Mint a signed HS256 JWT.

    Used only by the magic-link flow; SSO tokens come from Azure AD directly.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "role": role,
        "auth_type": auth_type,
        "display_name": display_name,
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_TTL_HOURS),
        "aud": settings.jwt_audience,
        "iss": settings.jwt_issuer,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_HS256)


def validate_jwt(token: str) -> dict:
    """Validate either an HS256 magic-link JWT or an RS256 Azure AD token.

    Returns a normalized claims dict: {sub, role, auth_type, display_name, ...}.
    `role` may be None for an AAD token whose holder is in neither configured
    group; callers (typically _require_auth) translate that into a 403.

    Raises jwt.InvalidTokenError on any validation failure.
    """
    settings = get_settings()
    header = jwt.get_unverified_header(token)
    alg = header.get("alg")

    if alg == _HS256:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[_HS256],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss", "role"]},
        )

    if alg == _RS256:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token).key
        aad_claims = jwt.decode(
            token,
            signing_key,
            algorithms=[_RS256],
            audience=settings.api_audience,
            issuer=(
                f"https://login.microsoftonline.com/"
                f"{settings.msal_tenant_id}/v2.0"
            ),
            leeway=_LEEWAY_SECONDS,
        )
        return _normalize_aad_claims(aad_claims)

    raise jwt.InvalidAlgorithmError(f"Unsupported token algorithm: {alg}")


def _normalize_aad_claims(aad_claims: dict) -> dict:
    """Map an Azure AD token's claims to the shape downstream code expects.

    Role comes from the `groups` claim: transitive membership in the admin
    group wins, then the user group, otherwise None. The groups claim is
    configured (in Azure AD) to emit only groups assigned to the application,
    so it cannot overflow into an overage reference for this codebase.
    """
    settings = get_settings()
    groups = aad_claims.get("groups", [])
    if not isinstance(groups, list):
        groups = []

    if (
        settings.azure_ad_admin_group_id
        and settings.azure_ad_admin_group_id in groups
    ):
        role = "admin"
    elif (
        settings.azure_ad_user_group_id
        and settings.azure_ad_user_group_id in groups
    ):
        role = "user"
    else:
        role = None

    email = (
        aad_claims.get("preferred_username")
        or aad_claims.get("email")
        or aad_claims.get("upn")
        or ""
    ).lower()

    return {
        "sub": email,
        "role": role,
        "auth_type": "sso",
        "display_name": aad_claims.get("name", ""),
        "iat": aad_claims.get("iat"),
        "exp": aad_claims.get("exp"),
        "aud": aad_claims.get("aud"),
        "iss": aad_claims.get("iss"),
    }


def extract_bearer_token(authorization: str | None) -> str | None:
    """Extract the token from a 'Bearer <token>' header value."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:]


def generate_magic_link_token() -> str:
    """Generate a cryptographically random token for magic links."""
    return secrets.token_urlsafe(48)


def get_magic_link_expiry() -> datetime:
    """Return the expiry datetime for a new magic link."""
    return datetime.now(timezone.utc) + timedelta(minutes=_MAGIC_LINK_TTL_MINUTES)
