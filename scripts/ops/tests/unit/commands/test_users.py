"""The `user` group (T-541, T-542; `86-operations.md` section 2.8.4;
`13-conversations-and-memory.md` sections 10.5 to 10.11; `80-data-model.md` sections 2.21 and
2.23; `82-security.md` SEC-119 and SEC-120).

Every command runs through the real parser against the scripted SQL layer and the three faked
Cosmos DB containers of `conftest.py`, writing real files under the test's temporary directory.
Their proofs against SQL Server and the Cosmos DB emulator are the user-data integration tests
(T-085, T-329, T-330, T-543, T-546) in `scripts/ops/tests/integration/commands/test_users.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from azure.cosmos.exceptions import CosmosClientTimeoutError, CosmosHttpResponseError
from azure.identity.aio import AzureCliCredential

from docchat_core.sql.engine import SqlUnavailable
from docchat_core.sql.tables import ACTOR_COLUMNS
from docchat_ops.commands import users
from docchat_ops.framework.exitcodes import ExitCode, UsageError
from docchat_testing.builders.cosmos import (
    memory_document,
    session_document,
    turn_document,
    turn_trace_document,
    usage_day_counter,
    usage_month_counter,
)
from docchat_testing.fakes.cosmos import http_error

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.tests.unit.conftest import OpsHarness, Outcome

TICKET = ("--ticket", "DSR-0042", "--reason", "data-subject request from the user")
USER = UUID("0b8f3c1e-6a2d-4f7b-9c55-1d2e3f405162")
OTHER = UUID("9a1b2c3d-4e5f-4061-8a7b-8c9d0e1f2a3b")
PROJECT = UUID("8b9c0d1e-2f3a-4b4c-9d5e-6f7a8b9c0d1e")
HIDDEN_PROJECT = UUID("1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f")
DOCUMENT = UUID("7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d")
OTHER_DOCUMENT = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
MARKER = "0123456789abcdef"
SYSTEM_FIELDS = {"_rid", "_self", "_etag", "_attachments", "_ts"}
COSMOS_DATABASE = "docchat"


def _sdk_error[E: BaseException](cls: Callable[..., E], **kwargs: Any) -> E:
    # The SDK's exception constructors are untyped; the call through a typed callable isn't.
    return cls(**kwargs)


def _items(ops: OpsHarness, container: str = "conversations") -> list[dict[str, Any]]:
    fake = {"conversations": ops.conversations, "memories": ops.memories, "usage": ops.usage}
    return list(fake[container].items.values())


def _seed_session(
    ops: OpsHarness,
    user: UUID = USER,
    *,
    turns: int = 2,
    project: UUID = PROJECT,
    answer: str | None = None,
) -> tuple[UUID, list[UUID]]:
    """A session with `turns` completed turns, each with its trace; returns the turn IDs."""
    session_id = uuid4()
    ops.conversations.put(
        session_document(
            user_id=user, session_id=session_id, project_id=project, turn_count=max(turns, 1)
        ).to_item()
    )
    turn_ids: list[UUID] = []
    for seq in range(1, turns + 1):
        overrides: dict[str, Any] = {"seq": seq, "project_id": project}
        if answer is not None:
            overrides["answer"] = answer
        turn = turn_document(user_id=user, session_id=session_id, **overrides)
        ops.conversations.put(turn.to_item())
        ops.conversations.put(
            turn_trace_document(user_id=user, session_id=session_id, turn=turn.id).to_item()
        )
        turn_ids.append(turn.id)
    return session_id, turn_ids


def _assert_modes(out: Path) -> None:
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for path in out.rglob("*"):
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected, path


def _audit(ops: OpsHarness) -> dict[str, Any]:
    (row,) = ops.database.audit_rows
    return row


def _metadata(row: dict[str, Any]) -> Any:
    return json.loads(row["metadata_json"])


def _no_content(ops: OpsHarness, outcome: Outcome) -> None:
    assert ops.sentinel not in outcome.stdout
    assert ops.sentinel not in outcome.stderr
    assert "data-subject request from the user" not in outcome.stdout


# --- read-conversation ----------------------------------------------------------------------------


def _read(ops: OpsHarness, session_id: UUID, out: Path, *extra: str) -> Outcome:
    return ops.run(
        "--env",
        "dev",
        "--json",
        "user",
        "read-conversation",
        "--user",
        str(USER),
        "--session",
        str(session_id),
        "--out",
        str(out),
        *extra,
        *TICKET,
    )


def test_read_conversation_writes_the_session_and_its_audit_row(
    ops: OpsHarness, tmp_path: Path
) -> None:
    session_id, turn_ids = _seed_session(ops, answer=f"An answer {ops.sentinel}")
    _seed_session(ops, OTHER)
    out = tmp_path / "read"
    outcome = _read(ops, session_id, out)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    _assert_modes(out)
    written = json.loads((out / f"{session_id}.json").read_text(encoding="utf-8"))
    assert written["session"]["sessionId"] == str(session_id)
    assert [turn["id"] for turn in written["turns"]] == [str(turn) for turn in turn_ids]
    assert [trace["turnId"] for trace in written["turnTraces"]] == [str(t) for t in turn_ids]
    for document in (written["session"], *written["turns"], *written["turnTraces"]):
        assert not SYSTEM_FIELDS & set(document)
    assert ops.sentinel in written["turns"][0]["answer"]
    row = _audit(ops)
    assert row["action"] == "conversation.read"
    assert row["actor_type"] == "operator"
    assert str(row["actor_object_id"]) == ops.operator_oid
    assert str(row["subject_object_id"]) == str(USER)
    assert row["target_type"] == "session"
    assert row["target_id"] == str(session_id)
    assert row["project_id"] is None
    assert row["correlation_id"] == outcome.correlation_id
    assert _metadata(row) == {
        "turnIds": [str(turn) for turn in turn_ids],
        "ticket": "DSR-0042",
        "level": "read",
    }
    report = outcome.json()
    assert (report["result"], report["turns"], report["turnTraces"]) == ("read", 2, 2)
    assert report["file"] == str(out / f"{session_id}.json")
    _no_content(ops, outcome)
    assert "Dry run" not in outcome.stderr
    assert ops.cosmos_targets == [(ops.cosmos_endpoint, COSMOS_DATABASE)]
    assert ops.cosmos_closed
    read = ops.conversations.calls_of("read_item")[0]
    assert read.partition_key == (str(USER), str(session_id))
    assert read.kwargs["timeout"] == users.COSMOS_OPERATION_TIMEOUT_SECONDS
    for query in ops.conversations.calls_of("query_items"):
        assert query.partition_key == (str(USER), str(session_id))
        assert query.kwargs["timeout"] == users.COSMOS_OPERATION_TIMEOUT_SECONDS
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("write_audit_event", "none"),
    ]


def test_read_conversation_reads_the_newest_50_turns_and_their_traces(
    ops: OpsHarness, tmp_path: Path
) -> None:
    session_id, turn_ids = _seed_session(ops, turns=53)
    outcome = _read(ops, session_id, tmp_path / "read")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    written = json.loads((tmp_path / "read" / f"{session_id}.json").read_text(encoding="utf-8"))
    assert [turn["seq"] for turn in written["turns"]] == list(range(4, 54))
    assert {trace["turnId"] for trace in written["turnTraces"]} == {str(t) for t in turn_ids[3:]}
    assert _metadata(_audit(ops))["turnIds"] == [str(turn) for turn in turn_ids[3:]]
    first, second = ops.conversations.calls_of("query_items")
    assert first.query == users.TURNS_QUERY
    assert second.query == users.TRACES_BY_TURN_QUERY


def test_read_conversation_reads_only_the_given_turns(ops: OpsHarness, tmp_path: Path) -> None:
    session_id, turn_ids = _seed_session(ops, turns=4)
    wanted = [str(turn_ids[2]), str(turn_ids[0]), str(turn_ids[2]).upper()]
    outcome = _read(ops, session_id, tmp_path / "read", *(f"--turn={turn}" for turn in wanted))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    written = json.loads((tmp_path / "read" / f"{session_id}.json").read_text(encoding="utf-8"))
    assert [turn["seq"] for turn in written["turns"]] == [1, 3]
    assert [trace["turnId"] for trace in written["turnTraces"]] == [
        str(turn_ids[0]),
        str(turn_ids[2]),
    ]
    assert _metadata(_audit(ops))["turnIds"] == [str(turn_ids[0]), str(turn_ids[2])]
    turns_query = ops.conversations.calls_of("query_items")[0]
    assert turns_query.query == users.TURNS_BY_ID_QUERY
    parameters = {item["name"]: item["value"] for item in turns_query.kwargs["parameters"]}
    assert parameters["@turnIds"] == [str(turn_ids[2]), str(turn_ids[0])]
    assert parameters["@type"] == "turn"


def test_read_conversation_refuses_more_than_50_turns_before_any_sign_in(
    ops: OpsHarness, tmp_path: Path
) -> None:
    turns = [f"--turn={uuid4()}" for _ in range(51)]
    outcome = _read(ops, uuid4(), tmp_path / "read", *turns)
    assert outcome.code == ExitCode.USAGE
    assert "at most 50" in outcome.failure
    assert ops.credentials_made == 0
    assert not (tmp_path / "read").exists()


def test_read_conversation_of_a_missing_session_exits_4_with_nothing_written(
    ops: OpsHarness, tmp_path: Path
) -> None:
    out = tmp_path / "read"
    outcome = _read(ops, uuid4(), out)
    assert outcome.code == ExitCode.NOT_FOUND
    assert not out.exists()
    assert ops.database.audit_rows == []


def test_t541_out_refuses_a_directory_that_holds_files(ops: OpsHarness, tmp_path: Path) -> None:
    out = tmp_path / "read"
    out.mkdir()
    (out / "earlier.json").write_text("{}", encoding="utf-8")
    outcome = _read(ops, uuid4(), out)
    assert outcome.code == ExitCode.USAGE
    assert "already holds files" in outcome.failure
    assert ops.credentials_made == 0


def test_t541_out_takes_an_empty_directory_as_it_is(ops: OpsHarness, tmp_path: Path) -> None:
    session_id, _ = _seed_session(ops)
    out = tmp_path / "read"
    out.mkdir(mode=0o750)
    out.chmod(0o750)
    assert _read(ops, session_id, out).code == ExitCode.SUCCESS
    assert stat.S_IMODE(out.stat().st_mode) == 0o750
    assert stat.S_IMODE((out / f"{session_id}.json").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda out: out.write_text("x", encoding="utf-8"), "isn't a directory"),
        (lambda out: out.symlink_to(out.parent), "symbolic link"),
    ],
)
def test_out_refuses_a_file_or_a_link(
    ops: OpsHarness, tmp_path: Path, make: Any, message: str
) -> None:
    out = tmp_path / "read"
    make(out)
    outcome = _read(ops, uuid4(), out)
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure


def test_out_needs_an_existing_parent(ops: OpsHarness, tmp_path: Path) -> None:
    outcome = _read(ops, uuid4(), tmp_path / "missing" / "read")
    assert outcome.code == ExitCode.USAGE
    assert "doesn't exist" in outcome.failure


def test_read_conversation_removes_its_file_when_the_audit_row_fails(
    ops: OpsHarness, tmp_path: Path
) -> None:
    session_id, _ = _seed_session(ops)
    ops.database.write_error = SqlUnavailable("connection")
    out = tmp_path / "read"
    outcome = _read(ops, session_id, out)
    assert outcome.code == ExitCode.UNREACHABLE
    assert not out.exists()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (http_error(403), ExitCode.ACCESS_DENIED),
        (
            _sdk_error(
                CosmosHttpResponseError,
                status_code=403,
                message="Request originated from client IP 203.0.113.9 through public internet.",
            ),
            ExitCode.UNREACHABLE,
        ),
        (_sdk_error(CosmosClientTimeoutError), ExitCode.UNREACHABLE),
        (ServiceRequestError("refused"), ExitCode.UNREACHABLE),
        (http_error(503), ExitCode.UNREACHABLE),
        (ClientAuthenticationError("no token"), ExitCode.ACCESS_DENIED),
        (http_error(400), ExitCode.UNEXPECTED),
    ],
)
def test_read_conversation_maps_cosmos_failures(
    ops: OpsHarness, tmp_path: Path, error: BaseException, code: ExitCode
) -> None:
    session_id, _ = _seed_session(ops)
    ops.conversations.fail("read_item", error=error)
    out = tmp_path / "read"
    outcome = _read(ops, session_id, out)
    assert outcome.code == code
    assert not out.exists()
    assert ops.database.audit_rows == []
    assert "Request originated" not in outcome.stderr


def test_read_conversation_without_the_sql_level_exits_3_before_cosmos(
    ops: OpsHarness, tmp_path: Path
) -> None:
    ops.database.token = None
    outcome = _read(ops, uuid4(), tmp_path / "read")
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.cosmos_targets == []


def test_user_commands_refuse_a_cosmos_database_other_than_docchat(
    ops: OpsHarness, tmp_path: Path
) -> None:
    ops.write_azd("dev", DOCCHAT_COSMOS_DATABASE="other")
    outcome = _read(ops, uuid4(), tmp_path / "read")
    assert outcome.code == ExitCode.USAGE
    assert "azd env refresh -e dev" in outcome.failure
    assert ops.credentials_made == 0


def test_user_commands_refuse_an_insecure_cosmos_endpoint(ops: OpsHarness, tmp_path: Path) -> None:
    ops.write_azd("dev", DOCCHAT_COSMOS_ENDPOINT="http://127.0.0.1:8081")
    assert _read(ops, uuid4(), tmp_path / "read").code == ExitCode.USAGE


# --- export ---------------------------------------------------------------------------------------


def _user_row() -> dict[str, Any]:
    return {
        "object_id": USER,
        "tenant_id": UUID(int=1),
        "display_name": "Avery Example",
        "email": "avery@example.com",
        "last_known_role": "User",
        "first_seen_at": datetime(2026, 9, 1, 8, 0),  # noqa: DTZ001 - DATETIME2 has no zone
        "last_seen_at": datetime(2026, 10, 29, 17, 30),  # noqa: DTZ001
    }


def _audit_row(row_id: int, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": row_id,
        "occurred_at": datetime(2026, 10, 1, 9, 0),  # noqa: DTZ001
        "actor_type": "user",
        "actor_object_id": USER,
        "subject_object_id": USER,
        "action": "memory.paused",
        "target_type": "memory",
        "target_id": str(USER),
        "project_id": None,
        "metadata_json": None,
        "correlation_id": "0af7651916cd43dd8448eb211c80319c",
    }
    return row | overrides


def _script_export_read(
    ops: OpsHarness,
    *,
    user_row: dict[str, Any] | None = None,
    memberships: tuple[dict[str, Any], ...] = (),
    overrides: tuple[dict[str, Any], ...] = (),
    actor_events: tuple[dict[str, Any], ...] = (),
    subject_events: tuple[dict[str, Any], ...] = (),
    references: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> None:
    database = ops.database
    database.answer()
    database.answer(*([user_row] if user_row else []))
    database.answer(*memberships)
    database.answer(*overrides)
    database.answer(*actor_events)
    database.answer(*subject_events)
    for table in sorted(ACTOR_COLUMNS):
        for column in ACTOR_COLUMNS[table]:
            # The fake answers every read of `app.ops_config` from its own rows.
            if table != "ops_config":
                database.answer(*(references or {}).get((table, column), []))
    database.answer()


def _export(ops: OpsHarness, out: Path) -> Outcome:
    return ops.run(
        "--env", "dev", "--json", "user", "export", "--user", str(USER), "--out", str(out), *TICKET
    )


def _seed_everything(ops: OpsHarness) -> dict[str, Any]:
    visible, visible_turns = _seed_session(
        ops, answer="Covered at all sites ⟦1⟧, two exceptions ⟦2⟧."
    )
    hidden, _ = _seed_session(ops, turns=1, project=HIDDEN_PROJECT)
    turn_key = (str(USER), str(visible))
    turn = ops.conversations.get(turn_key, str(visible_turns[0]))
    assert turn is not None
    citation = {
        "number": 1,
        "sourceId": "S1",
        "chunkKey": f"{DOCUMENT}_{uuid4()}_0",
        "searchIndexId": str(uuid4()),
        "physicalIndexId": str(uuid4()),
        "documentId": str(DOCUMENT),
        "documentVersionId": str(uuid4()),
        "chunkSetId": str(uuid4()),
        "title": "Supplier policy",
        "pageNumber": 3,
        "headingPath": ["Scope"],
        "contentType": "application/pdf",
        "openAs": "pdf_page",
    }
    ops.conversations.touch(turn_key, str(visible_turns[0]), citations=[citation])
    # A turn whose session document has expired.
    orphan_session = uuid4()
    ops.conversations.put(
        turn_document(user_id=USER, session_id=orphan_session, project_id=PROJECT).to_item()
    )
    ops.memories.put(memory_document(user_id=USER).to_item())
    ops.usage.put(usage_day_counter(user_id=USER, total_tokens=0).to_item())
    ops.usage.put(usage_month_counter(user_id=USER).to_item())
    _seed_session(ops, OTHER)
    ops.memories.put(memory_document(user_id=OTHER).to_item())
    return {"visible": visible, "hidden": hidden, "orphan": orphan_session}


EXPORT_COUNTS = {
    "conversations": 9,
    "memories": 1,
    "usage": 2,
    "users": 1,
    "projectMembers": 1,
    "userIndexOverrides": 1,
    "auditEvents": 3,
    "actorReferences": 1,
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_lines(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _run_full_export(ops: OpsHarness, tmp_path: Path) -> tuple[Outcome, Path, dict[str, Any]]:
    """A user with a visible, a hidden and an expired session, memory, usage, SQL rows, audit
    rows and an actor reference; another user's data beside it."""
    seeded = _seed_everything(ops)
    project_reference = {"id": PROJECT}
    _script_export_read(
        ops,
        user_row=_user_row(),
        memberships=(
            {
                "user_object_id": USER,
                "project_id": PROJECT,
                "grant_source": "admin",
                "granted_by": OTHER,
                "granted_at": datetime(2026, 9, 2, 10, 0),  # noqa: DTZ001
                "project_name": "Procurement",
            },
        ),
        overrides=(
            {
                "user_object_id": USER,
                "project_id": PROJECT,
                "search_index_id": DOCUMENT,
                "set_by": OTHER,
                "set_at": datetime(2026, 10, 1, 9, 0),  # noqa: DTZ001
                "expires_at": datetime(2026, 10, 8, 9, 0),  # noqa: DTZ001
            },
        ),
        actor_events=(_audit_row(7), _audit_row(9)),
        subject_events=(
            _audit_row(9),
            _audit_row(12, actor_type="operator", actor_object_id=OTHER),
        ),
        references={("projects", "created_by"): [project_reference]},
    )
    ops.database.answer({"id": PROJECT, "name": "Procurement"})
    out = tmp_path / "export"
    outcome = _export(ops, out)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    return outcome, out, seeded


