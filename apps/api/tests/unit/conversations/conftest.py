"""Fixtures of the session, feedback and memory route tests: the in-process app over
`FakeContainer`s for the three Cosmos DB containers, the unit suite's fake data-access layer
taught the access queries Q1 and Q4 and the after-commit audit insert, and the chat doubles
(Prompt Shields, and the character tokenizer the store counts fact tokens with).

The app is built in a worker thread when a test starts it (`running_api`), so the build never
holds the test's loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from docchat_api.app import Ports
from docchat_core.audit import AuditEvent, writer
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.sql.engine import SqlEngine, SqlUnavailable
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.api import ApiUnderTest, api_settings, ports_under_test, running_api
from docchat_testing.tokens import SigningKeys

START = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


@dataclass
class AccessScript:
    """What Q1 and Q4 answer, and the audit rows written after a commit.

    `members` maps each project to the users with a membership row; `archived` projects are
    reachable by no one; an administrator reaches every active project. `audit_failures` makes
    that many audit attempts fail as SQL unavailable."""

    members: dict[UUID, set[UUID]] = field(default_factory=dict)
    archived: set[UUID] = field(default_factory=set)
    audits: list[AuditEvent] = field(default_factory=list)
    audit_attempts: int = 0
    audit_failures: int = 0
    failures: dict[str, BaseException] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def grant(self, project: UUID, *users: UUID) -> None:
        self.members.setdefault(project, set()).update(users)

    def revoke(self, project: UUID, user: UUID) -> None:
        self.members.get(project, set()).discard(user)

    def project_row(self, parameters: Mapping[str, Any]) -> SimpleNamespace | None:
        project, oid = parameters["project_id"], parameters["oid"]
        if project not in self.members:
            return None
        return SimpleNamespace(
            id=project,
            name="Travel",
            description="",
            instructions="",
            example_questions_json="[]",
            status="archived" if project in self.archived else "active",
            is_member=1 if oid in self.members[project] else 0,
        )

    def reachable(self, parameters: Mapping[str, Any] | None) -> list[SimpleNamespace]:
        active = [project for project in self.members if project not in self.archived]
        if parameters is None:
            # The administrator form of Q4 takes no parameter.
            return [SimpleNamespace(id=project) for project in active]
        oid = parameters["oid"]
        return [
            SimpleNamespace(project_id=project)
            for project in active
            if oid in self.members[project]
        ]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def access_script(fake_sql: Any, monkeypatch: pytest.MonkeyPatch) -> AccessScript:
    script = AccessScript()
    fetch_one, fetch_all, run = fake_sql.fetch_one, fake_sql.fetch_all, fake_sql.run

    def fail(name: str) -> None:
        script.calls.append(name)
        failure = script.failures.get(name)
        if failure is not None:
            raise failure

    async def answer_one(
        statement: object, parameters: Any = None, *, name: str, retry: str = "transient"
    ) -> Any:
        if name == "authorize_project":
            fail(name)
            return script.project_row(parameters)
        return await fetch_one(statement, parameters, name=name, retry=retry)

    async def answer_all(
        statement: object, parameters: Any = None, *, name: str, retry: str = "transient"
    ) -> Any:
        if name == "reachable_projects":
            fail(name)
            return script.reachable(parameters)
        return await fetch_all(statement, parameters, name=name, retry=retry)

    async def answer_run(work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        if name == "write_audit_event":
            script.audit_attempts += 1
            if script.audit_failures:
                script.audit_failures -= 1
                raise SqlUnavailable("connection", 40613, "")
            script.audits.append(cast("Any", work).keywords["event"])
            return None
        return await run(work, name=name, retry=retry)

    monkeypatch.setattr(fake_sql, "fetch_one", answer_one)
    monkeypatch.setattr(fake_sql, "fetch_all", answer_all)
    monkeypatch.setattr(fake_sql, "run", answer_run)
    return script


@pytest.fixture
def containers(clock: FakeClock) -> dict[str, FakeContainer]:
    return {
        definition.name: FakeContainer.of(definition, clock=clock)
        for definition in (CONVERSATIONS, MEMORIES, USAGE)
    }


@pytest.fixture
def route_fakes() -> ChatFakes:
    """The chat doubles, built outside the test's loop (their HTTP client)."""
    return ChatFakes()


@pytest.fixture
def ports(
    fake_sql: Any,
    clock: FakeClock,
    containers: dict[str, FakeContainer],
    route_fakes: ChatFakes,
    access_script: AccessScript,
) -> Ports:
    return replace(
        ports_under_test(
            sql_engine=cast("SqlEngine", fake_sql), clock=clock, chat=route_fakes.ports()
        ),
        container_wrapper=lambda container: cast("Any", containers[container.id]),
    )


@asynccontextmanager
async def serve(ports: Ports, signing_keys: SigningKeys) -> AsyncIterator[ApiUnderTest]:
    async with running_api(settings=api_settings(), ports=ports, signing_keys=signing_keys) as api:
        yield api


@pytest.fixture
async def api(
    ports: Ports, signing_keys: SigningKeys, captured: Any
) -> AsyncIterator[ApiUnderTest]:
    async with serve(ports, signing_keys) as started:
        yield started


@pytest.fixture
def no_audit_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The after-commit audit helper's retries without their 0.2, 0.8 and 2 s waits."""
    monkeypatch.setattr(writer, "AFTER_COMMIT_RETRY_DELAYS", (0.0, 0.0, 0.0))
