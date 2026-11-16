"""T-112, the mapping invariant integration tests (`80-data-model.md` sections 2.5, 2.6 and
acceptance criterion 41), at the database's half.

`UX_project_indexes_default` keeps a project to at most one default mapping, and
`FK_user_index_overrides_mapping` refuses an override to an index the project doesn't map and
removes the override when the mapping goes. The other half, that a project's first mapping is its
default and that a project with mappings keeps exactly one through map, unmap and set-default,
belongs to the mapping operations of `11-api.md` section 7.2 and is proven against them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, Table, delete, insert, select
from sqlalchemy.exc import IntegrityError

from docchat_core.sql.tables.access import (
    project_indexes,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.ingestion import search_indexes
from docchat_testing.builders import (
    project_index_row,
    project_row,
    search_index_row,
    user_index_override_row,
    user_row,
)


def put(admin: Engine, *rows: tuple[Table, dict[str, Any]]) -> None:
    with admin.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def mapped_project(admin: Engine) -> tuple[UUID, UUID, UUID]:
    """A project mapping one index as its default, and a second index it doesn't map."""
    project, default, unmapped = project_row(), search_index_row(), search_index_row()
    put(
        admin,
        (projects, project),
        (search_indexes, default),
        (search_indexes, unmapped),
        (project_indexes, project_index_row(project["id"], default["id"], is_default=True)),
    )
    return project["id"], default["id"], unmapped["id"]


def defaults_of(admin: Engine, project_id: UUID) -> list[UUID]:
    with admin.connect() as connection:
        return [
            row.search_index_id
            for row in connection.execute(
                select(project_indexes.c.search_index_id).where(
                    project_indexes.c.project_id == project_id, project_indexes.c.is_default
                )
            )
        ]


# T-112


def test_t112_a_project_holds_at_most_one_default_mapping(admin: Engine) -> None:
    project_id, default_id, unmapped_id = mapped_project(admin)

    with pytest.raises(IntegrityError, match="UX_project_indexes_default"):
        put(admin, (project_indexes, project_index_row(project_id, unmapped_id, is_default=True)))

    assert defaults_of(admin, project_id) == [default_id]


def test_t112_a_second_mapping_that_isnt_default_keeps_the_one_default(admin: Engine) -> None:
    project_id, default_id, other_id = mapped_project(admin)

    put(admin, (project_indexes, project_index_row(project_id, other_id)))

    assert defaults_of(admin, project_id) == [default_id]


def test_t112_an_override_to_an_unmapped_index_is_refused_by_the_mapping_key(
    admin: Engine,
) -> None:
    project_id, _, unmapped_id = mapped_project(admin)
    user = user_row()
    put(admin, (users, user))

    with pytest.raises(IntegrityError, match="FK_user_index_overrides_mapping"):
        put(
            admin,
            (
                user_index_overrides,
                user_index_override_row(user["object_id"], project_id, unmapped_id),
            ),
        )

    with admin.connect() as connection:
        assert (
            connection.execute(
                select(user_index_overrides).where(
                    user_index_overrides.c.user_object_id == user["object_id"]
                )
            ).first()
            is None
        )


def test_t112_unmapping_removes_the_overrides_that_named_the_mapping(admin: Engine) -> None:
    project_id, default_id, other_id = mapped_project(admin)
    user = user_row()
    put(
        admin,
        (users, user),
        (project_indexes, project_index_row(project_id, other_id)),
        (user_index_overrides, user_index_override_row(user["object_id"], project_id, other_id)),
    )

    with admin.begin() as connection:
        connection.execute(
            delete(project_indexes).where(
                project_indexes.c.project_id == project_id,
                project_indexes.c.search_index_id == other_id,
            )
        )

    with admin.connect() as connection:
        remaining = connection.execute(
            select(user_index_overrides).where(
                user_index_overrides.c.user_object_id == user["object_id"]
            )
        ).all()
    assert remaining == []
    assert defaults_of(admin, project_id) == [default_id]