def test_t541_export_writes_every_file_with_mode_0600(ops: OpsHarness, tmp_path: Path) -> None:
    outcome, out, _ = _run_full_export(ops, tmp_path)
    _assert_modes(out)
    files = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
    assert files == sorted([*users._EXPORT_FILES, "manifest.json"])
    _no_content(ops, outcome)
    assert "Avery" not in outcome.stdout


def test_export_subject_package(ops: OpsHarness, tmp_path: Path) -> None:
    _, out, seeded = _run_full_export(ops, tmp_path)
    profile = _read_json(out / "subject/profile.json")
    assert profile == {
        "displayName": "Avery Example",
        "email": "avery@example.com",
        "firstSeenAt": "2026-09-01T08:00:00.000Z",
        "lastSeenAt": "2026-10-29T17:30:00.000Z",
        "lastKnownRole": "User",
    }
    assert _read_json(out / "subject/memberships.json") == [
        {
            "projectId": str(PROJECT),
            "projectName": "Procurement",
            "grantedAt": "2026-09-02T10:00:00.000Z",
        }
    ]
    by_id = {session["sessionId"]: session for session in _read_json(out / "subject/sessions.json")}
    assert set(by_id) == {str(seeded["visible"]), str(seeded["hidden"]), str(seeded["orphan"])}
    visible = by_id[str(seeded["visible"])]
    assert visible["projectName"] == "Procurement"
    assert [turn["seq"] for turn in visible["turns"]] == [1, 2]
    first = visible["turns"][0]
    assert first["answer"] == "Covered at all sites, two exceptions."
    assert first["citations"] == [
        {
            "number": 1,
            "title": "Supplier policy",
            "pageNumber": 3,
            "headingPath": ["Scope"],
            "contentType": "application/pdf",
            "openAs": "pdf_page",
        }
    ]
    assert set(first) == {
        "seq",
        "status",
        "question",
        "answer",
        "citations",
        "followups",
        "feedback",
        "createdAt",
        "endedAt",
    }
    assert by_id[str(seeded["hidden"])]["projectName"] is None
    assert by_id[str(seeded["orphan"])]["title"] is None
    memory = _read_json(out / "subject/memory.json")
    assert memory["paused"] is False
    assert set(memory["facts"][0]) == {"text", "category", "createdAt", "updatedAt"}
    usage = _read_json(out / "subject/usage.json")
    assert (len(usage["days"]), len(usage["months"])) == (1, 1)


