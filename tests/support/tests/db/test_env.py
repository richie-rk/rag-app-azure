"""Unit tests of `db/migrations/env.py` (`80-data-model.md` section 2.24; T-072)."""

from __future__ import annotations

import runpy
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import alembic
import pytest
from sqlalchemy import Column, Connection, Integer
from sqlalchemy.dialects import mssql

from docchat_core.sql.tables import metadata

ENV = Path(__file__).resolve().parents[4] / "db" / "migrations" / "env.py"


class FakeContext:
    def __init__(self, offline: bool) -> None:
        self.offline = offline
        self.options: dict[str, Any] = {}
        self.ran = 0
        self.config = SimpleNamespace(attributes={})

    def is_offline_mode(self) -> bool:
        return self.offline

    def configure(self, **options: Any) -> None:
        self.options = options

    @contextmanager
    def begin_transaction(self) -> Iterator[None]:
        yield

    def run_migrations(self) -> None:
        self.ran += 1


def load(
    monkeypatch: pytest.MonkeyPatch, *, offline: bool = True
) -> tuple[FakeContext, dict[str, Any]]:
    fake = FakeContext(offline)
    monkeypatch.setattr(alembic, "context", fake)
    namespace = runpy.run_path(str(ENV))
    return fake, namespace


def test_t072_include_name_admits_only_schema_app(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = load(monkeypatch)
    include_name = fake.options["include_name"]
    assert include_name("app", "schema", {}) is True
    assert include_name("dbo", "schema", {}) is False
    assert include_name(None, "schema", {}) is False
    assert include_name("users", "table", {"schema_name": "app"}) is True


def test_t072_options(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = load(monkeypatch)
    options = fake.options
    assert options["target_metadata"] is metadata
    assert options["include_schemas"] is True
    assert options["transaction_per_migration"] is True
    assert options["version_table_schema"] == "dbo"
    assert options["dialect_name"] == "mssql"
    assert options["literal_binds"] is True
    assert fake.ran == 1


def test_rowversion_matches_its_reflected_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = load(monkeypatch)
    compare = fake.options["compare_type"]
    rowversion = mssql.ROWVERSION()  # type: ignore[no-untyped-call]
    timestamp = mssql.TIMESTAMP()  # type: ignore[no-untyped-call]
    column = Column("row_version", rowversion)
    assert compare(None, column, column, timestamp, rowversion) is False
    other = Column("n", Integer())
    assert compare(None, other, other, Integer(), Integer()) is None


def test_online_connects_as_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, namespace = load(monkeypatch)
    fake.offline = False
    calls: list[tuple[str, str | None]] = []

    class Engine:
        @contextmanager
        def connect(self) -> Iterator[str]:
            yield "connection"

        def dispose(self) -> None:
            calls.append(("dispose", None))

    def create_sync_engine(service: str, *, access_token: str | None) -> Engine:
        calls.append((service, access_token))
        return Engine()

    run_online = namespace["run_migrations_online"]
    monkeypatch.setitem(run_online.__globals__, "create_sync_engine", create_sync_engine)
    monkeypatch.setitem(run_online.__globals__, "access_token", lambda: "token")
    run_online()
    assert calls == [("migrations", "token"), ("dispose", None)]
    assert fake.options["connection"] == "connection"
    assert fake.options["version_table_schema"] == "dbo"


def test_no_token_against_the_local_emulator(monkeypatch: pytest.MonkeyPatch) -> None:
    _, namespace = load(monkeypatch)
    for name in list(__import__("os").environ):
        if name.startswith("DOCCHAT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DOCCHAT_LOCAL_EMULATORS", "true")
    assert namespace["access_token"]() is None


def test_token_from_the_azure_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    _, namespace = load(monkeypatch)
    for name in list(__import__("os").environ):
        if name.startswith("DOCCHAT_"):
            monkeypatch.delenv(name)
    scopes: list[str] = []

    class Credential:
        def get_token(self, scope: str) -> Any:
            scopes.append(scope)
            return type("Token", (), {"token": "cli-token"})()

    access_token = namespace["access_token"]
    monkeypatch.setitem(access_token.__globals__, "AzureCliCredential", Credential)
    assert access_token() == "cli-token"
    assert scopes == ["https://database.windows.net/.default"]


def test_a_fixtures_connection_is_used_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, namespace = load(monkeypatch)
    fake.offline = False
    connection = MagicMock(spec=Connection)
    fake.config.attributes["connection"] = connection

    def no_engine(service: str, *, access_token: str | None) -> None:
        raise AssertionError("the given connection is used")

    run_online = namespace["run_migrations_online"]
    monkeypatch.setitem(run_online.__globals__, "create_sync_engine", no_engine)
    run_online()
    assert fake.options["connection"] is connection
    assert fake.ran == 2
