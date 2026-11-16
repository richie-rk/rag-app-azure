"""Unit tests of the initial revision, on its offline rendering (`alembic upgrade head --sql`):
the schema equals `docchat_core.sql.tables`, the grants equal the matrix, the seed rows equal the
data model and the configuration catalogue (`80-data-model.md` section 2.24; `81-config.md`
section 3; T-073, and the offline half of T-071, T-078, T-080 and T-086)."""

from __future__ import annotations

import io
import json
import re
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateIndex, CreateTable

from docchat_core.audit.catalogue import AUDIT_ACTIONS, AUDIT_TARGET_TYPES
from docchat_core.sql.tables import DATABASE_ROLES, GRANT_MATRIX, metadata

DB = Path(__file__).resolve().parents[4] / "db"
DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]
CREATE_INDEX = re.compile(r"CREATE (?:UNIQUE )?INDEX \[(\w+)\] ON ")
DROP_EXISTING = " WITH (DROP_EXISTING = ON)"


def spec(name: str) -> str:
    for parent in DB.parents:
        candidate = parent / "docs" / "spec" / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(name)


def config(buffer: io.StringIO | None = None) -> Config:
    return Config(str(DB / "alembic.ini"), output_buffer=buffer)


@cache
def offline_sql() -> str:
    buffer = io.StringIO()
    command.upgrade(config(buffer), "head", sql=True)
    return buffer.getvalue()


@cache
def statements() -> tuple[str, ...]:
    batches = re.split(r"\n\s*GO\s*\n", offline_sql())
    found = []
    for batch in batches:
        # Alembic's "-- Running upgrade" comments aren't statements, and each revision's own
        # transaction (`transaction_per_migration`) begins in the batch of its first statement.
        lines = [
            line
            for line in batch.splitlines()
            if not line.startswith("-- ") and line != "BEGIN TRANSACTION;"
        ]
        text = "\n".join(lines).strip().rstrip(";").strip()
        if text:
            found.append(text)
    return tuple(found)


def revision_module() -> ModuleType:
    script = ScriptDirectory.from_config(config())
    revision = script.get_revision("0001")
    assert revision is not None
    return revision.module


def normalise(ddl: str) -> set[str]:
    return {line.strip().rstrip(",") for line in ddl.strip().splitlines() if line.strip()}


def test_one_linear_history() -> None:
    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["0002"]
    for revision in script.walk_revisions():
        assert re.fullmatch(r"\d{4}", revision.revision)
        assert Path(revision.path).name.startswith(f"{revision.revision}_")


def test_t073_downgrade_rolls_forward() -> None:
    script = ScriptDirectory.from_config(config())
    for revision in script.walk_revisions():
        with pytest.raises(NotImplementedError, match="roll forward"):
            revision.module.downgrade()


def test_version_table_in_dbo() -> None:
    assert "CREATE TABLE dbo.alembic_version" in offline_sql()


def test_t071_every_table_equals_the_metadata() -> None:
    created = {
        match.group(1): statement
        for statement in statements()
        if (match := re.match(r"CREATE TABLE app\.(\w+) \(", statement))
    }
    assert set(created) == {table.name for table in metadata.tables.values()}
    for table in metadata.tables.values():
        expected = str(CreateTable(table).compile(dialect=DIALECT)).strip()
        assert normalise(created[table.name]) == normalise(expected), table.name


def test_t071_every_index_equals_the_metadata() -> None:
    # A later revision rebuilds an index in place under its name (`DROP_EXISTING`), so the last
    # statement naming an index is the one `upgrade head` leaves.
    rendered: dict[str, str] = {}
    for statement in statements():
        if " INDEX " not in statement:
            continue
        match = CREATE_INDEX.match(statement)
        assert match is not None, statement
        rendered[match.group(1)] = statement.removesuffix(DROP_EXISTING)
    expected = {
        str(index.name): str(CreateIndex(index).compile(dialect=DIALECT)).strip()
        for table in metadata.tables.values()
        for index in table.indexes
    }
    assert rendered == expected


def test_circular_keys_follow_both_tables() -> None:
    order = list(statements())

    def position(fragment: str) -> int:
        return next(i for i, statement in enumerate(order) if fragment in statement)

    active = position("ADD CONSTRAINT [FK_search_indexes_active_physical] FOREIGN KEY")
    assert active > position("CREATE TABLE app.physical_indexes (")
    current = position("ADD CONSTRAINT [FK_documents_current_version] FOREIGN KEY")
    assert current > position("CREATE TABLE app.document_versions (")
    assert position("CREATE TABLE app.project_indexes (") > position(
        "CREATE TABLE app.scan_canaries"
    )
    assert position("CREATE SCHEMA app AUTHORIZATION dbo") < position("CREATE ROLE docchat_api")
    assert position("CREATE ROLE docchat_api") < position("CREATE TABLE app.users (")
    assert position("GRANT ") > position("CREATE TABLE app.user_index_overrides (")
    assert position("INSERT INTO app.ingestion_dispatcher_state") > position("GRANT ")


def test_t128_trigger_is_the_data_models_statement() -> None:
    text = spec("80-data-model.md")
    block = re.search(
        r"```sql\n(CREATE TRIGGER app\.trg_document_versions_scan_insert.*?)\n```", text, re.S
    )
    assert block is not None
    (trigger,) = [statement for statement in statements() if "CREATE TRIGGER" in statement]
    assert trigger + ";" == block.group(1) or trigger == block.group(1)
    assert block.group(1) == revision_module().SCAN_INSERT_TRIGGER