def test_export_internal_package(ops: OpsHarness, tmp_path: Path) -> None:
    _, out, _ = _run_full_export(ops, tmp_path)
    conversations = _read_lines(out / "internal/conversations.jsonl")
    traces = _read_lines(out / "internal/turn_traces.jsonl")
    assert {document["type"] for document in conversations} == {"session", "turn"}
    assert len(conversations) == 6
    assert {document["type"] for document in traces} == {"turn_trace"}
    assert len(traces) == 3
    assert all(document["userId"] == str(USER) for document in (*conversations, *traces))
    assert all(not SYSTEM_FIELDS & set(document) for document in (*conversations, *traces))
    raw_turn = next(document for document in conversations if document.get("citations"))
    assert "⟦1⟧" in raw_turn["answer"]
    raw_memory = _read_json(out / "internal/memory.json")
    assert raw_memory["userId"] == str(USER)
    assert not SYSTEM_FIELDS & set(raw_memory)
    assert [event["id"] for event in _read_json(out / "internal/audit_events.json")] == [7, 9, 12]
    assert _read_json(out / "internal/actor_references.json") == [
        {"table": "projects", "column": "created_by", "primaryKey": {"id": str(PROJECT)}}
    ]
    overrides = _read_json(out / "internal/user_index_overrides.json")
    assert overrides[0]["search_index_id"] == str(DOCUMENT)
    assert "reason" not in overrides[0]


