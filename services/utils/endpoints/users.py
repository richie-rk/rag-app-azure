"""User provisioning endpoints.

Corporate users sign in with Entra ID SSO; guests use magic links.
Auto-provisions users on first login with default project access.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.shared.models import Project, User, UserProjectAccess

logger = logging.getLogger(__name__)


def _ensure_main_project(session: Session) -> None:
    """Ensure an active default project exists, creating or reactivating
    'main'. Every authenticated user gets viewer access to a default project,
    so an inactive-only default would leave new users with zero access.
    See ADR-0003.
    """
    active_default = (
        session.query(Project)
        .filter(Project.is_default == True, Project.is_active == True)
        .first()
    )
    if active_default:
        return

    # No active default. Reuse an existing 'main' if it is in the table
    # (Project.name is unique, so we cannot create a second).
    main = session.query(Project).filter(Project.name == "main").first()
    if main:
        main.is_active = True
        main.is_default = True
        session.flush()
        logger.info("Reactivated default project 'main'")
        return

    main = Project(
        name="main",
        display_name="Main",
        index_name="main-index",
        department="",
        system_prompt="",
        example_questions="[]",
        chunking_strategy="page_wise",
        search_strategy="hybrid",
        llm_deployment="gpt-4o",
        is_default=True,
        is_active=True,
    )
    session.add(main)
    session.flush()
    logger.info("Bootstrapped default project 'main'")


def ensure_default_access(session: Session, user: User) -> None:
    """Ensure the default project exists and the user has viewer access to
    every default project they do not already have. Idempotent. See ADR-0003.
    """
    _ensure_main_project(session)
    default_projects = (
        session.query(Project)
        .filter(Project.is_default == True, Project.is_active == True)
        .all()
    )
    for project in default_projects:
        already = (
            session.query(UserProjectAccess)
            .filter(
                UserProjectAccess.user_id == user.id,
                UserProjectAccess.project_id == project.id,
            )
            .first()
        )
        if already:
            continue
        # Each insert in a savepoint so a competing transaction that wins the
        # UniqueConstraint(user_id, project_id) race rolls back just this row,
        # not the surrounding work. The competing insert means the grant now
        # exists, which is exactly what we wanted, so swallow the error.
        try:
            with session.begin_nested():
                session.add(
                    UserProjectAccess(
                        user_id=user.id, project_id=project.id, role="viewer"
                    )
                )
        except IntegrityError:
            pass


def provision_user(
    session: Session, claims: dict, display_name: str = ""
) -> dict:
    """Create or update a user from validated claims; ensure default access.

    Identity (email, role, auth_type) is taken only from the validated token,
    never from a request body. The DB role column is refreshed on every login
    so it stays a usable cache for admin views. See ADR-0003.
    """
    email = claims["sub"].lower()
    role = claims["role"]
    auth_type = claims.get("auth_type", "sso")

    user = session.query(User).filter(User.email == email).first()
    if user:
        user.last_login = datetime.now(timezone.utc)
        user.role = role
        if display_name:
            user.display_name = display_name
    else:
        user = User(
            email=email,
            display_name=display_name or email.split("@")[0],
            auth_type=auth_type,
            role=role,
            last_login=datetime.now(timezone.utc),
        )
        session.add(user)
        session.flush()
        logger.info("Provisioned new user '%s'", email)

    ensure_default_access(session, user)
    session.commit()

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
