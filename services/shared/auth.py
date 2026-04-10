"""JWT creation/validation and magic link helpers.

Shared by all services for consistent auth handling.
"""

import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .config import get_settings

_ALGORITHM = "HS256"
_TOKEN_TTL_HOURS = 24
_MAGIC_LINK_TTL_MINUTES = 15


def create_jwt(
    email: str,
    role: str = "user",
    auth_type: str = "sso",
    display_name: str = "",
) -> str:
    """Create a signed JWT token."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "role": role,
        "auth_type": auth_type,
        "display_name": display_name,
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def validate_jwt(token: str) -> dict:
    """Validate a JWT token and return decoded claims.

    Raises jwt.InvalidTokenError on failure.
    """
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])


def extract_bearer_token(authorization: str | None) -> str | None:
    """Extract token from 'Bearer <token>' header value."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:]


def generate_magic_link_token() -> str:
    """Generate a cryptographically random token for magic links."""
    return secrets.token_urlsafe(48)


def get_magic_link_expiry() -> datetime:
    """Return the expiry datetime for a new magic link."""
    return datetime.now(timezone.utc) + timedelta(minutes=_MAGIC_LINK_TTL_MINUTES)