def test_export_manifest_and_audit_row(ops: OpsHarness, tmp_path: Path) -> None:
    outcome, out, _ = _run_full_export(ops, tmp_path)
    counts = EXPORT_COUNTS
    manifest_bytes = (out / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["user"] == str(USER)
    assert manifest["environment"] == "dev"
    assert manifest["ticket"] == "DSR-0042"
    assert manifest["operator"] == ops.operator_oid
    assert manifest["createdAt"] == "2026-10-30T12:00:00.000Z"
    assert manifest["counts"] == counts
    assert manifest["commandVersion"]
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((out / relative).read_bytes()).hexdigest() == digest
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    row = _audit(ops)
    assert row["action"] == "user_data.exported"
    assert (row["target_type"], row["target_id"]) == ("user", str(USER))
    assert str(row["subject_object_id"]) == str(USER)
    assert row["correlation_id"] == outcome.correlation_id
    assert _metadata(row) == {
        "ticket": "DSR-0042",
        "counts": counts,
        "manifestSha256": manifest_sha256,
    }
    report = outcome.json()
    assert report["counts"] == counts
    assert report["manifestSha256"] == manifest_sha256


def test_export_reads_sql_in_one_snapshot_and_cosmos_by_partition(
    ops: OpsHarness, tmp_path: Path
) -> None:
    _run_full_export(ops, tmp_path)
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("user_export_read", "transient"),
        ("user_export_project_names", "transient"),
        ("write_audit_event", "none"),
    ]
    statements = ops.database.sql("user_export_read")
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL SNAPSHOT"
    # The driver's transaction holding only the `SET` ends, so the reads start at `SNAPSHOT`.
    assert ops.database.driver_commits == ["user_export_read"]
    assert statements[-1] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert any("JOIN app.projects" in statement for statement in statements)
    assert all("reason" not in statement for statement in statements if "overrides" in statement)
    prefix = ops.conversations.calls_of("query_items")[0]
    assert prefix.query == users.USER_ITEMS_QUERY
    assert prefix.partition_key == (str(USER),)
    assert ops.memories.calls_of("query_items")[0].partition_key == (str(USER),)


def test_export_of_a_user_with_no_data_exits_4_and_leaves_nothing(
    ops: OpsHarness, tmp_path: Path
) -> None:
    _seed_session(ops, OTHER)
    _script_export_read(ops)
    out = tmp_path / "export"
    outcome = _export(ops, out)
    assert outcome.code == ExitCode.NOT_FOUND
    assert not out.exists()
    assert ops.database.audit_rows == []


def test_export_into_an_empty_directory_leaves_it_empty_on_failure(
    ops: OpsHarness, tmp_path: Path
) -> None:
    _seed_session(ops)
    _script_export_read(ops, user_row=_user_row())
    ops.conversations.fail("query_items", error=_sdk_error(CosmosClientTimeoutError))
    out = tmp_path / "export"
    out.mkdir()
    outcome = _export(ops, out)
    assert outcome.code == ExitCode.UNREACHABLE
    assert out.is_dir()
    assert list(out.iterdir()) == []
    assert ops.database.audit_rows == []


def test_export_removes_the_packages_when_the_audit_row_fails(
    ops: OpsHarness, tmp_path: Path
) -> None:
    _script_export_read(ops, user_row=_user_row())
    ops.database.write_error = SqlUnavailable("connection")
    out = tmp_path / "export"
    outcome = _export(ops, out)
    assert outcome.code == ExitCode.UNREACHABLE
    assert not out.exists()


def test_export_without_the_level_exits_3_before_writing(ops: OpsHarness, tmp_path: Path) -> None:
    ops.database.unit_errors["user_export_read"] = SqlUnavailable("connection", number=229)
    out = tmp_path / "export"
    outcome = _export(ops, out)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert not out.exists()


# --- delete ---------------------------------------------------------------------------------------


def _delete(ops: OpsHarness, *extra: str, environment: str = "dev") -> Outcome:
    return ops.run(
        "--env",
        environment,
        "--json",
        "user",
        "delete",
        "--user",
        str(USER),
        *extra,
        *TICKET,
    )


DELETE_ARGS = ("--confirm", str(USER), "--assignment-removed", "--yes")


def _script_counts(ops: OpsHarness, users_rows: int, members: int, overrides: int) -> None:
    for count in (users_rows, members, overrides):
        ops.database.answer({"count_1": count})


def _seed_deletable(ops: OpsHarness) -> None:
    _seed_session(ops, turns=3)
    _seed_session(ops, turns=3)
    ops.memories.put(memory_document(user_id=USER).to_item())
    ops.usage.put(usage_day_counter(user_id=USER).to_item())
    ops.usage.put(usage_month_counter(user_id=USER).to_item())
    _seed_session(ops, OTHER)
    ops.memories.put(memory_document(user_id=OTHER).to_item())
    ops.usage.put(usage_day_counter(user_id=OTHER).to_item())


def test_delete_removes_every_item_then_the_rows_with_its_audit_row(ops: OpsHarness) -> None:
    _seed_deletable(ops)
    sessions = {
        item["id"]
        for item in _items(ops)
        if item["type"] == "session" and item["userId"] == str(USER)
    }
    _script_counts(ops, 1, 2, 1)
    for rowcount in (1, 2, 1):
        ops.database.answer(rowcount=rowcount)
    _script_counts(ops, 0, 0, 0)
    in_flight = peak = 0
    original = ops.conversations.delete_item

    async def tracking(item: str, partition_key: Any, **kwargs: Any) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0)
            await original(item, partition_key, **kwargs)
        finally:
            in_flight -= 1

    ops.conversations.delete_item = tracking  # type: ignore[method-assign]
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert {item["userId"] for item in _items(ops)} == {str(OTHER)}
    assert {item["userId"] for item in _items(ops, "memories")} == {str(OTHER)}
    assert {item["userId"] for item in _items(ops, "usage")} == {str(OTHER)}
    deleted = [call.item_id for call in ops.conversations.calls_of("delete_item")]
    assert len(deleted) == 14
    assert set(deleted[-2:]) == sessions
    assert not sessions & set(deleted[:-2])
    assert peak == users.DELETE_BATCH
    assert all(
        call.kwargs["timeout"] == users.COSMOS_OPERATION_TIMEOUT_SECONDS
        for call in ops.conversations.calls_of("delete_item")
    )
    counts = {
        "conversations": 14,
        "memories": 1,
        "usage": 2,
        "users": 1,
        "projectMembers": 2,
        "userIndexOverrides": 1,
    }
    row = _audit(ops)
    assert row["action"] == "user_data.deleted"
    assert (row["target_type"], row["target_id"]) == ("user", str(USER))
    assert str(row["subject_object_id"]) == str(USER)
    assert row["correlation_id"] == outcome.correlation_id
    assert _metadata(row) == {"ticket": "DSR-0042", "counts": counts}
    report = outcome.json()
    assert report["result"] == "deleted"
    assert report["counts"] == counts
    assert set(report["remaining"].values()) == {0}
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("user_rows_count", "transient"),
        ("user_rows_delete", "none"),
        ("user_rows_count", "transient"),
    ]
    delete_statements = ops.database.sql("user_rows_delete")
    # The referencing rows first, then the user's row, and the audit row in the same unit.
    assert [statement.split(" WHERE ")[0].strip() for statement in delete_statements[:3]] == [
        "DELETE FROM app.user_index_overrides",
        "DELETE FROM app.project_members",
        "DELETE FROM app.users",
    ]
    assert delete_statements[3].startswith("INSERT INTO app.audit_events ")
    assert "Dry run in dev:" in outcome.stderr
    assert "conversations: 14 items (12 turns and traces, 2 sessions)" in outcome.stderr
    _no_content(ops, outcome)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--confirm", str(OTHER), "--assignment-removed"), "--confirm differs"),
        (("--confirm", str(USER)), "--assignment-removed is missing"),
    ],
)
def test_delete_refuses_before_any_sign_in(
    ops: OpsHarness, extra: tuple[str, ...], message: str
) -> None:
    outcome = _delete(ops, *extra)
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure
    assert ops.credentials_made == 0


