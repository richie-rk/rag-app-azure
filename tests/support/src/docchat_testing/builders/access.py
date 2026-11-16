"""Row builders of the seven access and configuration tables (`80-data-model.md` sections 2.2 to
2.8).

Each builder returns the values of one valid row for `insert(<table>)`: generated IDs, canary
text for every content column, and the foreign keys the caller passes. Columns with a server
default (times, flags, `row_version`) are left to the server unless a test overrides them; an
override naming a column the table lacks raises `KeyError`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from docchat_core.sql.tables.access import (
    audit_events,
    ops_config,
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_testing.builders.canaries import canary
from docchat_testing.builders.rows import build_row, new_id, sql_now
from docchat_testing.tokens import SESSION_AUTH

OVERRIDE_LIFETIME: Final = timedelta(days=7)
"""Inside the 30-day limit of `CK_user_index_overrides_expiry`."""


def user_row(**overrides: Any) -> dict[str, Any]:
    """An `app.users` row of a `User` in the session's tenant."""
    return build_row(
        users,
        {
            "object_id": new_id(),
            "tenant_id": SESSION_AUTH.tenant_id,
            "display_name": canary("displayname"),
            "email": canary("email"),
            "last_known_role": "User",
        },
        overrides,
    )


def project_row(**overrides: Any) -> dict[str, Any]:
    """An active, non-default `app.projects` row with a canary name, description and
    instructions."""
    actor = new_id()
    return build_row(
        projects,
        {
            "id": new_id(),
            "name": canary("title"),
            "description": canary("summary"),
            "instructions": canary("instruction"),
            "created_by": actor,
            "updated_by": actor,
        },
        overrides,
    )


def project_member_row(user_object_id: UUID, project_id: UUID, **overrides: Any) -> dict[str, Any]:
    """An `app.project_members` row granted by an administrator."""
    return build_row(
        project_members,
        {
            "user_object_id": user_object_id,
            "project_id": project_id,
            "grant_source": "admin",
            "granted_by": new_id(),
        },
        overrides,
    )


def project_index_row(project_id: UUID, search_index_id: UUID, **overrides: Any) -> dict[str, Any]:
    """An `app.project_indexes` mapping that isn't the project's default."""
    actor = new_id()
    return build_row(
        project_indexes,
        {
            "project_id": project_id,
            "search_index_id": search_index_id,
            "is_default": False,
            "created_by": actor,
            "updated_by": actor,
        },
        overrides,
    )


def user_index_override_row(
    user_object_id: UUID, project_id: UUID, search_index_id: UUID, **overrides: Any
) -> dict[str, Any]:
    """An `app.user_index_overrides` row set now and expiring in 7 days; the project must map
    the index (`FK_user_index_overrides_mapping`)."""
    set_at = sql_now()
    return build_row(
        user_index_overrides,
        {
            "user_object_id": user_object_id,
            "project_id": project_id,
            "search_index_id": search_index_id,
            "reason": canary("comment"),
            "set_by": new_id(),
            "set_at": set_at,
            "expires_at": set_at + OVERRIDE_LIFETIME,
        },
        overrides,
    )


def ops_config_row(config_key: str, value_json: str, **overrides: Any) -> dict[str, Any]:
    """An `app.ops_config` row whose value is its default. The migration seeds every key of the
    first version, so a test changes one of those with an update; this builds a key a later
    revision would add."""
    return build_row(
        ops_config,
        {"config_key": config_key, "value_json": value_json, "default_json": value_json},
        overrides,
    )


def audit_event_row(**overrides: Any) -> dict[str, Any]:
    """An `app.audit_events` row: a user restoring a project, one of the actions without
    metadata."""
    project_id = new_id()
    return build_row(
        audit_events,
        {
            "actor_type": "user",
            "actor_object_id": new_id(),
            "action": "project.restored",
            "target_type": "project",
            "target_id": str(project_id),
            "project_id": project_id,
        },
        overrides,
    )
