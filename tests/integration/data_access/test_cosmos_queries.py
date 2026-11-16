"""T-095, the Cosmos query integration tests (`80-data-model.md` section 3.11 and acceptance
criterion 26).

Each query of section 3.11 runs, with the very text its owner sends (the conversation store, the
after-response work and `docchat-ops user`), against the session's emulator database, whose
containers carry the production partition keys and indexing policies, and returns the expected
documents in the expected order. Every test lays out its documents under fresh user IDs, so no
other test's items are in its partitions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from azure.cosmos.aio import ContainerProxy, DatabaseProxy

from docchat_api.conversations.post_turn import RECHECK_QUERY, RECHECK_STATES
from docchat_api.conversations.store import (
    RECENT_TURNS_QUERY,
    SESSION_TURNS_QUERY,
    SESSIONS_AFTER_QUERY,
    SESSIONS_QUERY,
)
from docchat_api.conversations.summary import SUMMARY_LOAD_QUERY
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.conversations import (
    ExtractionState,
    MemoryExtractionState,
    TurnStatus,
)
from docchat_ops.commands.users import (
    CONVERSATION_KEYS_QUERY,
    ITEM_IDS_QUERY,
    REDACTION_QUERY,
    TRACES_BY_TURN_QUERY,
    TURNS_BY_ID_QUERY,
    TURNS_QUERY,
    USER_ITEMS_QUERY,
)
from docchat_testing.builders.cosmos import (
    cosmos_now,
    memory_document,
    session_document,
    turn_document,
    turn_trace_document,
    usage_day_counter,
    usage_month_counter,
)

type Parameters = Mapping[str, Any]


async def run(
    container: ContainerProxy,
    query: str,
    parameters: Parameters,
    *,
    partition_key: Any = None,
    cross_partition: bool = False,
) -> list[dict[str, Any]]:
    """Every row of the query, pages joined, with `@name` parameters. The async client queries
    across partitions when no partition key is given; it has no `enable_cross_partition_query`
    keyword."""
    options: dict[str, Any] = {}
    if not cross_partition:
        options["partition_key"] = partition_key
    pager = container.query_items(
        query=query,
        parameters=[{"name": f"@{name}", "value": value} for name, value in parameters.items()],
        **options,
    )
    return [row async for row in pager]


def conversations(database: DatabaseProxy) -> ContainerProxy:
    return database.get_container_client(CONVERSATIONS.name)


async def put(container: ContainerProxy, *documents: Any) -> None:
    for document in documents:
        await container.upsert_item(document.to_item())


async def turns_of(
    database: DatabaseProxy,
    user: UUID,
    session: UUID,
    statuses: Sequence[TurnStatus],
    **fields: Any,
) -> list[Any]:
    """A session of `user` holding one turn per status, `seq` 1 onwards."""
    made = [
        turn_document(user_id=user, session_id=session, seq=seq, status=status, **fields)
        for seq, status in enumerate(statuses, start=1)
    ]
    await put(
        conversations(database),
        session_document(user_id=user, session_id=session, turn_count=len(made)),
        *made,
    )
    return made


# --- The application's queries ------------------------------------------------------------------


async def test_t095_recent_completed_turns_newest_first(cosmos_database: DatabaseProxy) -> None:
    user, session = uuid4(), uuid4()
    statuses = [TurnStatus.COMPLETED, TurnStatus.COMPLETED, TurnStatus.FAILED]
    statuses += [TurnStatus.COMPLETED, TurnStatus.CANCELLED, TurnStatus.COMPLETED]
    await turns_of(cosmos_database, user, session, statuses)

    rows = await run(
        conversations(cosmos_database),
        RECENT_TURNS_QUERY,
        {
            "userId": str(user),
            "sessionId": str(session),
            "type": "turn",
            "status": "completed",
            "covered": 1,
        },
        partition_key=[str(user), str(session)],
    )

    assert [row["seq"] for row in rows] == [6, 4, 2]


async def test_t095_the_session_list_by_last_turn_then_session_id(
    cosmos_database: DatabaseProxy,
) -> None:
    user, shown, other, hidden = uuid4(), uuid4(), uuid4(), uuid4()
    now = cosmos_now()
    tied = now - timedelta(minutes=10)
    sessions = [
        session_document(user_id=user, project_id=shown, last_turn_at=now - timedelta(minutes=5)),
        session_document(user_id=user, project_id=other, last_turn_at=tied),
        session_document(user_id=user, project_id=shown, last_turn_at=tied),
        session_document(user_id=user, project_id=hidden, last_turn_at=now),
        session_document(user_id=user, project_id=shown, last_turn_at=now - timedelta(hours=1)),
    ]
    await put(conversations(cosmos_database), *sessions)
    parameters: dict[str, Any] = {
        "take": 3,
        "userId": str(user),
        "type": "session",
        "projectIds": [str(shown), str(other)],
    }

    first = await run(
        conversations(cosmos_database), SESSIONS_QUERY, parameters, partition_key=[str(user)]
    )
    last = first[-1]
    after = await run(
        conversations(cosmos_database),
        SESSIONS_AFTER_QUERY,
        {**parameters, "t": last["lastTurnAt"], "s": last["sessionId"]},
        partition_key=[str(user)],
    )

    by_tie = sorted((sessions[1], sessions[2]), key=lambda s: str(s.session_id), reverse=True)
    expected = [sessions[0], *by_tie, sessions[4]]
    listed = [row["sessionId"] for row in [*first, *after]]
    assert listed == [str(session.session_id) for session in expected]


async def test_t095_session_turns_before_a_cursor_newest_first(
    cosmos_database: DatabaseProxy,
) -> None:
    user, session = uuid4(), uuid4()
    await turns_of(cosmos_database, user, session, [TurnStatus.COMPLETED] * 5)

    rows = await run(
        conversations(cosmos_database),
        SESSION_TURNS_QUERY,
        {"take": 2, "userId": str(user), "sessionId": str(session), "type": "turn", "before": 5},
        partition_key=[str(user), str(session)],
    )

    assert [row["seq"] for row in rows] == [4, 3]


async def test_t095_the_pending_extraction_recheck_oldest_first(
    cosmos_database: DatabaseProxy,
) -> None:
    user = uuid4()
    now = cosmos_now()
    states = [
        (ExtractionState.PENDING, now - timedelta(minutes=1)),
        (ExtractionState.DONE, now - timedelta(minutes=9)),
        (ExtractionState.RUNNING, now - timedelta(minutes=5)),
        (ExtractionState.PENDING, now - timedelta(minutes=7)),
    ]
    made = [
        turn_document(
            user_id=user,
            session_id=uuid4(),
            ended_at=ended,
            memory_extraction=MemoryExtractionState(state=state),
        )
        for state, ended in states
    ]
    await put(conversations(cosmos_database), *made)

    rows = await run(
        conversations(cosmos_database),
        RECHECK_QUERY,
        {"userId": str(user), "type": "turn", "states": list(RECHECK_STATES)},
        partition_key=[str(user)],
    )

    assert [row["id"] for row in rows] == [str(made[3].id), str(made[2].id), str(made[0].id)]
    assert [row["state"] for row in rows] == ["pending", "running", "pending"]


async def test_t095_the_summary_input_oldest_first(cosmos_database: DatabaseProxy) -> None:
    user, session = uuid4(), uuid4()
    statuses = [TurnStatus.COMPLETED, TurnStatus.COMPLETED, TurnStatus.FAILED]
    statuses += [TurnStatus.COMPLETED, TurnStatus.COMPLETED]
    await turns_of(cosmos_database, user, session, statuses)

    rows = await run(
        conversations(cosmos_database),
        SUMMARY_LOAD_QUERY,
        {
            "userId": str(user),
            "sessionId": str(session),
            "type": "turn",
            "status": "completed",
            "covered": 1,
        },
        partition_key=[str(user), str(session)],
    )

    assert [row["seq"] for row in rows] == [2, 4, 5]


# --- The operator commands' queries ---------------------------------------------------------------


async def test_t095_the_operator_reads_a_conversation(cosmos_database: DatabaseProxy) -> None:
    user, session = uuid4(), uuid4()
    made = await turns_of(cosmos_database, user, session, [TurnStatus.COMPLETED] * 3)
    traces = [turn_trace_document(user_id=user, session_id=session, turn=turn.id) for turn in made]
    await put(conversations(cosmos_database), *traces)
    base = {"userId": str(user), "sessionId": str(session)}
    partition = [str(user), str(session)]
    chosen = [str(made[0].id), str(made[2].id)]

    every = await run(
        conversations(cosmos_database),
        TURNS_QUERY,
        {**base, "type": "turn"},
        partition_key=partition,
    )
    some = await run(
        conversations(cosmos_database),
        TURNS_BY_ID_QUERY,
        {**base, "type": "turn", "turnIds": chosen},
        partition_key=partition,
    )
    their_traces = await run(
        conversations(cosmos_database),
        TRACES_BY_TURN_QUERY,
        {**base, "type": "turn_trace", "turnIds": chosen},
        partition_key=partition,
    )

    assert sorted(row["id"] for row in every) == sorted(str(turn.id) for turn in made)
    assert sorted(row["id"] for row in some) == sorted(chosen)
    assert sorted(row["turnId"] for row in their_traces) == sorted(chosen)


async def test_t095_export_and_delete_read_every_item_of_the_users_partitions(
    cosmos_database: DatabaseProxy,
) -> None:
    user, someone_else = uuid4(), uuid4()
    first, second = uuid4(), uuid4()
    mine = [
        *await turns_of(cosmos_database, user, first, [TurnStatus.COMPLETED]),
        *await turns_of(cosmos_database, user, second, [TurnStatus.FAILED]),
    ]
    await turns_of(cosmos_database, someone_else, uuid4(), [TurnStatus.COMPLETED])
    await put(cosmos_database.get_container_client(MEMORIES.name), memory_document(user_id=user))
    usage = cosmos_database.get_container_client(USAGE.name)
    await put(usage, usage_day_counter(user_id=user), usage_month_counter(user_id=user))
    await put(usage, usage_day_counter(user_id=someone_else))
    parameters = {"userId": str(user)}

    items = await run(
        conversations(cosmos_database), USER_ITEMS_QUERY, parameters, partition_key=[str(user)]
    )
    keys = await run(
        conversations(cosmos_database),
        CONVERSATION_KEYS_QUERY,
        parameters,
        partition_key=[str(user)],
    )
    memory = await run(
        cosmos_database.get_container_client(MEMORIES.name),
        USER_ITEMS_QUERY,
        parameters,
        partition_key=str(user),
    )
    counters = await run(usage, ITEM_IDS_QUERY, parameters, partition_key=str(user))

    expected = {str(turn.id) for turn in mine} | {str(first), str(second)}
    assert {row["id"] for row in items} == expected
    assert {(row["id"], row["type"]) for row in keys} == {(row["id"], row["type"]) for row in items}
    assert [row["id"] for row in memory] == [str(user)]
    assert len(counters) == 2


async def test_t095_legal_removal_finds_every_trace_that_kept_a_document(
    cosmos_database: DatabaseProxy,
) -> None:
    withdrawn, other_document = uuid4(), uuid4()
    holders = [uuid4(), uuid4()]

    def kept(document: UUID, rank: int) -> dict[str, Any]:
        return {
            "rank": rank,
            "sourceId": f"S{rank}",
            "chunkKey": f"{document}_{uuid4()}_{rank}",
            "documentId": str(document),
            "documentVersionId": str(uuid4()),
            "chunkSetId": str(uuid4()),
            "rerankerScore": 2.5,
            "outcome": "sent",
        }

    def trace(user: UUID, *documents: UUID) -> Any:
        retrieval = {
            "searchIndexId": str(uuid4()),
            "physicalIndexId": str(uuid4()),
            "physicalIndexName": "docchat-idx-1",
            "indexSource": "project_default",
            "queryEmbeddingDeployment": "embed-query",
            "embeddingModel": "text-embedding-3-large",
            "embeddingDimensions": 1024,
            "candidateCount": len(documents),
            "keepCount": len(documents),
            "rerankerThreshold": 1.5,
            "minConfidentResults": 1,
            "rerankPartial": False,
            "broadenAttempted": False,
            "broadened": False,
            "broadenOutcome": "not_needed",
            "searchPassage": None,
            "firstPass": [],
            "broadenedPass": None,
            "kept": [kept(document, rank) for rank, document in enumerate(documents, 1)],
            "screening": None,
        }
        return turn_trace_document(user_id=user, retrieval=retrieval)

    found = [trace(holders[0], other_document, withdrawn), trace(holders[1], withdrawn)]
    await put(conversations(cosmos_database), *found, trace(uuid4(), other_document))

    rows = await run(
        conversations(cosmos_database),
        REDACTION_QUERY,
        {"type": "turn_trace", "documentId": str(withdrawn)},
        cross_partition=True,
    )

    assert {row["id"] for row in rows} == {str(document.id) for document in found}
    assert {row["userId"] for row in rows} == {str(holder) for holder in holders}