def test_delete_without_confirm_is_a_usage_error(ops: OpsHarness) -> None:
    assert _delete(ops, "--assignment-removed").code == ExitCode.USAGE


def test_delete_declined_writes_nothing(ops: OpsHarness) -> None:
    _seed_deletable(ops)
    _script_counts(ops, 1, 0, 0)
    ops.answer("no\n")
    before = len(_items(ops))
    outcome = _delete(ops, "--confirm", str(USER), "--assignment-removed")
    assert outcome.code == ExitCode.USAGE
    assert len(_items(ops)) == before
    assert ops.conversations.calls_of("delete_item") == []
    assert ops.database.audit_rows == []


def test_delete_in_prod_needs_the_typed_environment_name(ops: OpsHarness) -> None:
    _seed_deletable(ops)
    _script_counts(ops, 0, 0, 0)
    ops.answer("yes\n", terminal=True)
    outcome = _delete(ops, *DELETE_ARGS, environment="prod")
    assert outcome.code == ExitCode.USAGE
    assert "Type the environment name to continue: " in outcome.stderr
    assert ops.conversations.calls_of("delete_item") == []


def test_delete_counts_an_item_already_gone_as_deleted(ops: OpsHarness) -> None:
    session_id, turn_ids = _seed_session(ops, turns=1)
    _script_counts(ops, 0, 0, 0)
    for _ in range(3):
        ops.database.answer(rowcount=0)
    _script_counts(ops, 0, 0, 0)
    key = ((str(USER), str(session_id)), str(turn_ids[0]))
    ops.conversations.before("delete_item", lambda: ops.conversations.items.pop(key))
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert _metadata(_audit(ops))["counts"]["conversations"] == 3


def test_delete_that_stops_part_way_exits_5_before_the_rows(ops: OpsHarness) -> None:
    session_id, _ = _seed_session(ops, turns=1)
    _script_counts(ops, 1, 0, 0)
    ops.conversations.fail("delete_item", status=503, item_id=str(session_id))
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.INCOMPLETE
    assert "stopped part-way" in outcome.failure
    assert ops.database.audit_rows == []
    assert [item["type"] for item in _items(ops)] == ["session"]
    assert ("user_rows_delete", "none") not in ops.database.units


def test_delete_refused_by_cosmos_before_any_delete_keeps_exit_3(ops: OpsHarness) -> None:
    _seed_session(ops, turns=1)
    _script_counts(ops, 0, 0, 0)
    ops.conversations.fail("delete_item", status=403, times=8)
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert len(_items(ops)) == 3


def test_delete_with_a_sql_failure_after_the_cosmos_deletes_exits_5(ops: OpsHarness) -> None:
    _seed_session(ops, turns=1)
    _script_counts(ops, 1, 0, 0)
    ops.database.unit_errors["user_rows_delete"] = SqlUnavailable("connection")
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.INCOMPLETE
    assert "SQL is unreachable" in outcome.failure
    assert _items(ops) == []


def test_delete_with_nothing_in_cosmos_keeps_the_sql_exit(ops: OpsHarness) -> None:
    _script_counts(ops, 1, 0, 0)
    ops.database.unit_errors["user_rows_delete"] = SqlUnavailable("connection")
    assert _delete(ops, *DELETE_ARGS).code == ExitCode.UNREACHABLE


def test_delete_with_something_left_at_the_recount_exits_5(ops: OpsHarness) -> None:
    _seed_session(ops, turns=1)
    _script_counts(ops, 1, 0, 0)
    for rowcount in (0, 0, 1):
        ops.database.answer(rowcount=rowcount)
    _script_counts(ops, 0, 1, 0)
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.INCOMPLETE
    assert outcome.json()["result"] == "incomplete"
    assert outcome.json()["remaining"]["projectMembers"] == 1
    assert _audit(ops)["action"] == "user_data.deleted"


def test_delete_whose_recount_fails_exits_5(ops: OpsHarness) -> None:
    _script_counts(ops, 0, 0, 0)
    for _ in range(3):
        ops.database.answer(rowcount=0)
    queries = 0
    original = ops.usage.query_items

    def second_fails(query: str, **kwargs: Any) -> Any:
        nonlocal queries
        queries += 1
        if queries == 2:
            ops.usage.fail("query_items", status=503)
        return original(query, **kwargs)

    ops.usage.query_items = second_fails  # type: ignore[method-assign]
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.INCOMPLETE
    assert "the recount failed (Cosmos DB answered 503)" in outcome.failure
    assert _audit(ops)["action"] == "user_data.deleted"


def test_delete_with_nothing_left_records_zero_counts(ops: OpsHarness) -> None:
    _script_counts(ops, 0, 0, 0)
    for _ in range(3):
        ops.database.answer(rowcount=0)
    _script_counts(ops, 0, 0, 0)
    outcome = _delete(ops, *DELETE_ARGS)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert set(_metadata(_audit(ops))["counts"].values()) == {0}


# --- verify ---------------------------------------------------------------------------------------


def _verify(ops: OpsHarness) -> Outcome:
    return ops.run("--env", "dev", "--json", "user", "verify", "--user", str(USER), *TICKET)


def test_verify_exits_0_when_every_count_is_zero(ops: OpsHarness) -> None:
    _seed_session(ops, OTHER)
    _script_counts(ops, 0, 0, 0)
    outcome = _verify(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["result"] == "verified"
    assert set(report["counts"].values()) == {0}
    assert ops.database.audit_rows == []
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("user_rows_count", "transient"),
    ]
    assert "Dry run" not in outcome.stderr


def test_verify_exits_5_when_anything_is_left(ops: OpsHarness) -> None:
    ops.usage.put(usage_day_counter(user_id=USER).to_item())
    _script_counts(ops, 0, 0, 0)
    outcome = _verify(ops)
    assert outcome.code == ExitCode.INCOMPLETE
    assert outcome.json()["result"] == "remaining"
    assert outcome.json()["counts"]["usage"] == 1
    assert ops.database.audit_rows == []


