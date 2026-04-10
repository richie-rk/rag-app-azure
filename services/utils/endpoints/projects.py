"""Project CRUD endpoints.

Consolidates Max AI's create_project + get_projects + project_info into clean
SQLAlchemy ORM operations. No SQL injection, no string formatting.
"""

import json
import logging

from sqlalchemy.orm import Session

from services.shared.models import Project, UserProjectAccess

logger = logging.getLogger(__name__)


def create_project(session: Session, data: dict) -> dict:
    """Create a new project and its search index name.

    Args:
        data: {name, display_name, department, system_prompt, example_questions,
               chunking_strategy, search_strategy, llm_deployment, is_default, user_ids}
    """
    name = data["name"]

    existing = session.query(Project).filter(Project.name == name).first()
    if existing:
        return {"error": "Project name already exists", "status": 409}

    index_name = name.replace(" ", "-").lower() + "-index"

    project = Project(
        name=name,
        display_name=data.get("display_name", name),
        index_name=index_name,
        department=data.get("department", ""),
        system_prompt=data.get("system_prompt", ""),
        example_questions=json.dumps(data.get("example_questions", [])),
        chunking_strategy=data.get("chunking_strategy", "page_wise"),
        search_strategy=data.get("search_strategy", "hybrid"),
        llm_deployment=data.get("llm_deployment", "gpt-4o"),
        is_default=data.get("is_default", False),
    )
    session.add(project)
    session.flush()

    # Associate users if provided
    user_ids = data.get("user_ids", [])
    for user_id in user_ids:
        access = UserProjectAccess(
            user_id=user_id,
            project_id=project.id,
            role="viewer",
        )
        session.add(access)

    session.commit()
    logger.info("Created project '%s' with index '%s'", name, index_name)

    return {
        "id": project.id,
        "name": project.name,
        "index_name": project.index_name,
        "status": 201,
    }


def get_projects(session: Session, user_id: int | None = None) -> list[dict]:
    """Get all projects, optionally filtered by user access.

    Consolidates Max AI's get_projects + project_info.
    """
    query = session.query(Project).filter(Project.is_active == True)

    if user_id:
        query = (
            query.join(UserProjectAccess)
            .filter(UserProjectAccess.user_id == user_id)
        )

    projects = query.all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "display_name": p.display_name,
            "index_name": p.index_name,
            "department": p.department,
            "system_prompt": p.system_prompt,
            "example_questions": json.loads(p.example_questions or "[]"),
            "chunking_strategy": p.chunking_strategy,
            "search_strategy": p.search_strategy,
            "llm_deployment": p.llm_deployment,
            "is_default": p.is_default,
        }
        for p in projects
    ]


def update_project(session: Session, project_id: int, data: dict) -> dict:
    """Update an existing project."""
    project = session.query(Project).filter(Project.id == project_id).first()
    if not project:
        return {"error": "Project not found", "status": 404}

    for field in [
        "display_name", "department", "system_prompt", "chunking_strategy",
        "search_strategy", "llm_deployment", "is_default",
    ]:
        if field in data:
            setattr(project, field, data[field])

    if "example_questions" in data:
        project.example_questions = json.dumps(data["example_questions"])

    session.commit()
    return {"id": project.id, "name": project.name, "status": 200}
