"""Authorization helpers shared across services.

Project access is recorded in user_project_access. These helpers are the
single source of truth for "may this caller touch this project/index",
used by the chat service (per-request index authorization) and by the
utils service where a lookup by index name is needed.
"""

from sqlalchemy.orm import Session

from .models import Project, User, UserProjectAccess


def get_project_by_index(session: Session, index_name: str) -> Project | None:
    """Return the active project that owns index_name, or None."""
    return (
        session.query(Project)
        .filter(Project.index_name == index_name, Project.is_active.is_(True))
        .first()
    )


def user_can_access_project_id(session: Session, email: str, project_id: int) -> bool:
    """True if the user with this email has an access row for the project."""
    user = session.query(User).filter(User.email == email).first()
    if not user:
        return False
    access = (
        session.query(UserProjectAccess)
        .filter(
            UserProjectAccess.user_id == user.id,
            UserProjectAccess.project_id == project_id,
        )
        .first()
    )
    return access is not None


def user_can_access_index(
    session: Session,
    email: str,
    index_name: str,
    role: str | None = None,
) -> bool:
    """True if the caller may query/ingest into index_name.

    The index name must belong to an active project, and the caller must
    either be a platform admin or hold an access row for that project.
    Unknown index names are always rejected so the services can never be
    pointed at an arbitrary Azure AI Search index.
    """
    project = get_project_by_index(session, index_name)
    if project is None:
        return False
    if role == "admin":
        return True
    return user_can_access_project_id(session, email, project.id)