def test_verify_reads_without_the_caller_type_check(ops: OpsHarness) -> None:
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    _script_counts(ops, 0, 0, 0)
    assert _verify(ops).code == ExitCode.SUCCESS
    assert ops.az.calls == []


# --- redact-document ------------------------------------------------------------------------------


def _source_block(source_id: str, text: str, marker: str = MARKER) -> str:
    return f"[[BEGIN SOURCE {source_id} {marker}]]\n{text}\n[[END SOURCE {source_id} {marker}]]"


def _kept(rank: int, source_id: str | None, document: UUID) -> dict[str, Any]:
    return {
        "rank": rank,
        "sourceId": source_id,
        "chunkKey": f"{document}_{uuid4()}_{rank}",
        "documentId": str(document),
        "documentVersionId": str(uuid4()),
        "chunkSetId": str(uuid4()),
        "rerankerScore": 2.5,
        "outcome": "sent" if source_id else "flagged",
    }


def _retrieval(kept: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "searchIndexId": str(uuid4()),
        "physicalIndexId": str(uuid4()),
        "physicalIndexName": "docchat-idx-1",
        "indexSource": "project_default",
        "queryEmbeddingDeployment": "embed-query",
        "embeddingModel": "text-embedding-3-large",
        "embeddingDimensions": 1024,
        "candidateCount": len(kept),
        "keepCount": len(kept),
        "rerankerThreshold": 1.5,
        "minConfidentResults": 1,
        "rerankPartial": False,
        "broadenAttempted": False,
        "broadened": False,
        "broadenOutcome": "not_needed",
        "searchPassage": None,
        "firstPass": [],
        "broadenedPass": None,
        "kept": kept,
        "screening": None,
    }


def _model_input(messages: list[str]) -> dict[str, Any]:
    return {
        "rulesVersion": "1",
        "marker": MARKER,
        "deployment": "chat",
        "model": "gpt-5.6-sol",
        "maxCompletionTokens": 2000,
        "reasoningEffort": "low",
        "messages": [{"role": "user", "content": content} for content in messages],
        "promptTokens": 900,
        "droppedTurns": 0,
        "droppedSourceIds": [],
    }


def _seed_trace(
    ops: OpsHarness,
    user: UUID,
    kept: list[dict[str, Any]],
    model_input: dict[str, Any] | None,
    **overrides: Any,
) -> dict[str, Any]:
    trace = turn_trace_document(
        user_id=user, retrieval=_retrieval(kept), model_input=model_input, **overrides
    )
    return ops.conversations.put(trace.to_item())


CANARY = "D-CANARY-5b1e withdrawn clause text"


def _citing_d(ops: OpsHarness, user: UUID) -> dict[str, Any]:
    content = "\n\n".join(
        [
            "[[BEGIN QUESTION " + MARKER + "]]\nWhat applies?\n[[END QUESTION " + MARKER + "]]",
            _source_block("S1", f"{CANARY}\nsecond line {ops.sentinel}"),
            _source_block("S2", "text of document E"),
        ]
    )
    kept = [_kept(1, "S1", DOCUMENT), _kept(2, "S2", OTHER_DOCUMENT), _kept(3, None, DOCUMENT)]
    return _seed_trace(ops, user, kept, _model_input(["system rules", content]))


def _redact(ops: OpsHarness, *extra: str) -> Outcome:
    return ops.run(
        "--env",
        "dev",
        "--json",
        "user",
        "redact-document",
        "--document-id",
        str(DOCUMENT),
        "--yes",
        *extra,
        *TICKET,
    )


def _stored(ops: OpsHarness, item: dict[str, Any]) -> dict[str, Any]:
    stored = ops.conversations.get((item["userId"], item["sessionId"]), item["id"])
    assert stored is not None
    return stored