def test_t080_roles_and_grants_equal_the_matrix() -> None:
    roles = [s for s in statements() if s.startswith("CREATE ROLE")]
    assert roles == [f"CREATE ROLE {role} AUTHORIZATION dbo" for role in DATABASE_ROLES]
    grants = {s for s in statements() if s.startswith("GRANT")}
    expected: set[str] = set()
    for table, row in GRANT_MATRIX.items():
        for role, cell in row.items():
            verbs = [v for v in ("SELECT", "INSERT", "UPDATE", "DELETE") if v in cell.verbs]
            if verbs:
                expected.add(f"GRANT {', '.join(verbs)} ON OBJECT::app.{table} TO {role}")
            if cell.update_columns:
                columns = ", ".join(cell.update_columns)
                expected.add(f"GRANT UPDATE ON OBJECT::app.{table} ({columns}) TO {role}")
    assert grants == expected
    for grant in grants:
        assert re.fullmatch(
            r"GRANT [A-Z, ]+ ON OBJECT::app\.\w+( \([a-z_, ]+\))? TO docchat_\w+", grant
        )
        assert " public" not in grant
    # The access area's grants come before the ingestion area's.
    order = [s for s in statements() if s.startswith("GRANT")]
    last_access = max(i for i, s in enumerate(order) if "app.audit_events" in s)
    first_ingestion = min(i for i, s in enumerate(order) if "app.ingestion_methods" in s)
    assert last_access < first_ingestion


def test_t078_audit_constraints_carry_the_catalogue() -> None:
    (audit,) = [s for s in statements() if s.startswith("CREATE TABLE app.audit_events")]
    action = re.search(
        r"CONSTRAINT \[CK_audit_events_action\] CHECK \(action IN \(([^)]*)\)\)", audit
    )
    target = re.search(
        r"CONSTRAINT \[CK_audit_events_target_type\] CHECK \(target_type IN \(([^)]*)\)\)", audit
    )
    assert action is not None
    assert target is not None
    assert tuple(re.findall(r"N'([^']+)'", action.group(1))) == AUDIT_ACTIONS
    assert tuple(re.findall(r"N'([^']+)'", target.group(1))) == AUDIT_TARGET_TYPES


def seeded_ops_config() -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for statement in statements():
        match = re.fullmatch(
            r"INSERT INTO app\.ops_config \(config_key, value_json, default_json\) "
            r"VALUES \(N'([a-z0-9_.]+)', N'(.*)', N'(.*)'\)",
            statement,
        )
        if match:
            rows[match.group(1)] = (match.group(2), match.group(3))
    return rows


def test_t086_seventy_three_rows_of_the_data_model() -> None:
    rows = seeded_ops_config()
    assert len(rows) == 73
    text = spec("80-data-model.md")
    documented = dict(re.findall(r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `\w+` \|$", text, re.M))
    assert len(documented) == 73
    assert {key: value for key, (value, _) in rows.items()} == documented
    for value, default in rows.values():
        assert value == default
        assert json.dumps(json.loads(value), sort_keys=True, separators=(",", ":")) == value


def catalogue_defaults() -> dict[str, object]:
    """The defaults of `81-config.md` sections 3.4 to 3.8, parsed as JSON values."""
    text = spec("81-config.md")
    part = text[text.index("### 3.4 Area `api`") : text.index("### 3.9 Values in `load`")]
    defaults: dict[str, object] = {}
    column = 0
    for line in part.splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        if cells[0] == "Key":
            column = cells.index("Default")
            continue
        key = cells[0].strip("`")
        raw = cells[column].strip("`").replace(",", "")
        try:
            defaults[key] = json.loads(raw)
        except json.JSONDecodeError:
            defaults[key] = raw
    return defaults


def test_t086_rows_equal_the_configuration_catalogue() -> None:
    defaults = catalogue_defaults()
    assert len(defaults) == 73
    rows = seeded_ops_config()
    assert set(rows) == set(defaults)
    for key, (value, _) in rows.items():
        assert json.loads(value) == {"value": defaults[key]}, key
        assert type(json.loads(value)["value"]) is type(defaults[key]), key


def test_seed_order_follows_the_owning_areas() -> None:
    keys = list(seeded_ops_config())
    areas = [key.split(".")[0] for key in keys]
    assert areas[0] == "api"
    assert areas[-1] == "ingestion"
    assert areas.index("conversations") > areas.index("pricing")


def test_dispatcher_row_and_method_rows() -> None:
    assert "INSERT INTO app.ingestion_dispatcher_state (id) VALUES (1)" in statements()
    text = spec("80-data-model.md")
    schemas = {
        key: json.loads(block)
        for key, block in re.findall(
            r"`(pagewise|recursive)` version 1 parameter schema.*?```json\n(.*?)\n```", text, re.S
        )
    }
    method_row = r"^\| `(pagewise|recursive)` \| 1 \| ([^|]+) \| ([^|]+) \| `(\{[^`]+\})` \|$"
    table = {
        key: (display, description, defaults)
        for key, display, description, defaults in re.findall(method_row, text, re.M)
    }
    content_types = json.dumps(
        [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ],
        separators=(",", ":"),
    )
    inserted = [s for s in statements() if s.startswith("INSERT INTO app.ingestion_methods")]
    assert len(inserted) == 2
    for statement in inserted:
        values = re.search(r"VALUES \((.*)\)$", statement)
        assert values is not None
        parts = re.findall(r"N'((?:[^']|'')*)'|(\d+)", values.group(1))
        key, version, display, description, schema, defaults, types = (
            text_value or number for text_value, number in parts
        )
        assert version == "1"
        assert (display, description, defaults) == tuple(p.strip() for p in table[key])
        assert schema == json.dumps(schemas[key], separators=(",", ":"), ensure_ascii=False)
        assert types == content_types


def test_revision_identity() -> None:
    module = revision_module()
    assert module.revision == "0001"
    assert module.down_revision is None
    assert not (module.__doc__ or "").startswith("Contract:")
