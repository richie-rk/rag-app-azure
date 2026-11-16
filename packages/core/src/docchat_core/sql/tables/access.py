"""The seven access and configuration tables and their grants (`80-data-model.md` sections 2.2 to
2.8, 2.20 and 2.22).

Every key, constraint and index carries the name the data model gives it, so `alembic check`
compares names as well as shapes. `project_indexes` and `user_index_overrides` reference
`app.search_indexes`, which `docchat_core.sql.tables.ingestion` defines on the same metadata.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Identity,
    Index,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    literal_column,
)
from sqlalchemy.dialects.mssql import BIT, NVARCHAR, ROWVERSION, UNIQUEIDENTIFIER

from docchat_core.audit.catalogue import AUDIT_ACTIONS, AUDIT_ACTOR_TYPES, AUDIT_TARGET_TYPES
from docchat_core.sql.tables.schema import (
    BIT_FALSE,
    BIT_TRUE,
    EMPTY_NVARCHAR,
    GrantMatrix,
    actor,
    in_list,
    metadata,
    nvarchar_literal,
    rights,
    utc_time,
)

PROJECT_STATUSES: Final = ("active", "archived")
GRANT_SOURCES: Final = ("default", "admin")
USER_ROLES: Final = ("Admin", "User")

users: Final = Table(
    "users",
    metadata,
    Column("object_id", UNIQUEIDENTIFIER, nullable=False),
    Column("tenant_id", UNIQUEIDENTIFIER, nullable=False),
    Column("display_name", NVARCHAR(256), nullable=False, server_default=EMPTY_NVARCHAR),
    Column("email", NVARCHAR(320), nullable=False, server_default=EMPTY_NVARCHAR),
    Column("last_known_role", NVARCHAR(32), nullable=False),
    utc_time("first_seen_at", nullable=False, defaulted=True),
    utc_time("last_seen_at", nullable=False, defaulted=True),
    PrimaryKeyConstraint("object_id", name="PK_users", mssql_clustered=True),
    CheckConstraint("last_known_role IN (N'Admin', N'User')", name="CK_users_last_known_role"),
    CheckConstraint("last_seen_at >= first_seen_at", name="CK_users_seen_order"),
    Index("IX_users_display_name", "display_name", "object_id"),
    Index("IX_users_email", "email", "object_id"),
)

projects: Final = Table(
    "projects",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("name", NVARCHAR(80), nullable=False),
    Column("description", NVARCHAR(400), nullable=False, server_default=EMPTY_NVARCHAR),
    Column("instructions", NVARCHAR(2000), nullable=False, server_default=EMPTY_NVARCHAR),
    Column(
        "example_questions_json",
        NVARCHAR(None),
        nullable=False,
        server_default=literal_column("N'[]'"),
    ),
    Column("is_default", BIT, nullable=False, server_default=BIT_FALSE),
    Column("status", NVARCHAR(32), nullable=False, server_default=nvarchar_literal("active")),
    utc_time("archived_at", nullable=True),
    actor("archived_by", nullable=True),
    utc_time("created_at", nullable=False, defaulted=True),
    actor("created_by", nullable=False),
    utc_time("updated_at", nullable=False, defaulted=True),
    actor("updated_by", nullable=False),
    Column("row_version", ROWVERSION, nullable=False),
    PrimaryKeyConstraint("id", name="PK_projects", mssql_clustered=True),
    UniqueConstraint("name", name="UQ_projects_name"),
    CheckConstraint("LEN(name) >= 1", name="CK_projects_name_not_empty"),
    CheckConstraint(
        "ISJSON(example_questions_json) = 1 AND LEFT(example_questions_json, 1) = N'['",
        name="CK_projects_example_questions_json",
    ),
    CheckConstraint(in_list("status", PROJECT_STATUSES), name="CK_projects_status"),
    CheckConstraint("is_default = 0 OR status = N'active'", name="CK_projects_default_active"),
    CheckConstraint(
        "(status = N'active' AND archived_at IS NULL AND archived_by IS NULL) OR "
        "(status = N'archived' AND archived_at IS NOT NULL AND archived_by IS NOT NULL)",
        name="CK_projects_archive_fields",
    ),
    Index(
        "IX_projects_status_name",
        "status",
        "name",
        mssql_include=["description", "example_questions_json"],
    ),
)
Index(
    "IX_projects_default",
    projects.c.id,
    mssql_where=projects.c.is_default == BIT_TRUE,
)

project_members: Final = Table(
    "project_members",
    metadata,
    Column("user_object_id", UNIQUEIDENTIFIER, nullable=False),
    Column("project_id", UNIQUEIDENTIFIER, nullable=False),
    Column("grant_source", NVARCHAR(32), nullable=False),
    actor("granted_by", nullable=True),
    utc_time("granted_at", nullable=False, defaulted=True),
    PrimaryKeyConstraint(
        "user_object_id", "project_id", name="PK_project_members", mssql_clustered=True
    ),
    ForeignKeyConstraint(
        ["user_object_id"],
        ["app.users.object_id"],
        name="FK_project_members_users",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(["project_id"], ["app.projects.id"], name="FK_project_members_projects"),
    CheckConstraint(in_list("grant_source", GRANT_SOURCES), name="CK_project_members_grant_source"),
    CheckConstraint(
        "(grant_source = N'default' AND granted_by IS NULL) OR "
        "(grant_source = N'admin' AND granted_by IS NOT NULL)",
        name="CK_project_members_granted_by",
    ),
    Index("IX_project_members_project", "project_id", "user_object_id"),
)

project_indexes: Final = Table(
    "project_indexes",
    metadata,
    Column("project_id", UNIQUEIDENTIFIER, nullable=False),
    Column("search_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("is_default", BIT, nullable=False, server_default=BIT_FALSE),
    utc_time("created_at", nullable=False, defaulted=True),
    actor("created_by", nullable=False),
    utc_time("updated_at", nullable=False, defaulted=True),
    actor("updated_by", nullable=False),
    PrimaryKeyConstraint(
        "project_id", "search_index_id", name="PK_project_indexes", mssql_clustered=True
    ),
    ForeignKeyConstraint(["project_id"], ["app.projects.id"], name="FK_project_indexes_projects"),
    ForeignKeyConstraint(
        ["search_index_id"], ["app.search_indexes.id"], name="FK_project_indexes_search_indexes"
    ),
    Index("IX_project_indexes_index", "search_index_id", "project_id"),
)
Index(
    "UX_project_indexes_default",
    project_indexes.c.project_id,
    unique=True,
    mssql_where=project_indexes.c.is_default == BIT_TRUE,
)

user_index_overrides: Final = Table(
    "user_index_overrides",
    metadata,
    Column("user_object_id", UNIQUEIDENTIFIER, nullable=False),
    Column("project_id", UNIQUEIDENTIFIER, nullable=False),
    Column("search_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("reason", NVARCHAR(500), nullable=False),
    actor("set_by", nullable=False),
    utc_time("set_at", nullable=False, defaulted=True),
    utc_time("expires_at", nullable=False),
    PrimaryKeyConstraint(
        "user_object_id", "project_id", name="PK_user_index_overrides", mssql_clustered=True
    ),
    ForeignKeyConstraint(
        ["user_object_id"],
        ["app.users.object_id"],
        name="FK_user_index_overrides_users",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["project_id", "search_index_id"],
        ["app.project_indexes.project_id", "app.project_indexes.search_index_id"],
        name="FK_user_index_overrides_mapping",
        ondelete="CASCADE",
    ),
    CheckConstraint("LEN(reason) >= 10", name="CK_user_index_overrides_reason"),
    CheckConstraint(
        "expires_at > set_at AND expires_at <= DATEADD(DAY, 30, set_at)",
        name="CK_user_index_overrides_expiry",
    ),
    Index("IX_user_index_overrides_mapping", "project_id", "search_index_id"),
)

ops_config: Final = Table(
    "ops_config",
    metadata,
    Column("config_key", NVARCHAR(64), nullable=False),
    Column("value_json", NVARCHAR(None), nullable=False),
    Column("default_json", NVARCHAR(None), nullable=False),
    Column("change_reason", NVARCHAR(500), nullable=True),
    utc_time("updated_at", nullable=False, defaulted=True),
    actor("updated_by", nullable=True),
    Column("row_version", ROWVERSION, nullable=False),
    PrimaryKeyConstraint("config_key", name="PK_ops_config", mssql_clustered=True),
    CheckConstraint(
        "config_key COLLATE Latin1_General_BIN2 NOT LIKE N'%[^a-z0-9_.]%' "
        "AND CHARINDEX(N'.', config_key) > 1 AND RIGHT(config_key, 1) <> N'.'",
        name="CK_ops_config_key",
    ),
    CheckConstraint("ISJSON(value_json) = 1", name="CK_ops_config_value_json"),
    CheckConstraint("ISJSON(default_json) = 1", name="CK_ops_config_default_json"),
    CheckConstraint(
        "change_reason IS NULL OR LEN(change_reason) >= 10", name="CK_ops_config_reason"
    ),
)

audit_events: Final = Table(
    "audit_events",
    metadata,
    Column("id", BigInteger, Identity(start=1, increment=1), nullable=False),
    utc_time("occurred_at", nullable=False, defaulted=True),
    Column("actor_type", NVARCHAR(32), nullable=False),
    Column("actor_object_id", UNIQUEIDENTIFIER, nullable=False),
    Column("subject_object_id", UNIQUEIDENTIFIER, nullable=True),
    Column("action", NVARCHAR(64), nullable=False),
    Column("target_type", NVARCHAR(32), nullable=False),
    Column("target_id", NVARCHAR(128), nullable=True),
    Column("project_id", UNIQUEIDENTIFIER, nullable=True),
    Column("metadata_json", NVARCHAR(None), nullable=True),
    Column("correlation_id", NVARCHAR(32), nullable=True),
    PrimaryKeyConstraint("id", name="PK_audit_events", mssql_clustered=True),
    CheckConstraint(in_list("actor_type", AUDIT_ACTOR_TYPES), name="CK_audit_events_actor_type"),
    CheckConstraint(in_list("action", AUDIT_ACTIONS), name="CK_audit_events_action"),
    CheckConstraint(in_list("target_type", AUDIT_TARGET_TYPES), name="CK_audit_events_target_type"),
    CheckConstraint(
        "metadata_json IS NULL OR (ISJSON(metadata_json) = 1 AND LEN(metadata_json) <= 4000)",
        name="CK_audit_events_metadata",
    ),
    CheckConstraint(
        "correlation_id IS NULL OR (LEN(correlation_id) = 32 AND "
        "correlation_id COLLATE Latin1_General_BIN2 NOT LIKE N'%[^0-9a-f]%')",
        name="CK_audit_events_correlation",
    ),
    Index("IX_audit_events_actor", "actor_object_id", "occurred_at"),
    Index("IX_audit_events_target", "target_type", "target_id", "occurred_at"),
    # The writers hold INSERT only (80-data-model.md section 2.22), and SQL Server needs SELECT on
    # the columns an OUTPUT clause returns, so an insert must not read the identity back.
    implicit_returning=False,
)
Index(
    "IX_audit_events_subject",
    audit_events.c.subject_object_id,
    audit_events.c.occurred_at,
    mssql_where=audit_events.c.subject_object_id.is_not(None),
)
Index(
    "IX_audit_events_project",
    audit_events.c.project_id,
    audit_events.c.occurred_at,
    mssql_where=audit_events.c.project_id.is_not(None),
)

ACCESS_TABLES: Final = (
    users,
    projects,
    project_members,
    project_indexes,
    user_index_overrides,
    ops_config,
    audit_events,
)

ACCESS_GRANTS: Final[GrantMatrix] = {
    "users": {
        "docchat_api": rights("SIU"),
        "docchat_config_operator": rights("S"),
        "docchat_user_data_operator": rights("SD"),
    },
    "projects": {
        "docchat_api": rights("SIU"),
        "docchat_config_operator": rights("S"),
        "docchat_user_data_operator": rights("S"),
    },
    "project_members": {
        "docchat_api": rights("SID"),
        "docchat_user_data_operator": rights("SD"),
    },
    "project_indexes": {
        "docchat_api": rights("SIUD"),
        "docchat_config_operator": rights("S"),
        "docchat_user_data_operator": rights("S"),
    },
    "user_index_overrides": {
        "docchat_api": rights("S"),
        "docchat_config_operator": rights("SIUD"),
        "docchat_user_data_operator": rights("SD"),
    },
    "ops_config": {
        "docchat_api": rights("S"),
        "docchat_ingestion": rights("S"),
        "docchat_config_operator": rights("SU"),
        "docchat_user_data_operator": rights("S"),
    },
    "audit_events": {
        "docchat_api": rights("I"),
        "docchat_ingestion": rights("I"),
        "docchat_config_operator": rights("I"),
        "docchat_conversation_reader": rights("I"),
        "docchat_user_data_operator": rights("SI"),
    },
}
"""The access area's rows of the grant matrix (`80-data-model.md` section 2.22)."""
