"""T-074: the migration-check script unit tests (80-data-model.md section 2.24, ADR 0027).

Each case builds a temporary git repository whose base commit holds the earlier revisions, adds
the pull request's revision in a second commit, and answers the deployments API from recordings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_migrations  # noqa: E402

VERSIONS = "db/migrations/versions"

INITIAL = '''\
"""Initial schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320)),
        sa.Index("IX_users_email", "email"),
        schema="app",
    )
    op.create_index("IX_users_old", "users", ["id"], schema="app")
    op.execute("INSERT INTO app.ops_config (config_key, value_json) VALUES ('drop.x', '{}')")


def downgrade() -> None:
    raise NotImplementedError("roll forward")
'''

EXPAND = '''\
"""Add users.display_name."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(200)), schema="app")
    op.create_index("IX_users_display_name", "users", ["display_name"], schema="app")


def downgrade() -> None:
    raise NotImplementedError("roll forward")
'''

REVISION = '''\
"""{docstring}"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"

DROP_SQL = "DROP VIEW app.v_users"


def upgrade() -> None:
{body}


def downgrade() -> None:
    op.drop_column("users", "nickname", schema="app")
    raise NotImplementedError("roll forward")
'''

DESTRUCTIVE = {
    "drop_table": ('op.drop_table("users", schema="app")', "op.drop_table"),
    "drop_column": ('op.drop_column("users", "email", schema="app")', "op.drop_column"),
    "drop_constraint": (
        'op.drop_constraint("FK_users_projects", "users", schema="app")',
        "op.drop_constraint of FK_users_projects",
    ),
    "drop_index_before_parent": (
        'op.drop_index(op.f("IX_users_old"), table_name="users", schema="app")',
        "op.drop_index of IX_users_old",
    ),
    "drop_index_from_create_table": (
        'op.drop_index("IX_users_email", table_name="users", schema="app")',
        "op.drop_index of IX_users_email",
    ),
    "alter_column_type": (
        'op.alter_column("users", "email", type_=sa.String(100), schema="app")',
        "op.alter_column changing type_",
    ),
    "alter_column_not_null": (
        'op.alter_column("users", "email", nullable=False, schema="app")',
        "op.alter_column setting nullable=False",
    ),
    "rename_column": (
        'op.alter_column("users", "email", new_column_name="mail", schema="app")',
        "op.alter_column renaming a column",
    ),
    "rename_table": ('op.rename_table("users", "people", schema="app")', "op.rename_table"),
    "execute_rename": (
        "op.execute(\"EXEC sp_rename 'app.users.email', 'mail', 'COLUMN'\")",
        "op.execute renaming with sp_rename",
    ),
    "execute_drop": ("op.execute(DROP_SQL)", "op.execute with DROP"),
    "execute_truncate": (
        'op.execute(sa.text("TRUNCATE TABLE app.audit_events"))',
        "op.execute with TRUNCATE",
    ),
    "execute_delete": (
        'op.execute("DELETE FROM app.ops_config")',
        "op.execute with a DELETE without WHERE",
    ),
    "batch_drop_column": (
        "\n".join(
            [
                'with op.batch_alter_table("users", schema="app") as batch:',
                '    batch.drop_column("email")',
            ]
        ),
        "op.drop_column",
    ),
}

AUDIT_TARGET_TYPE_SQL = (
    "ALTER TABLE app.audit_events DROP CONSTRAINT CK_audit_events_target_type",
    (
        "ALTER TABLE app.audit_events ADD CONSTRAINT CK_audit_events_target_type "
        "CHECK (target_type IN ('user'))"
    ),
)

NON_DESTRUCTIVE = {
    "add_nullable_column": 'op.add_column("users", sa.Column("bio", sa.String(500)))',
    "drop_index_of_parent": 'op.drop_index("IX_users_display_name", table_name="users")',
    "delete_with_where": "op.execute(\"DELETE FROM app.ops_config WHERE config_key = 'x'\")",
    "words_inside_values": (
        "op.execute(\"INSERT INTO app.ops_config VALUES ('drop', 'truncate', 'delete')\")"
    ),
    "widened_audit_checks": "\n".join(
        [
            'op.drop_constraint("CK_audit_events_action", "audit_events", schema="app")',
            'op.create_check_constraint("CK_audit_events_action", "audit_events", "action IN (1)")',
            *(f"op.execute({json.dumps(sql)})" for sql in AUDIT_TARGET_TYPE_SQL),
        ]
    ),
}

DEPLOYMENTS = "/repos/octo/docchat/deployments?environment=prod&per_page=10"


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._git("init", "-q", "-b", "main")

    def _git(self, *arguments: str) -> str:
        executable = shutil.which("git")
        assert executable is not None
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(self.root),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        completed = subprocess.run(  # noqa: S603
            [executable, "-c", "commit.gpgsign=false", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return completed.stdout.strip()

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            target = self.root / VERSIONS / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "change")
        return self._git("rev-parse", "HEAD")


def revision(body: str, docstring: str = "Rename and drop.") -> str:
    return REVISION.format(docstring=docstring, body=textwrap.indent(body, "    "))


class RecordedDeployments:
    """GitHub's deployments API, from recorded answers: one deployment per (sha, states)."""

    def __init__(self, deployments: list[tuple[str, list[str]]]) -> None:
        self.deployments = deployments
        self.requested: list[str] = []

    def __call__(self, path: str) -> Any:
        self.requested.append(path)
        if path == DEPLOYMENTS:
            return [
                {"id": 100 + index, "sha": sha, "environment": "prod"}
                for index, (sha, _) in enumerate(self.deployments)
            ]
        for index, (_, states) in enumerate(self.deployments):
            if path == f"/repos/octo/docchat/deployments/{100 + index}/statuses":
                return [{"state": state} for state in states]
        raise AssertionError(path)


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/docchat")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    return Repository(tmp_path)


def run(
    repository: Repository,
    monkeypatch: pytest.MonkeyPatch,
    base: str,
    deployments: RecordedDeployments | None = None,
) -> int:
    recorded = deployments or RecordedDeployments([])
    monkeypatch.setattr(check_migrations, "github_get", recorded)
    return check_migrations.main(["--root", str(repository.root), "--base", base])


@pytest.mark.parametrize("case", sorted(DESTRUCTIVE), ids=sorted(DESTRUCTIVE))
def test_t074_a_destructive_operation_in_an_expand_revision_fails(
    repository: Repository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    body, operation = DESTRUCTIVE[case]
    base = repository.commit({"0001_initial_schema.py": INITIAL, "0002_display_name.py": EXPAND})
    repository.commit({"0003_change.py": revision(body)})

    assert run(repository, monkeypatch, base) == 1
    output = capsys.readouterr().out
    assert f"{VERSIONS}/0003_change.py:" in output
    assert f": {operation} in an expand revision" in output
    assert "Contract: completes NNNN" in output


@pytest.mark.parametrize("case", sorted(NON_DESTRUCTIVE), ids=sorted(NON_DESTRUCTIVE))
def test_t074_an_expand_revision_without_destructive_operations_passes(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    base = repository.commit({"0001_initial_schema.py": INITIAL, "0002_display_name.py": EXPAND})
    repository.commit({"0003_change.py": revision(NON_DESTRUCTIVE[case], "Widen.")})

    assert run(repository, monkeypatch, base) == 0


def test_t074_a_contract_revision_whose_expand_reached_prod_passes(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = repository.commit({"0001_initial_schema.py": INITIAL, "0002_display_name.py": EXPAND})
    repository.commit(
        {"0003_change.py": revision('op.drop_column("users", "email")', "Contract: completes 0002")}
    )
    deployments = RecordedDeployments([("f" * 40, ["in_progress", "failure"]), (base, ["success"])])

    assert run(repository, monkeypatch, base, deployments) == 0
    assert deployments.requested[0] == DEPLOYMENTS


def test_t074_a_contract_revision_whose_expand_is_missing_from_prod_fails(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deployed = repository.commit({"0001_initial_schema.py": INITIAL})
    base = repository.commit({"0002_display_name.py": EXPAND})
    repository.commit(
        {"0003_change.py": revision('op.drop_column("users", "email")', "Contract: completes 0002")}
    )

    assert run(repository, monkeypatch, base, RecordedDeployments([(deployed, ["success"])])) == 1
    assert (
        f"{VERSIONS}/0003_change.py: completes 0002, which isn't in commit {deployed} of the "
        "latest successful prod deployment"
    ) in capsys.readouterr().out


def test_t074_a_contract_revision_before_any_successful_prod_deployment_fails(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repository.commit({"0001_initial_schema.py": INITIAL, "0002_display_name.py": EXPAND})
    repository.commit(
        {"0003_change.py": revision('op.drop_table("users")', "Contract: completes 0002")}
    )

    assert run(repository, monkeypatch, base, RecordedDeployments([(base, ["failure"])])) == 1
    assert "no prod deployment has succeeded yet" in capsys.readouterr().out


def test_t074_a_contract_revision_naming_no_earlier_revision_fails(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repository.commit({"0001_initial_schema.py": INITIAL, "0002_display_name.py": EXPAND})
    repository.commit(
        {"0003_change.py": revision('op.drop_table("users")', "Contract: completes 0007")}
    )

    assert run(repository, monkeypatch, base, RecordedDeployments([(base, ["success"])])) == 1
    assert "completes 0007, which isn't an earlier revision" in capsys.readouterr().out


def test_t074_revisions_already_on_the_base_are_not_checked_again(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository.commit({"0001_initial_schema.py": INITIAL})
    base = repository.commit({"0002_change.py": revision('op.drop_table("users")')})
    repository.commit({"0003_display_name.py": EXPAND.replace('"0002"', '"0003"')})

    assert run(repository, monkeypatch, base) == 0


def test_the_base_comes_from_the_pull_request_event(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = repository.commit({"0001_initial_schema.py": INITIAL, "0002_display_name.py": EXPAND})
    repository.commit({"0003_change.py": revision('op.drop_table("users")')})
    event = tmp_path / "event.json"
    event.write_text(f'{{"pull_request": {{"base": {{"sha": "{base}"}}}}}}', encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setattr(check_migrations, "github_get", RecordedDeployments([]))

    assert check_migrations.main(["--root", str(repository.root)]) == 1


def test_without_a_base_the_check_cannot_run(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

    assert check_migrations.main(["--root", str(repository.root)]) == 1
