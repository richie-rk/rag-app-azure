"""User provisioning endpoints.

Corporate users sign in with Entra ID SSO; guests use magic links.
Auto-provisions users on first login with default project access.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from services.shared.models import Project, User, UserProjectAccess

logger = logging.getLogger(__name__)


def provision_user(session: Session, data: dict) -> dict:
    """Create or update a user on first login. Grants default project access.

    Args:
        data: {email, display_name, auth_type, role}
    """
    email = data["email"].lower()

    user = session.query(User).filter(User.email == email).first()

    if user:
        # Update last login
        user.last_login = datetime.now(timezone.utc)
        if data.get("display_name"):
            user.display_name = data["display_name"]
        session.commit()
    else:
        # Create new user. Role is derived from auth_type, never taken from
        # the request body, so a caller cannot self-assign the admin role.
        auth_type = data.get("auth_type", "sso")
        user = User(
            email=email,
            display_name=data.get("display_name", email.split("@")[0]),
            auth_type=auth_type,
            role="guest" if auth_type == "magic_link" else "user",
            last_login=datetime.now(timezone.utc),
        )
        session.add(user)
        session.flush()

        # Grant access to all default projects
        default_projects = (
            session.query(Project)
            .filter(Project.is_default == True, Project.is_active == True)
            .all()
        )
        for project in default_projects:
            access = UserProjectAccess(
                user_id=user.id,
                project_id=project.id,
                role="viewer",
            )
            session.add(access)

        session.commit()
        logger.info("Provisioned new user '%s' with %d default projects", email, len(default_projects))

    # Return user info + accessible projects
    project_access = (
        session.query(Project)
        .join(UserProjectAccess)
        .filter(UserProjectAccess.user_id == user.id, Project.is_active == True)
        .all()
    )

    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "auth_type": user.auth_type,
        "projects": [
            {"id": p.id, "name": p.name, "index_name": p.index_name}
            for p in project_access
        ],
    }


def list_users(session: Session) -> list[dict]:
    """List all active users."""
    users = session.query(User).filter(User.is_active == True).all()
    seen_emails = set()
    result = []
    for u in users:
        if u.email not in seen_emails:
            seen_emails.add(u.email)
            result.append({
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "role": u.role,
            })
    return result
