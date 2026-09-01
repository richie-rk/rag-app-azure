"""Magic-link auth: issue a single-use, time-limited token and redeem it for a JWT."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from services.shared.auth import (
    create_jwt,
    generate_magic_link_token,
    get_magic_link_expiry,
)
from services.shared.config import get_settings
from services.shared.models import MagicLink, User

from .users import ensure_default_access

logger = logging.getLogger(__name__)


def create_magic_link(session: Session, email: str) -> dict:
    """Generate a magic link for guest authentication.

    Returns the link URL (in production, this would be emailed).
    """
    settings = get_settings()

    token = generate_magic_link_token()
    expires_at = get_magic_link_expiry()

    magic_link = MagicLink(
        email=email.lower(),
        token=token,
        expires_at=expires_at,
    )
    session.add(magic_link)
    session.commit()

    link_url = f"{settings.magic_link_base_url}?token={token}"

    logger.info("Created magic link for '%s'", email)
    return {"message": "Magic link created", "link": link_url}


def verify_magic_link(session: Session, token: str) -> dict:
    """Validate a magic link token and return a JWT.

    Marks the token as used after validation.
    """
    # Row-lock the link so concurrent /auth/verify calls for the same token
    # serialize: only one transaction can read used=False, flip it, and commit.
    magic_link = (
        session.query(MagicLink)
        .filter(MagicLink.token == token, MagicLink.used == False)
        .with_for_update()
        .first()
    )

    if not magic_link:
        return {"error": "Invalid or already used token", "status": 401}

    # expires_at is written as aware UTC but the column is a naive DateTime,
    # so the driver strips tzinfo on the round-trip. Re-attach UTC before
    # comparing, otherwise naive-vs-aware comparison raises TypeError (500).
    expires_at = magic_link.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return {"error": "Token has expired", "status": 401}

    # Mark as used
    magic_link.used = True

    # Create or get user
    user = session.query(User).filter(User.email == magic_link.email).first()
    if not user:
        user = User(
            email=magic_link.email,
            display_name=magic_link.email.split("@")[0],
            auth_type="magic_link",
            role="guest",
        )
        session.add(user)
        session.flush()

    # Grant viewer access to the default project so the guest has somewhere
    # to read. See ADR-0003.
    ensure_default_access(session, user)
    session.commit()

    jwt_token = create_jwt(
        email=user.email,
        role=user.role,
        auth_type="magic_link",
        display_name=user.display_name or "",
    )

    return {"token": jwt_token, "email": user.email, "role": user.role}
