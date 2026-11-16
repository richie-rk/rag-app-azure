"""T-530: the contract-enum check fixture tests (01-repository.md section 7.5, ADR 0013).

Each case builds a temporary git repository: the base commit holds the released OpenAPI document
and model, the second commit the pull request's change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_contract_enums  # noqa: E402

DOCUMENT = "apps/api/openapi.json"
MODEL = "apps/api/src/docchat_api/chat/models.py"
ROUTE = "apps/api/src/docchat_api/chat/turns.py"

MODEL_BASE = '''\
from enum import StrEnum


class TurnStatus(StrEnum):
    """The stored status of a turn."""

    COMPLETED = "completed"
    FAILED = "failed"
'''
MODEL_HEAD = MODEL_BASE + '    ABANDONED = "abandoned"\n'
ROUTE_BASE = """\
from docchat_api.chat.models import TurnStatus


def status(done: bool) -> TurnStatus:
    return TurnStatus.COMPLETED if done else TurnStatus.FAILED
"""

ROUTE_HEAD = ROUTE_BASE + "\n\ndef abandoned() -> TurnStatus:\n    return TurnStatus.ABANDONED\n"


def document(
    turn_statuses: list[str], request_kinds: list[str], *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """An OpenAPI 3.1 document with one response enumeration and one request enumeration."""
    schemas: dict[str, Any] = {
        "TurnStatus": {"type": "string", "enum": turn_statuses, "title": "TurnStatus"},
        "Turn": {
            "type": "object",
            "properties": {
                "status": {"$ref": "#/components/schemas/TurnStatus"},
                "role": {"const": "assistant", "type": "string"},
            },
        },
        "FeedbackRequest": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": request_kinds}},
        },
    }
    schemas.update(extra or {})
    return {
        "openapi": "3.1.0",
        "paths": {
            "/v1/sessions/{sessionId}/turns/{turnId}": {
                "get": {
                    "operationId": "getTurn",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Turn"}
                                }
                            }
                        },
                        "404": {"$ref": "#/components/responses/NotFound"},
                    },
                },
                "put": {
                    "operationId": "putTurnFeedback",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/FeedbackRequest"}
                            }
                        }
                    },
                    "responses": {"204": {"description": "saved"}},
                },
            }
        },
        "components": {
            "schemas": schemas,
            "responses": {
                "NotFound": {
                    "content": {
                        "application/problem+json": {
                            "schema": {
                                "type": "object",
                                "properties": {"code": {"type": "string", "enum": ["not_found"]}},
                            }
                        }
                    }
                }
            },
        },
    }


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._run("init", "-q", "-b", "main")

    def _run(self, *arguments: str) -> str:
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

    def commit(self, files: dict[str, str | dict[str, Any]]) -> str:
        for name, content in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            text = content if isinstance(content, str) else json.dumps(content, indent=2) + "\n"
            target.write_text(text, encoding="utf-8")
        self._run("add", "-A")
        self._run("commit", "-q", "-m", "change")
        return self._run("rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    root = tmp_path / "repository"
    root.mkdir()
    return Repository(root)


def released(repository: Repository) -> str:
    return repository.commit(
        {
            DOCUMENT: document(["completed", "failed"], ["helpful", "wrong"]),
            MODEL: MODEL_BASE,
            ROUTE: ROUTE_BASE,
        }
    )


def check(repository: Repository, base: str, *labels: str) -> int:
    arguments = ["--root", str(repository.root), "--base", base]
    for label in labels:
        arguments += ["--label", label]
    return check_contract_enums.main(arguments)


def test_t530_a_gained_value_returned_by_an_enum_member_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    base = released(repository)
    repository.commit(
        {
            DOCUMENT: document(["completed", "failed", "abandoned"], ["helpful", "wrong"]),
            MODEL: MODEL_HEAD,
            ROUTE: ROUTE_HEAD,
        }
    )

    assert check(repository, base) == 1
    output = capsys.readouterr().out
    assert '#/components/schemas/TurnStatus: the response enumeration gains "abandoned"' in output
    assert f'{ROUTE}:9: returns "abandoned" (TurnStatus.ABANDONED) in the same pull request' in (
        output
    )


def test_t530_a_gained_value_returned_as_a_literal_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    base = released(repository)
    repository.commit(
        {
            DOCUMENT: document(["completed", "failed", "abandoned"], ["helpful", "wrong"]),
            MODEL: MODEL_HEAD,
            "packages/core/src/docchat_core/chat/finalise.py": 'STATUS = "abandoned"\n',
        }
    )

    assert check(repository, base) == 1
    assert 'docchat_core/chat/finalise.py:1: returns "abandoned" (literal)' in (
        capsys.readouterr().out
    )


def test_t530_the_label_enum_already_released_passes_the_change(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    base = released(repository)
    repository.commit(
        {
            DOCUMENT: document(["completed", "failed", "abandoned"], ["helpful", "wrong"]),
            MODEL: MODEL_HEAD,
            ROUTE: ROUTE_HEAD,
        }
    )

    assert check(repository, base, "enum-already-released") == 0
    assert "carries the label enum-already-released" in capsys.readouterr().out


def test_t530_the_label_is_read_from_the_pull_request_event(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = released(repository)
    repository.commit(
        {
            DOCUMENT: document(["completed", "failed", "abandoned"], ["helpful", "wrong"]),
            ROUTE: ROUTE_BASE + 'ABANDONED = "abandoned"\n',
        }
    )
    event = tmp_path / "event.json"
    pull_request = {"base": {"sha": base}, "labels": [{"name": "enum-already-released"}]}
    event.write_text(json.dumps({"pull_request": pull_request}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    assert check_contract_enums.main(["--root", str(repository.root)]) == 0
    pull_request["labels"] = [{"name": "documentation"}]
    event.write_text(json.dumps({"pull_request": pull_request}), encoding="utf-8")
    assert check_contract_enums.main(["--root", str(repository.root)]) == 1


def test_t530_adding_the_value_to_the_model_and_document_only_passes(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    base = released(repository)
    repository.commit(
        {
            DOCUMENT: document(["completed", "failed", "abandoned"], ["helpful", "wrong"]),
            MODEL: MODEL_HEAD,
            "apps/api/src/docchat_api/chat/schemas.py": (
                "from typing import Literal\n\n"
                'Status = Literal["completed", "failed", "abandoned"]\n'
                '"""A docstring may mention "abandoned"."""\n'
            ),
        }
    )

    assert check(repository, base) == 0
    output = capsys.readouterr().out
    assert 'gains "abandoned"' in output
    assert "no added code path returns a gained response-enumeration value" in output


def test_t530_a_request_enumeration_may_gain_a_returned_value(repository: Repository) -> None:
    base = released(repository)
    repository.commit(
        {
            DOCUMENT: document(["completed", "failed"], ["helpful", "wrong", "unsafe"]),
            ROUTE: ROUTE_BASE + 'KIND = "unsafe"\n',
        }
    )

    assert check(repository, base) == 0


def test_t530_a_new_response_enumeration_gains_nothing(repository: Repository) -> None:
    base = released(repository)
    extra = {"Phase": {"type": "string", "enum": ["queued", "running"]}}
    head = document(["completed", "failed"], ["helpful", "wrong"], extra=extra)
    turn = head["components"]["schemas"]["Turn"]["properties"]
    turn["phase"] = {"$ref": "#/components/schemas/Phase"}
    repository.commit({DOCUMENT: head, ROUTE: ROUTE_BASE + 'PHASE = "queued"\n'})

    assert check(repository, base) == 0


def test_a_const_that_becomes_an_enum_gains_its_new_values(repository: Repository) -> None:
    base = released(repository)
    head = document(["completed", "failed"], ["helpful", "wrong"])
    head["components"]["schemas"]["Turn"]["properties"]["role"] = {
        "type": "string",
        "enum": ["assistant", "system"],
    }
    repository.commit({DOCUMENT: head, ROUTE: ROUTE_BASE + 'ROLE = "system"\n'})

    assert check(repository, base) == 1


def test_values_on_lines_the_pull_request_did_not_add_are_not_code_paths(
    repository: Repository,
) -> None:
    base = repository.commit(
        {
            DOCUMENT: document(["completed", "failed"], ["helpful", "wrong"]),
            ROUTE: ROUTE_BASE + 'LEGACY = "abandoned"\n',
        }
    )
    repository.commit(
        {DOCUMENT: document(["completed", "failed", "abandoned"], ["helpful", "wrong"])}
    )

    assert check(repository, base) == 0


def test_without_a_base_document_nothing_is_gained(repository: Repository) -> None:
    base = repository.commit({ROUTE: ROUTE_BASE})
    repository.commit(
        {DOCUMENT: document(["completed", "failed"], ["helpful", "wrong"]), MODEL: MODEL_HEAD}
    )

    assert check(repository, base) == 0


def test_an_unknown_base_fails_the_check(repository: Repository) -> None:
    released(repository)

    assert check(repository, "0" * 40) == 1


def test_added_lines_reads_a_zero_context_diff() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -3,0 +4,2 @@\n+a\n+b\n@@ -9 +11 @@\n-c\n+d\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-e\n"
    )

    assert check_contract_enums.added_lines(diff) == {"x.py": {4, 5, 11}}
