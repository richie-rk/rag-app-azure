"""Unit tests of revision 0002, the covering list indexes, on its offline rendering
(`80-data-model.md` sections 2.3, 2.16 and 2.24)."""

from __future__ import annotations

import io
import re
from pathlib import Path
from types import ModuleType

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

DB = Path(__file__).resolve().parents[4] / "db"
REBUILT = (
    "IX_projects_status_name",
    "IX_ingestion_runs_created",
    "IX_ingestion_runs_index_created",
)


def config(buffer: io.StringIO | None = None) -> Config:
    return Config(str(DB / "alembic.ini"), output_buffer=buffer)


def revision_module(number: str) -> ModuleType:
    revision = ScriptDirectory.from_config(config()).get_revision(number)
    assert revision is not None
    return revision.module


def rendered(span: str) -> list[str]:
    """The `CREATE INDEX` statements of the offline upgrade over `span` (`"0001:0002"`)."""
    buffer = io.StringIO()
    command.upgrade(config(buffer), span, sql=True)
    return re.findall(r"^(CREATE (?:UNIQUE )?INDEX .*?);$", buffer.getvalue(), re.MULTILINE)


def test_revision_identity() -> None:
    module = revision_module("0002")
    assert module.revision == "0002"
    assert module.down_revision == "0001"
    assert not (module.__doc__ or "").startswith("Contract:")


def test_each_index_is_rebuilt_in_place_with_its_keys() -> None:
    # Only included columns change, so the release serving `prod` keeps every index it plans with.
    before = {
        statement.split()[2]: statement
        for statement in rendered("base:0001")
        if statement.split()[2].strip("[]") in REBUILT
    }
    after = rendered("0001:0002")
    assert [statement.split()[2].strip("[]") for statement in after] == list(REBUILT)
    for statement in after:
        assert statement.endswith(" WITH (DROP_EXISTING = ON)")
        keys = re.sub(r" INCLUDE \([^)]*\) WITH \(DROP_EXISTING = ON\)$", "", statement)
        assert keys == before[statement.split()[2]]
