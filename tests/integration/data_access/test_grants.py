"""The grant matrix on the SQL Server container, one test-only login per role
(`80-data-model.md` section 2.22; ADR 0025; T-076, T-080)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, Table, delete, false, func, insert, select, update
from sqlalchemy.dialects.mssql import ROWVERSION
from sqlalchemy.exc import DBAPIError

from docchat_core.sql.engine import describe_driver_error, driver_error_of
from docchat_core.sql.tables import DATABASE_ROLES, GRANT_MATRIX, metadata
from docchat_core.sql.tables.access import project_indexes, projects, user_index_overrides, users
from docchat_core.sql.tables.ingestion import document_versions, search_indexes

DENIED: int = 229
COLUMN_DENIED: int = 230
ADMIN = uuid.UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
SCAN_COLUMNS = (
    "scan_status",
    "scan_required",
    "scan_time",
    "scan_raw_result",
    "converter_version",
    "converted_sha256",
    "document_pdf_status",
)


def outcome(engine: Engine, build: Callable[[], object]) -> int:
    """The error number of one statement run as a role and rolled back; 0 when it ran."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(build())  # type: ignore[call-overload]
        except DBAPIError as error:
            driver = driver_error_of(error)
            assert driver is not None
            return describe_driver_error(driver).number
        finally:
            if transaction.is_active:
                transaction.rollback()
    return 0


def plain_column(table: Table) -> str:
    """A column an `UPDATE` can name: no identity and no row version."""
    for column in table.columns:
        if column.identity is None and not isinstance(column.type, ROWVERSION):
            return column.name
    raise AssertionError(table.name)


def statements_for(table: Table) -> dict[str, Callable[[], object]]:
    column = plain_column(table)
    first = next(iter(table.columns)).name
    # Every primary-key column the server doesn't generate is named too, so SQLAlchemy has no key
    # column without a value to warn about (warnings are errors, 01-repository.md section 5.4).
    keys = (key.name for key in table.primary_key.columns if key.identity is None)
    nulls = dict.fromkeys([first, *keys])
    return {
        "SELECT": lambda: select(func.count()).select_from(table),
        # Permission is checked before the row is: a permitted insert fails on its NULL instead.
        "INSERT": lambda: insert(table).values(nulls),
        "UPDATE": lambda: update(table).where(false()).values({column: table.c[column]}),
        "DELETE": lambda: delete(table).where(false()),
    }


@pytest.mark.parametrize("role", DATABASE_ROLES)
def test_t080_each_verb_of_each_table(role: str, role_engines: dict[str, Engine]) -> None:
    engine = role_engines[role]
    wrong: list[str] = []
    for name, row in GRANT_MATRIX.items():
        table = metadata.tables[f"app.{name}"]
        cell = row.get(role)  # type: ignore[call-overload]
        granted = set(cell.verbs) if cell else set()
        for verb, build in statements_for(table).items():
            if verb == "UPDATE" and cell is not None and cell.update_columns:
                continue
            number = outcome(engine, build)
            refused = number in (DENIED, COLUMN_DENIED)
            if refused == (verb in granted):
                wrong.append(f"{verb} on app.{name}: error {number}")
    assert wrong == []


@pytest.mark.parametrize(
    ("role", "columns"),
    [
        ("docchat_api", GRANT_MATRIX["document_versions"]["docchat_api"].update_columns),
        (
            "docchat_config_operator",
            GRANT_MATRIX["document_versions"]["docchat_config_operator"].update_columns,
        ),
    ],
)
def test_t080_column_grants_on_document_versions(
    role: str, columns: tuple[str, ...], role_engines: dict[str, Engine]
) -> None:
    engine = role_engines[role]
    for column in columns:
        number = outcome(
            engine,
            lambda column=column: (
                update(document_versions)  # type: ignore[misc]
                .where(false())
                .values({column: document_versions.c[column]})
            ),
        )
        assert number == 0, column
    for column in SCAN_COLUMNS:
        number = outcome(
            engine,
            lambda column=column: (
                update(document_versions)  # type: ignore[misc]
                .where(false())
                .values({column: document_versions.c[column]})
            ),
        )
        assert number == COLUMN_DENIED, column
    ingestion = role_engines["docchat_ingestion"]
    assert (
        outcome(
            ingestion,
            lambda: update(document_versions).where(false()).values(scan_status="clean"),
        )
        == 0
    )


def test_t080_no_role_is_in_a_built_in_role(role_engines: dict[str, Engine]) -> None:
    for engine in role_engines.values():
        with engine.connect() as connection:
            for built_in in ("db_owner", "db_datareader", "db_datawriter", "db_ddladmin"):
                member = connection.exec_driver_sql(
                    "SELECT IS_ROLEMEMBER(?)", (built_in,)
                ).scalar_one()
                assert member == 0, built_in


def test_t076_cascade_under_the_api_role(admin: Engine, role_engines: dict[str, Engine]) -> None:
    user_id, project_id, index_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with admin.begin() as connection:
        connection.execute(
            insert(users).values(object_id=user_id, tenant_id=uuid.uuid4(), last_known_role="User")
        )
        connection.execute(
            insert(projects).values(
                id=project_id,
                name=f"cascade {project_id.hex[:8]}",
                created_by=ADMIN,
                updated_by=ADMIN,
            )
        )
        connection.execute(
            insert(search_indexes).values(
                id=index_id,
                display_name=f"cascade {index_id.hex[:8]}",
                method_key="recursive",
                method_version=1,
                method_parameters="{}",
                method_parameter_hash=b"\x00" * 32,
                embedding_model="text-embedding-3-large",
                embedding_dimensions=1024,
                created_by=ADMIN,
                updated_by=ADMIN,
            )
        )
        connection.execute(
            insert(project_indexes).values(
                project_id=project_id,
                search_index_id=index_id,
                is_default=True,
                created_by=ADMIN,
                updated_by=ADMIN,
            )
        )
        connection.execute(
            insert(user_index_overrides).values(
                user_object_id=user_id,
                project_id=project_id,
                search_index_id=index_id,
                reason="integration test of the cascade",
                set_by=ADMIN,
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
            )
        )
    api = role_engines["docchat_api"]
    assert outcome(api, lambda: delete(user_index_overrides).where(false())) == DENIED
    with api.begin() as connection:
        removed = connection.execute(
            delete(project_indexes).where(project_indexes.c.project_id == project_id)
        )
        assert removed.rowcount == 1
    with admin.connect() as connection:
        left = connection.execute(
            select(func.count())
            .select_from(user_index_overrides)
            .where(user_index_overrides.c.user_object_id == user_id)
        ).scalar_one()
    assert left == 0