def test_redact_document_replaces_the_document_blocks_and_writes_its_audit_row(
    ops: OpsHarness,
) -> None:
    first = _citing_d(ops, USER)
    second = _citing_d(ops, OTHER)
    only_e = _seed_trace(
        ops,
        USER,
        [_kept(1, "S1", OTHER_DOCUMENT)],
        _model_input([_source_block("S1", "text of document E")]),
    )
    omitted = _seed_trace(ops, USER, [_kept(1, "S1", DOCUMENT)], {"omitted": "size_limit"})
    never_sent = _seed_trace(ops, OTHER, [_kept(1, None, DOCUMENT)], None)
    untouched = {item["id"]: _stored(ops, item) for item in (only_e, omitted, never_sent)}
    outcome = _redact(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    for item in (first, second):
        after = _stored(ops, item)
        assert CANARY not in json.dumps(after)
        assert after["redactedAt"] == "2026-10-30T12:00:00.000Z"
        content = after["modelInput"]["messages"][1]["content"]
        assert _source_block("S1", "[removed]") in content
        assert _source_block("S2", "text of document E") in content
        expected = {key: value for key, value in item.items() if key not in SYSTEM_FIELDS}
        expected["modelInput"] = after["modelInput"]
        expected["redactedAt"] = after["redactedAt"]
        assert {key: value for key, value in after.items() if key not in SYSTEM_FIELDS} == expected
        assert after["modelInput"]["messages"][0] == item["modelInput"]["messages"][0]
    for item_id, before in untouched.items():
        matching = [item for item in _items(ops) if item["id"] == item_id]
        assert matching == [before]
    replaces = ops.conversations.calls_of("replace_item")
    assert len(replaces) == 2
    for call in replaces:
        assert call.kwargs["etag"] in {first["_etag"], second["_etag"]}
        assert call.kwargs["match_condition"].name == "IfNotModified"
        assert call.kwargs["timeout"] == users.COSMOS_OPERATION_TIMEOUT_SECONDS
    (query,) = ops.conversations.calls_of("query_items")
    assert query.query == users.REDACTION_QUERY
    assert query.partition_key is None
    assert "enable_cross_partition_query" not in query.kwargs
    assert query.kwargs["max_item_count"] == 100
    parameters = {item["name"]: item["value"] for item in query.kwargs["parameters"]}
    assert parameters == {"@type": "turn_trace", "@documentId": str(DOCUMENT)}
    row = _audit(ops)
    assert row["action"] == "document.traces_redacted"
    assert row["subject_object_id"] is None
    assert (row["target_type"], row["target_id"]) == ("document", str(DOCUMENT))
    assert row["correlation_id"] == outcome.correlation_id
    assert _metadata(row) == {
        "documentId": str(DOCUMENT),
        "traceCount": 2,
        "ticket": "DSR-0042",
    }
    report = outcome.json()
    assert (report["tracesFound"], report["traceCount"]) == (4, 2)
    assert report["documentId"] == str(DOCUMENT)
    assert report["sessions"] == sorted(
        (
            {"userId": item["userId"], "sessionId": item["sessionId"]}
            for item in (first, second, omitted, never_sent)
        ),
        key=lambda pair: (pair["userId"], pair["sessionId"]),
    )
    assert "matching turn traces: 4" in outcome.stderr
    _no_content(ops, outcome)
    assert CANARY not in outcome.stdout


def test_redact_document_twice_reports_trace_count_0(ops: OpsHarness) -> None:
    _citing_d(ops, USER)
    assert _redact(ops).code == ExitCode.SUCCESS
    ops.database.executed.clear()
    outcome = _redact(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["traceCount"] == 0
    assert _metadata(_audit(ops))["traceCount"] == 0


def test_redact_document_rereads_after_a_412(ops: OpsHarness) -> None:
    item = _citing_d(ops, USER)
    key = (item["userId"], item["sessionId"])
    ops.conversations.before(
        "replace_item", lambda: ops.conversations.touch(key, item["id"], contentFilterDegraded=True)
    )
    outcome = _redact(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    after = _stored(ops, item)
    assert after["contentFilterDegraded"] is True
    assert CANARY not in json.dumps(after)
    assert len(ops.conversations.calls_of("read_item")) == 2


def test_redact_document_with_a_conflict_left_after_3_attempts_exits_6(ops: OpsHarness) -> None:
    first = _citing_d(ops, USER)
    second = _citing_d(ops, OTHER)
    hits = sorted((first, second), key=lambda item: (item["userId"], item["sessionId"]))
    stubborn = hits[1]
    key = (stubborn["userId"], stubborn["sessionId"])
    ops.conversations.before(
        "replace_item",
        lambda: ops.conversations.touch(key, stubborn["id"], contentFilterDegraded=True),
        item_id=stubborn["id"],
        times=3,
    )
    outcome = _redact(ops)
    assert outcome.code == ExitCode.CONFLICT
    assert ops.database.audit_rows == []
    # Changed before it, stays changed.
    changed = [item for item in (first, second) if item["id"] != stubborn["id"]]
    assert CANARY not in json.dumps(_stored(ops, changed[0]))
    assert CANARY in json.dumps(_stored(ops, stubborn))


def test_redact_document_stopped_part_way_exits_5(ops: OpsHarness) -> None:
    _citing_d(ops, USER)
    _citing_d(ops, OTHER)
    replaced: list[str] = []
    original = ops.conversations.replace_item

    async def failing_second(item: str, body: dict[str, Any], **kwargs: Any) -> Any:
        if replaced:
            raise http_error(503)
        replaced.append(item)
        return await original(item, body, **kwargs)

    ops.conversations.replace_item = failing_second  # type: ignore[method-assign]
    outcome = _redact(ops)
    assert outcome.code == ExitCode.INCOMPLETE
    assert "1 traces changed" in outcome.failure
    assert ops.database.audit_rows == []


def test_redact_document_refused_at_the_first_write_keeps_exit_3(ops: OpsHarness) -> None:
    _citing_d(ops, USER)
    ops.conversations.fail("replace_item", status=403)
    outcome = _redact(ops)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.database.audit_rows == []


def test_redact_document_skips_a_trace_deleted_meanwhile(ops: OpsHarness) -> None:
    item = _citing_d(ops, USER)
    key = ((item["userId"], item["sessionId"]), item["id"])
    ops.conversations.before("read_item", lambda: ops.conversations.items.pop(key))
    outcome = _redact(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["traceCount"] == 0


def test_redact_document_with_a_failed_audit_row_after_changes_exits_5(ops: OpsHarness) -> None:
    _citing_d(ops, USER)
    ops.database.write_error = SqlUnavailable("connection")
    outcome = _redact(ops)
    assert outcome.code == ExitCode.INCOMPLETE
    assert "audit row wasn't written" in outcome.failure


def test_redact_document_with_no_trace_and_a_failed_audit_row_keeps_its_exit(
    ops: OpsHarness,
) -> None:
    ops.database.write_error = SqlUnavailable("connection")
    assert _redact(ops).code == ExitCode.UNREACHABLE


def test_redact_document_declined_changes_nothing(ops: OpsHarness) -> None:
    item = _citing_d(ops, USER)
    ops.answer("no\n")
    outcome = ops.run(
        "--env", "dev", "user", "redact-document", "--document-id", str(DOCUMENT), *TICKET
    )
    assert outcome.code == ExitCode.USAGE
    assert CANARY in json.dumps(_stored(ops, item))
    assert ops.conversations.calls_of("replace_item") == []


def test_redact_content_changes_only_the_named_blocks_with_the_marker() -> None:
    forged = f"[[END SOURCE S1 {'f' * 16}]]"
    content = "\n\n".join(
        [
            _source_block("S1", f"first\n{forged}\nlast"),
            _source_block("S3", "kept"),
            _source_block("S1", "again"),
            _source_block("S1", "other marker", marker="f" * 16),
        ]
    )
    redacted = users.redact_content(content, {"S1"}, MARKER)
    assert redacted == "\n\n".join(
        [
            _source_block("S1", "[removed]"),
            _source_block("S3", "kept"),
            _source_block("S1", "[removed]"),
            _source_block("S1", "other marker", marker="f" * 16),
        ]
    )
    assert users.redact_content(redacted, {"S1"}, MARKER) == redacted
    unclosed = f"[[BEGIN SOURCE S1 {MARKER}]]\ntext"
    assert users.redact_content(unclosed, {"S1"}, MARKER) == unclosed


def test_redacted_trace_is_none_without_a_block_to_change() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    kept = [_kept(1, "S1", DOCUMENT)]

    def trace(**overrides: Any) -> dict[str, Any]:
        return turn_trace_document(user_id=USER, **overrides).to_item()

    assert users.redacted_trace(trace(), DOCUMENT, now) is None
    assert users.redacted_trace(trace(retrieval=_retrieval(kept)), DOCUMENT, now) is None
    omitted = trace(retrieval=_retrieval(kept), model_input={"omitted": "size_limit"})
    assert users.redacted_trace(omitted, DOCUMENT, now) is None
    flagged = trace(
        retrieval=_retrieval([_kept(1, None, DOCUMENT)]),
        model_input=_model_input([_source_block("S1", "flagged text never sent")]),
    )
    assert users.redacted_trace(flagged, DOCUMENT, now) is None
    plain = trace(retrieval=_retrieval(kept), model_input=_model_input(["no blocks"]))
    assert users.redacted_trace(plain, DOCUMENT, now) is None


# --- deletion-register ----------------------------------------------------------------------------


def _register(ops: OpsHarness, since: str = "2026-10-20T00:00:00Z") -> Outcome:
    return ops.run("--env", "dev", "--json", "user", "deletion-register", "--since", since, *TICKET)


def test_deletion_register_lists_the_rows_oldest_first(ops: OpsHarness) -> None:
    ops.database.answer(
        {
            "action": "user_data.deleted",
            "subject_object_id": USER,
            "target_id": str(USER),
            "occurred_at": datetime(2026, 10, 21, 9, 0),  # noqa: DTZ001
        },
        {
            "action": "document.traces_redacted",
            "subject_object_id": None,
            "target_id": str(DOCUMENT),
            "occurred_at": datetime(2026, 10, 22, 9, 0),  # noqa: DTZ001
        },
    )
    outcome = _register(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["since"] == "2026-10-20T00:00:00.000Z"
    assert report["register"] == [
        {
            "action": "user_data.deleted",
            "subject_object_id": str(USER),
            "target_id": str(USER),
            "occurred_at": "2026-10-21T09:00:00.000Z",
        },
        {
            "action": "document.traces_redacted",
            "subject_object_id": None,
            "target_id": str(DOCUMENT),
            "occurred_at": "2026-10-22T09:00:00.000Z",
        },
    ]
    (statement,) = ops.database.sql("user_deletion_register")
    assert "app.audit_events.action IN (N'user_data.deleted', N'document.traces_redacted')" in (
        statement
    )
    assert "app.audit_events.occurred_at >= :restore_point" in statement
    assert "ORDER BY app.audit_events.occurred_at, app.audit_events.id" in statement
    (_, parameters) = ops.database.executed[-1]
    assert parameters == {"restore_point": datetime(2026, 10, 20)}  # noqa: DTZ001
    assert ops.database.audit_rows == []
    assert ops.cosmos_targets == []


def test_deletion_register_prints_a_table_without_json(ops: OpsHarness) -> None:
    ops.database.answer()
    outcome = ops.run(
        "--env", "dev", "user", "deletion-register", "--since", "2026-10-20T00:00:00Z", *TICKET
    )
    assert outcome.code == ExitCode.SUCCESS
    assert "action  subject_object_id  target_id  occurred_at" in outcome.stdout


@pytest.mark.parametrize(
    "since", ["2026-10-20", "2026-10-20T00:00:00+00:00", "2026-13-01T00:00:00Z", "yesterday"]
)
def test_deletion_register_refuses_a_malformed_time(ops: OpsHarness, since: str) -> None:
    outcome = _register(ops, since)
    assert outcome.code == ExitCode.USAGE
    assert ops.credentials_made == 0


def test_deletion_register_refuses_a_future_time(ops: OpsHarness) -> None:
    outcome = _register(ops, "2026-10-30T12:00:01Z")
    assert outcome.code == ExitCode.USAGE
    assert "in the future" in outcome.failure
    assert ops.credentials_made == 0


# --- audit-events ---------------------------------------------------------------------------------


def _audit_events(ops: OpsHarness, since: str = "2026-10-01T00:00:00Z") -> Outcome:
    return ops.run(
        "--env",
        "dev",
        "--json",
        "user",
        "audit-events",
        "--actor",
        str(OTHER),
        "--since",
        since,
        *TICKET,
    )


def test_audit_events_lists_a_persons_rows_newest_first(ops: OpsHarness) -> None:
    ops.database.answer(
        {
            "occurred_at": datetime(2026, 10, 29, 9, 0),  # noqa: DTZ001
            "action": "project_member.added",
            "target_type": "project_member",
            "target_id": str(USER),
            "project_id": PROJECT,
            "metadata_json": '{"grantSource":"admin"}',
        }
    )
    outcome = _audit_events(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["events"] == [
        {
            "occurred_at": "2026-10-29T09:00:00.000Z",
            "action": "project_member.added",
            "target_type": "project_member",
            "target_id": str(USER),
            "project_id": str(PROJECT),
            "metadata_json": '{"grantSource":"admin"}',
        }
    ]
    (statement,) = ops.database.sql("user_audit_events")
    assert (
        "WHERE app.audit_events.actor_object_id = :actor "
        "AND app.audit_events.occurred_at >= :since" in statement
    )
    assert "ORDER BY app.audit_events.occurred_at DESC, app.audit_events.id DESC" in statement
    (_, parameters) = ops.database.executed[-1]
    assert parameters == {"actor": OTHER, "since": datetime(2026, 10, 1)}  # noqa: DTZ001
    assert ops.database.audit_rows == []


@pytest.mark.parametrize(
    ("since", "message"),
    [
        ("2025-09-24T11:59:59Z", "more than 400 days back"),
        ("2026-11-01T00:00:00Z", "in the future"),
        ("2026-10-01 00:00:00", "yyyy-MM-ddTHH:mm:ssZ"),
    ],
)
def test_audit_events_refuses_a_time_outside_its_window(
    ops: OpsHarness, since: str, message: str
) -> None:
    outcome = _audit_events(ops, since)
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.stderr
    assert ops.credentials_made == 0


def test_audit_events_accepts_exactly_400_days_back(ops: OpsHarness) -> None:
    ops.database.answer()
    since = (ops.now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _audit_events(ops, since).code == ExitCode.SUCCESS


# --- Helpers --------------------------------------------------------------------------------------


def test_utc_time_parses_the_one_form() -> None:
    assert users.utc_time("2026-10-20T08:30:00Z") == datetime(2026, 10, 20, 8, 30, tzinfo=UTC)
    with pytest.raises(argparse.ArgumentTypeError):
        users.utc_time("2026-02-30T00:00:00Z")


def test_sql_value_renders_every_column_type() -> None:
    assert users.sql_value(USER) == str(USER)
    assert users.sql_value(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05.000Z"  # noqa: DTZ001
    assert users.sql_value(None) is None
    assert users.sql_value(True) is True
    assert users.sql_value(7) == 7
    assert users.sql_value("ops.key") == "ops.key"
    assert users.sql_value(b"AB") == "b'ab'"


def test_cosmos_failure_maps_a_status_without_a_rule_through_classify() -> None:
    assert users.cosmos_failure(http_error(404)).exit_code == ExitCode.NOT_FOUND
    assert users.cosmos_failure(http_error(429)).exit_code == ExitCode.UNREACHABLE
    assert users.cosmos_failure(http_error(401)).exit_code == ExitCode.ACCESS_DENIED


def test_output_directory_removes_what_it_created(tmp_path: Path) -> None:
    out = tmp_path / "out"
    directory = users.OutputDirectory.open(out)
    digest = directory.write("internal/a.json", b"{}\n")
    assert digest == hashlib.sha256(b"{}\n").hexdigest()
    streamed = directory.file("internal/b.jsonl")
    streamed.write(b"line\n")
    directory.close()
    directory.close()
    assert (out / "internal/b.jsonl").read_bytes() == b"line\n"
    directory.remove()
    assert not out.exists()


def test_output_directory_refuses_an_existing_file(tmp_path: Path) -> None:
    directory = users.OutputDirectory.open(tmp_path / "out")
    directory.write("a.json", b"{}")
    with pytest.raises(UsageError, match="can't be created"):
        directory.write("a.json", b"{}")


def test_output_directory_taken_while_it_filled_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "x").write_text("x", encoding="utf-8")
    with pytest.raises(UsageError, match="already holds files"):
        users.OutputDirectory.open(out)


def test_output_directory_that_cant_be_made_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="can't be created"):
        users.OutputDirectory.open(tmp_path / "missing" / "out")


def test_subject_sessions_and_memory_handle_absent_documents() -> None:
    assert users.subject_memory(None) is None
    assert users.subject_profile(None) is None
    exported = users.CosmosExport()
    assert users.subject_sessions(exported, {}) == []
    assert users.subject_usage([]) == {"days": [], "months": []}
    turn = users.subject_turn({"seq": 1, "answer": None, "citations": None})
    assert (turn["answer"], turn["citations"], turn["followups"]) == (None, [], [])


def test_command_version_names_the_distribution() -> None:
    assert users.command_version() == "0.0.0"


def test_cosmos_stores_builds_the_three_containers() -> None:
    stores = users.cosmos_stores(
        "https://cosmos-docchat-dev-abc123.documents.azure.com:443/",
        "docchat",
        AzureCliCredential(),
    )
    for container, name in (
        (stores.conversations, "conversations"),
        (stores.memories, "memories"),
        (stores.usage, "usage"),
    ):
        assert getattr(container, "id", None) == name

    async def close() -> None:
        await stores.close()

    asyncio.run(close())


def test_user_session_without_cosmos_has_no_containers() -> None:
    session = users.UserSession(
        credential=None,  # type: ignore[arg-type]
        database=None,  # type: ignore[arg-type]
        actor=str(USER),
        stores=None,
    )
    with pytest.raises(RuntimeError, match="no Cosmos DB client"):
        session.containers()
