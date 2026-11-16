from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.conversations import turn_id
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.identities import make_principals
from docchat_testing.isolation import (
    DATA_PATHS,
    REQUIRED_NEGATIVE_PATHS,
    Entry,
    IsolationContext,
    completeness_problems,
    run_entry,
)


def _entry(name: str, **changes: Any) -> Entry:
    return replace(DATA_PATHS[0], name=name, **changes)


def _complete() -> list[Entry]:
    return [
        _entry(f"ia.{path}.case", data_path=path, negative=True) for path in REQUIRED_NEGATIVE_PATHS
    ]


def test_t003_a_complete_registry_has_no_problem() -> None:
    assert completeness_problems(_complete()) == []


def test_t003_a_repeated_name_is_a_problem() -> None:
    entries = [*_complete(), _entry("ia.chat.case", data_path="chat")]

    assert completeness_problems(entries) == ["ia.chat.case: registered 2 times"]


def test_t003_a_path_without_a_negative_entry_is_a_problem() -> None:
    entries = [
        replace(entry, negative=False) if entry.data_path == "citations" else entry
        for entry in _complete()
    ]

    assert completeness_problems(entries) == ["citations: no negative entry"]


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"actor": "mallory"}, "ia.x.y: actor 'mallory' isn't a shared principal"),
        ({"data_path": "billing"}, "ia.x.y: no data path"),
        ({"owner": "ops"}, "ia.x.y: owner 'ops' isn't a component"),
    ],
)
def test_t003_an_entry_outside_the_lists_is_a_problem(
    changes: dict[str, Any], problem: str
) -> None:
    assert problem in completeness_problems([*_complete(), _entry("ia.x.y", **changes)])


def test_t003_a_name_must_be_dotted() -> None:
    assert "Bad Name: not a dotted <owner>.<data path>.<case> name" in completeness_problems(
        [*_complete(), _entry("Bad Name")]
    )


def test_the_identity_and_access_entries_are_registered_once() -> None:
    names = [entry.name for entry in DATA_PATHS]

    assert len(names) == len(set(names))
    assert {
        "ia.projects.member_lists_own",
        "ia.projects.admin_lists_all",
        "ia.projects.archived_hidden",
        "ia.members.user_token_refused",
        "ia.overrides.unmapped_refused",
        "ia.admin_routes.user_token_refused",
    } <= set(names)
    # The chat pipeline's entries are named `<data path>.<case>` (84-testing.md section 4.1).
    assert all(
        entry.name.startswith(
            f"{entry.data_path}." if entry.owner == "chat" else f"{entry.owner}.{entry.data_path}."
        )
        for entry in DATA_PATHS
    )
    assert [entry.name for entry in DATA_PATHS if not entry.negative] == [
        "ia.projects.admin_lists_all",
        "chat.retrieval_scope",
        "retrieval.shared_index",
    ]


async def test_run_entry_runs_setup_action_and_check_in_order() -> None:
    steps: list[str] = []

    async def action(context: IsolationContext) -> None:
        steps.append("action")

    entry = _entry(
        "ia.x.y",
        setup=lambda context: steps.append("setup"),
        action=action,
        expected=lambda context: steps.append("expected"),
    )
    async with httpx.AsyncClient() as client:
        context = IsolationContext(
            client=client,
            admin=cast("Engine", None),
            principals=make_principals(),
            document={"paths": {}},
        )
        await run_entry(entry, context)

    assert steps == ["setup", "action", "expected"]


# --- Ingestion entries ------------------------------------------------------------------------

ENTRIES = {entry.name: entry for entry in DATA_PATHS}
X, Y = uuid4(), uuid4()
DX, DY, VX, VY, RX, RY = (uuid4() for _ in range(6))
INGESTION_DOCUMENT: dict[str, Any] = {
    "paths": {
        "/v1/admin/indexes/{indexId}": {"get": {}},
        "/v1/admin/indexes/{indexId}/documents/{documentId}": {"get": {}, "delete": {}},
        "/v1/admin/indexes/{indexId}/uploads/{versionId}/content": {"put": {}},
        "/v1/admin/ingestion-runs/{runId}": {"get": {}},
    }
}


def _context(client: Any = None, document: dict[str, Any] | None = None) -> IsolationContext:
    context = IsolationContext(
        client=cast("httpx.AsyncClient", client),
        admin=cast("Engine", None),
        principals=make_principals(),
        document=document or {"paths": {}},
    )
    context.ids |= {"X": X, "Y": Y, "DX": DX, "DY": DY, "VX": VX, "VY": VY, "RX": RX, "RY": RY}
    return context


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _version(version_id: UUID) -> dict[str, str]:
    return {"versionId": str(version_id)}


def test_the_ingestion_entries_hold_the_documents_and_status_refusals() -> None:
    assert {
        "ing.documents.non_admin_refused",
        "ing.documents.other_index_absent",
        "ing.ingestion_status.other_index_absent",
    } <= set(ENTRIES)
    problems = completeness_problems(DATA_PATHS)
    assert "documents: no negative entry" not in problems
    assert "ingestion_status: no negative entry" not in problems


async def test_a_user_token_reaches_every_ingestion_route_with_real_and_random_ids() -> None:
    seen: list[tuple[str, str]] = []

    async def refuse(request: Request) -> Response:
        seen.append((request.method, request.url.path))
        return JSONResponse({"code": "admin_required"}, status_code=403)

    app = Starlette(routes=[Route("/{rest:path}", refuse, methods=["GET", "PUT", "DELETE"])])
    entry = ENTRIES["ing.documents.non_admin_refused"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        context = _context(client, INGESTION_DOCUMENT)
        await entry.action(context)
        entry.expected(context)

    assert len(seen) == 8
    assert ("GET", f"/v1/admin/indexes/{X}/documents/{DX}") in seen
    assert ("PUT", f"/v1/admin/indexes/{X}/uploads/{VX}/content") in seen
    assert ("GET", f"/v1/admin/ingestion-runs/{RX}") in seen
    assert f"/v1/admin/indexes/{X}" not in {path for _, path in seen}


def test_an_ingestion_route_answering_anything_but_admin_required_fails() -> None:
    entry = ENTRIES["ing.documents.non_admin_refused"]
    context = _context(document=INGESTION_DOCUMENT)
    context.responses = [_json(403, {"code": "admin_required"})] * 7
    context.responses.append(_json(404, {"code": "document_not_found"}))

    with pytest.raises(AssertionError):
        entry.expected(context)


def _documents_of_x(*, listed: list[UUID], versions: list[UUID]) -> list[httpx.Response]:
    items = [
        {"documentId": str(document), "currentVersion": _version(version), "latestVersion": None}
        for document, version in zip(listed, versions, strict=True)
    ]
    own = {"currentVersion": _version(VX), "latestVersion": None, "versions": [_version(VX)]}
    return [
        _json(200, {"items": items, "nextCursor": None}),
        _json(200, own),
        _json(404, {"code": "document_not_found"}),
        _json(404, {"code": "document_not_found"}),
        _json(404, {"code": "version_not_found"}),
    ]


def test_index_xs_documents_hold_nothing_of_index_y() -> None:
    entry = ENTRIES["ing.documents.other_index_absent"]
    context = _context()
    context.responses = _documents_of_x(listed=[DX], versions=[VX])

    entry.expected(context)


@pytest.mark.parametrize(
    ("listed", "versions"),
    [([DX, DY], [VX, VY]), ([DX], [VY])],
    ids=["document_of_y", "version_of_y"],
)
def test_a_document_or_version_of_index_y_under_x_fails(
    listed: list[UUID], versions: list[UUID]
) -> None:
    entry = ENTRIES["ing.documents.other_index_absent"]
    context = _context()
    context.responses = _documents_of_x(listed=listed, versions=versions)

    with pytest.raises(AssertionError):
        entry.expected(context)


@pytest.mark.parametrize(
    ("position", "answer"),
    [
        (3, httpx.Response(200, content=b"%PDF-1.7")),
        (4, _json(404, {"code": "index_not_found"})),
    ],
    ids=["pdf_of_y", "version_of_y_not_refused_as_a_version"],
)
def test_a_pdf_or_a_version_of_index_y_under_x_fails(position: int, answer: httpx.Response) -> None:
    entry = ENTRIES["ing.documents.other_index_absent"]
    context = _context()
    context.responses = _documents_of_x(listed=[DX], versions=[VX])
    context.responses[position] = answer

    with pytest.raises(AssertionError):
        entry.expected(context)


async def test_x_documents_ask_for_ys_pdf_and_version_under_x() -> None:
    seen: list[tuple[str, str, str | None]] = []

    async def answer(request: Request) -> Response:
        seen.append((request.method, request.url.path, request.headers.get("content-type")))
        return JSONResponse({"items": [], "nextCursor": None})

    app = Starlette(routes=[Route("/{rest:path}", answer, methods=["GET", "PUT"])])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        context = _context(client)
        await ENTRIES["ing.documents.other_index_absent"].action(context)

    assert seen[3:] == [
        ("GET", f"/v1/admin/indexes/{X}/documents/{DY}/pdf", None),
        ("PUT", f"/v1/admin/indexes/{X}/uploads/{VY}/content", "application/octet-stream"),
    ]


def _runs(*runs: tuple[UUID, UUID]) -> list[httpx.Response]:
    items = [{"runId": str(run), "index": {"indexId": str(index)}} for run, index in runs]
    return [_json(200, {"items": items, "nextCursor": None})]


def test_a_run_list_filtered_to_x_holds_only_xs_runs() -> None:
    entry = ENTRIES["ing.ingestion_status.other_index_absent"]
    context = _context()
    context.responses = _runs((RX, X))
    entry.expected(context)

    context.responses = _runs((RX, X), (RY, Y))
    with pytest.raises(AssertionError):
        entry.expected(context)


PY = uuid4()


def _upload_answers(submit_outcome: str, document_id: UUID) -> list[httpx.Response]:
    return [
        _json(
            200,
            {"runId": None, "results": [{"versionId": str(PY), "outcome": submit_outcome}]},
        ),
        _json(201, {"documentId": str(document_id), "versionId": str(uuid4())}),
    ]


def test_uploads_under_x_that_leave_y_alone_pass() -> None:
    entry = ENTRIES["ing.uploads.other_index_refused"]
    context = _context()
    context.ids["PY"] = PY
    context.responses = _upload_answers("not_found", uuid4())
    entry.expected(context)


@pytest.mark.parametrize(("outcome", "reuses_y"), [("queued", False), ("not_found", True)])
def test_an_upload_under_x_that_touches_y_fails(outcome: str, reuses_y: bool) -> None:
    entry = ENTRIES["ing.uploads.other_index_refused"]
    context = _context()
    context.ids["PY"] = PY
    context.responses = _upload_answers(outcome, DY if reuses_y else uuid4())
    with pytest.raises(AssertionError):
        entry.expected(context)


# --- Chat and retrieval --------------------------------------------------------------------------

CHAT_ENTRIES = (
    "chat.other_project",
    "chat.session_other_project",
    "chat.foreign_session",
    "chat.retrieval_scope",
    "retrieval.shared_index",
    "retrieval.override_unmapped",
)


def _ndjson(*events: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, content=b"".join(json.dumps(event).encode() + b"\n" for event in events)
    )


def _chat_context() -> IsolationContext:
    context = _context()
    context.chat = ChatFakes()
    context.names["X"] = "docchat-x-g1"
    return context


def test_the_six_chat_entries_are_registered_with_their_paths() -> None:
    assert set(CHAT_ENTRIES) <= set(ENTRIES)
    assert {ENTRIES[name].owner for name in CHAT_ENTRIES} == {"chat"}
    assert {ENTRIES[name].data_path for name in CHAT_ENTRIES} == {"chat", "retrieval"}
    problems = completeness_problems(DATA_PATHS)
    assert "chat: no negative entry" not in problems
    assert "retrieval: no negative entry" not in problems


def test_another_project_must_be_refused_without_a_model_call() -> None:
    check = ENTRIES["chat.other_project"].expected
    context = _chat_context()
    context.responses.append(_json(404, {"code": "project_not_found"}))
    check(context)

    assert context.chat is not None
    context.chat.model.requests.append({"kind": "chat"})
    with pytest.raises(AssertionError):
        check(context)


def test_a_foreign_session_must_answer_like_another_projects_session() -> None:
    check = ENTRIES["chat.foreign_session"].expected
    same = _chat_context()
    same.responses += [
        _json(404, {"code": "session_not_found", "correlationId": "a" * 32}),
        _json(404, {"code": "session_not_found", "correlationId": "b" * 32}),
    ]
    check(same)
    differing = _chat_context()
    differing.responses += [
        _json(404, {"code": "session_not_found", "sessionId": str(uuid4())}),
        _json(404, {"code": "session_not_found"}),
    ]

    with pytest.raises(AssertionError):
        check(differing)


def test_the_retrieval_scope_reads_the_trace_and_the_searched_index() -> None:
    check = ENTRIES["chat.retrieval_scope"].expected
    context = _chat_context()
    assert context.chat is not None
    context.chat.search.requests.append({"index": "docchat-x-g1"})
    context.found["trace"] = {
        "retrieval": {"searchIndexId": str(X), "kept": [{"documentId": str(DX)}]}
    }
    check(context)

    context.found["trace"]["retrieval"]["kept"].append({"documentId": str(DY)})
    with pytest.raises(AssertionError):
        check(context)


def test_a_shared_index_must_give_both_callers_its_sources() -> None:
    check = ENTRIES["retrieval.shared_index"].expected
    cited = {"type": "citations", "citations": [{"number": 1}], "allowedLinks": []}
    context = _chat_context()
    assert context.chat is not None
    context.chat.search.requests += [{"index": "docchat-x-g1"}, {"index": "docchat-x-g1"}]
    context.responses += [_ndjson({"type": "meta"}, cited), _ndjson({"type": "meta"}, cited)]
    check(context)

    context.responses[1] = _ndjson(
        {"type": "meta"}, {"type": "citations", "citations": [], "allowedLinks": []}
    )
    with pytest.raises(AssertionError):
        check(context)


def test_an_unmapped_override_must_be_refused_and_the_default_used() -> None:
    check = ENTRIES["retrieval.override_unmapped"].expected
    context = _chat_context()
    assert context.chat is not None
    context.chat.search.requests.append({"index": "docchat-x-g1"})
    context.responses.append(_ndjson({"type": "meta"}, {"type": "done"}))
    context.refused.append("IntegrityError")
    check(context)

    context.refused.clear()
    with pytest.raises(AssertionError):
        check(context)


# --- Citation entries -------------------------------------------------------------------------

CITATION_ENTRIES = (
    "citations.foreign_turn",
    "citations.revoked",
    "citations.superseded",
    "citations.unmapped_index",
    "ia.mappings.unmapped_hides_citations",
)
SA, SB, CA, CB, CX, A = (uuid4() for _ in range(6))
PDF = b"%PDF-1.7\n% a verified rendition\n%%EOF\n"


def test_the_citation_entries_are_registered_and_negative() -> None:
    for name in CITATION_ENTRIES:
        entry = ENTRIES[name]
        assert entry.negative is True
        assert entry.actor == "alice"
    assert {ENTRIES[name].owner for name in CITATION_ENTRIES} == {"chat", "ia"}
    assert "citations: no negative entry" not in completeness_problems(DATA_PATHS)


def _citation_context(client: Any = None) -> IsolationContext:
    context = _context(client)
    context.ids |= {
        "A": A,
        "CX": CX,
        "SA": SA,
        "SB": SB,
        "CA": CA,
        "CB": CB,
        "TA": turn_id(SA, CA),
        "TB": turn_id(SB, CB),
    }
    return context


class FakeDatabase:
    def __init__(self) -> None:
        self.containers = {
            definition.name: FakeContainer.of(definition)
            for definition in (CONVERSATIONS, MEMORIES, USAGE)
        }

    def get_container_client(self, name: str) -> FakeContainer:
        return self.containers[name]


class FakeRenditions:
    def __init__(self) -> None:
        self.uploaded: dict[str, bytes] = {}

    async def upload_blob(self, name: str, data: bytes, *, overwrite: bool) -> None:
        assert overwrite
        self.uploaded[name] = data


async def test_the_foreign_turn_entry_writes_both_turns_and_opens_bobs() -> None:
    seen: list[str] = []

    async def refuse(request: Request) -> Response:
        seen.append(request.url.path)
        return JSONResponse({"code": "citation_not_found"}, status_code=404)

    app = Starlette(routes=[Route("/{rest:path}", refuse)])
    database, renditions = FakeDatabase(), FakeRenditions()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        context = _citation_context(client)
        context.cosmos, context.renditions = database, renditions
        alice = str(context.principals["alice"].oid)
        database.containers["usage"].put(
            {"id": "dllease:1", "userId": alice, "type": "download_lease", "ttl": 300}
        )
        entry = ENTRIES["citations.foreign_turn"]
        await entry.action(context)
        entry.expected(context)

    base = f"/v1/sessions/{SB}/turns/{turn_id(SB, CB)}/citations/1"
    assert seen == [base, f"{base}/content", f"{base}/document"]
    conversations = database.containers["conversations"]
    alices = conversations.get([alice, str(SA)], str(turn_id(SA, CA)))
    bob = str(context.principals["bob"].oid)
    bobs = conversations.get([bob, str(SB)], str(turn_id(SB, CB)))
    assert alices is not None
    assert bobs is not None
    assert alices["projectId"] == bobs["projectId"] == str(A)
    assert alices["citations"][0]["documentId"] == str(DX)
    assert set(renditions.uploaded) == {
        f"{X}/{DX}/1/pages/1.pdf",
        f"{X}/{DX}/1/document.pdf",
    }
    assert database.containers["usage"].get(alice, "dllease:1") is None


def _citation_answers(status: int, code: str) -> list[httpx.Response]:
    return [_json(status, {"code": code})] * 3


@pytest.mark.parametrize(
    "name",
    ["citations.foreign_turn", "citations.unmapped_index", "ia.mappings.unmapped_hides_citations"],
)
def test_a_hidden_citation_is_404_on_all_three(name: str) -> None:
    context = _citation_context()
    context.responses = _citation_answers(404, "citation_not_found")
    ENTRIES[name].expected(context)

    context.responses[2] = httpx.Response(200, content=PDF)
    with pytest.raises(AssertionError):
        ENTRIES[name].expected(context)


def test_a_superseded_citation_is_410_on_all_three() -> None:
    context = _citation_context()
    context.responses = _citation_answers(410, "document_changed")
    ENTRIES["citations.superseded"].expected(context)

    context.responses = _citation_answers(404, "citation_not_found")
    with pytest.raises(AssertionError):
        ENTRIES["citations.superseded"].expected(context)


def _restored() -> list[httpx.Response]:
    pdf = {"content-type": "application/pdf"}
    return [
        _json(200, {"number": 1}),
        httpx.Response(200, content=PDF, headers=pdf),
        httpx.Response(200, content=PDF, headers=pdf),
    ]


def test_a_revoked_citation_hides_then_shows() -> None:
    context = _citation_context()
    context.responses = [*_citation_answers(404, "citation_not_found"), *_restored()]
    ENTRIES["citations.revoked"].expected(context)

    context.responses = [*_citation_answers(404, "citation_not_found")] * 2
    with pytest.raises(AssertionError):
        ENTRIES["citations.revoked"].expected(context)


class FakeAudit:
    def __init__(self, users: set[str]) -> None:
        self.users = users
        self.checked = False

    def user_ids(self) -> set[str]:
        return self.users

    def check(self) -> None:
        self.checked = True


def test_the_audit_must_name_only_alices_partition() -> None:
    context = _citation_context()
    context.responses = _citation_answers(404, "citation_not_found")
    alice = str(context.principals["alice"].oid)
    audit = FakeAudit({alice})
    context.audit = audit
    ENTRIES["citations.foreign_turn"].expected(context)
    assert audit.checked

    context.audit = FakeAudit({alice, str(context.principals["bob"].oid)})
    with pytest.raises(AssertionError):
        ENTRIES["citations.foreign_turn"].expected(context)


# --- Conversation, memory and feedback entries ---------------------------------------------------

CONVERSATION_ENTRIES = (
    "conv.sessions.list_own_only",
    "conv.sessions.other_user",
    "conv.sessions.revoked_and_restored",
    "conv.sessions.project_mismatch",
    "conv.memory.other_user_fact",
    "conv.memory.own_document_only",
    "conv.feedback.other_user",
    "conv.feedback.revoked_and_restored",
)


def test_t003_the_conversation_entries_complete_the_registry() -> None:
    assert set(CONVERSATION_ENTRIES) <= set(ENTRIES)
    for name in CONVERSATION_ENTRIES:
        entry = ENTRIES[name]
        assert (entry.owner, entry.negative, entry.actor) == ("conv", True, "alice")
    assert {ENTRIES[name].data_path for name in CONVERSATION_ENTRIES} == {
        "sessions",
        "memory",
        "feedback",
    }
    assert completeness_problems(DATA_PATHS) == []


def _conversation_context(client: Any = None) -> IsolationContext:
    context = _context(client)
    context.ids |= {"A": A, "B": uuid4()}
    context.cosmos = FakeDatabase()
    return context


def _not_found_body(code: str) -> httpx.Response:
    return _json(404, {"code": code})


def test_the_session_list_must_hold_only_alices_session() -> None:
    check = ENTRIES["conv.sessions.list_own_only"].expected
    context = _conversation_context()
    own, bobs = uuid4(), uuid4()
    context.ids["SA"] = own
    page = {"items": [{"sessionId": str(own)}], "nextCursor": None}
    context.responses = [_json(200, page), _json(200, page)]
    check(context)

    page = {"items": [{"sessionId": str(own)}, {"sessionId": str(bobs)}], "nextCursor": None}
    context.responses[1] = _json(200, page)
    with pytest.raises(AssertionError):
        check(context)


def test_a_revoked_session_must_hide_then_show_with_both_turns() -> None:
    check = ENTRIES["conv.sessions.revoked_and_restored"].expected
    context = _conversation_context()
    session, first, second = uuid4(), uuid4(), uuid4()
    context.ids |= {"S": session, "ST1": first, "ST2": second}
    listed = {"items": [{"sessionId": str(session)}], "nextCursor": None}
    turns = [{"turnId": str(first)}, {"turnId": str(second)}]
    context.responses = [
        _json(200, {"items": [], "nextCursor": None}),
        _not_found_body("session_not_found"),
        _not_found_body("session_not_found"),
        _json(200, listed),
        _json(200, {"turns": turns}),
        _json(200, {"items": turns, "nextCursor": None}),
    ]
    check(context)

    context.responses[0] = _json(200, listed)
    with pytest.raises(AssertionError):
        check(context)


def test_a_project_mismatch_must_create_no_turn() -> None:
    check = ENTRIES["conv.sessions.project_mismatch"].expected
    context = _conversation_context()
    context.chat = ChatFakes()
    context.responses = [_not_found_body("session_not_found")]
    context.found["session"] = {"turnCount": 1, "activeTurn": None}
    check(context)

    context.found["session"] = {"turnCount": 2, "activeTurn": None}
    with pytest.raises(AssertionError):
        check(context)


async def test_the_foreign_fact_entry_sends_each_callers_own_etag() -> None:
    seen: list[tuple[str, str, str | None, str]] = []

    async def answer(request: Request) -> Response:
        caller = request.headers["authorization"]
        seen.append((request.method, request.url.path, request.headers.get("if-match"), caller))
        if request.method == "GET":
            return JSONResponse({"etag": f'"{len(seen)}"', "facts": []})
        return JSONResponse({"code": "memory_fact_not_found"}, status_code=404)

    app = Starlette(routes=[Route("/{rest:path}", answer, methods=["GET", "PUT", "DELETE"])])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        context = _conversation_context(client)
        entry = ENTRIES["conv.memory.other_user_fact"]
        await entry.action(context)
        entry.expected(context)

    fact_path = f"/v1/memory/facts/{context.ids['Fbob0']}"
    assert [(method, path, etag) for method, path, etag, _ in seen] == [
        ("GET", "/v1/memory", None),
        ("PUT", fact_path, '"1"'),
        ("DELETE", fact_path, '"1"'),
        ("GET", "/v1/memory", None),
        ("PUT", fact_path, '"4"'),
        ("DELETE", fact_path, '"4"'),
    ]
    assert seen[0][3] != seen[3][3]
    bob = str(context.principals["bob"].oid)
    assert context.cosmos.containers["memories"].get(bob, bob) is not None


def test_bobs_memory_must_stay_unchanged() -> None:
    check = ENTRIES["conv.memory.other_user_fact"].expected
    context = _conversation_context()
    context.responses = [_not_found_body("memory_fact_not_found")] * 4
    stored = {"_etag": '"1"', "facts": [{"id": str(uuid4())}]}
    context.found |= {"memory:bob": stored, "memory:bob:after": dict(stored)}
    check(context)

    context.found["memory:bob:after"] = {"_etag": '"2"', "facts": []}
    with pytest.raises(AssertionError):
        check(context)


async def test_the_feedback_entry_writes_bobs_turn_and_reads_it_back() -> None:
    async def refuse(request: Request) -> Response:
        return JSONResponse({"code": "turn_not_found"}, status_code=404)

    app = Starlette(routes=[Route("/{rest:path}", refuse, methods=["PUT"])])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        context = _conversation_context(client)
        entry = ENTRIES["conv.feedback.other_user"]
        await entry.action(context)
        entry.expected(context)

    assert context.found["turn"]["projectId"] == str(A)
    assert context.found["turn"]["feedback"] is None

    context.found["turn"] = {"feedback": {"rating": "up"}}
    with pytest.raises(AssertionError):
        entry.expected(context)


def test_the_memory_entries_check_each_callers_document() -> None:
    check = ENTRIES["conv.memory.own_document_only"].expected
    context = _conversation_context()
    first, second = uuid4(), uuid4()
    context.ids |= {"Falice0": first, "Falice1": second}
    stored = {"_etag": '"1"', "facts": []}
    context.found |= {"memory:bob": stored, "memory:bob:after": stored}
    own = [{"factId": str(first)}, {"factId": str(second)}]
    context.responses = [
        _json(200, {"facts": own}),
        _json(200, {"facts": []}),
        _json(200, {"facts": []}),
        _json(200, {"facts": []}),
    ]
    check(context)

    context.responses[2] = _json(200, {"facts": [{"factId": str(uuid4())}]})
    with pytest.raises(AssertionError):
        check(context)


def test_a_revoked_member_is_refused_and_her_sessions_hidden_until_restored() -> None:
    check = ENTRIES["ia.members.revoked_and_restored"].expected
    context = _conversation_context()
    session = uuid4()
    context.ids["S"] = session
    listed = {"items": [{"sessionId": str(session)}], "nextCursor": None}
    context.responses = [
        _ndjson({"type": "meta", "sessionId": str(session)}),
        _not_found_body("project_not_found"),
        _json(200, {"items": [], "nextCursor": None}),
        _json(200, listed),
    ]
    check(context)

    context.responses[2] = _json(200, listed)
    with pytest.raises(AssertionError):
        check(context)


# --- The identity-and-access entries over the chat path -----------------------------------------

IA_CHAT_ENTRIES = (
    "ia.projects.archived_hidden",
    "ia.mappings.mapped_only",
    "ia.overrides.expired_ignored",
    "ia.overrides.other_user_unchanged",
)


def test_the_identity_and_access_chat_entries_are_registered_and_negative() -> None:
    assert {(ENTRIES[name].owner, ENTRIES[name].negative) for name in IA_CHAT_ENTRIES} == {
        ("ia", True)
    }
    assert [ENTRIES[name].data_path for name in IA_CHAT_ENTRIES] == [
        "projects",
        "mappings",
        "overrides",
        "overrides",
    ]


def _archived_answers(listed: list[str]) -> list[httpx.Response]:
    items = {"items": [{"projectId": project} for project in listed], "nextCursor": None}
    return [
        _json(200, items),
        _json(200, items),
        _not_found_body("project_not_found"),
        _not_found_body("project_not_found"),
        _not_found_body("session_not_found"),
        _not_found_body("turn_not_found"),
        *(_not_found_body("citation_not_found") for _ in range(3)),
    ]


def test_an_archived_project_must_be_hidden_on_both_lists_and_every_chat_path() -> None:
    check = ENTRIES["ia.projects.archived_hidden"].expected
    context = _context()
    other = uuid4()
    context.ids |= {"A": A}
    context.responses = _archived_answers([str(other)])
    check(context)

    still_listed = _context()
    still_listed.ids |= {"A": A}
    still_listed.responses = _archived_answers([str(A)])
    with pytest.raises(AssertionError):
        check(still_listed)
    served = _context()
    served.ids |= {"A": A}
    served.responses = _archived_answers([])
    served.responses[3] = _ndjson({"type": "meta"}, {"type": "done"})
    with pytest.raises(AssertionError):
        check(served)


def test_a_turn_in_a_must_search_only_as_index() -> None:
    check = ENTRIES["ia.mappings.mapped_only"].expected
    context = _chat_context()
    assert context.chat is not None
    context.ids["X"] = X
    context.names["Y"] = "docchat-y-g1"
    context.found["trace"] = {"retrieval": {"searchIndexId": str(X)}}
    context.responses += [_ndjson({"type": "done"}), _ndjson({"type": "done"})]
    context.chat.search.requests += [{"index": "docchat-x-g1"}, {"index": "docchat-y-g1"}]
    check(context)

    context.chat.search.requests[0] = {"index": "docchat-y-g1"}
    with pytest.raises(AssertionError):
        check(context)


def test_an_expired_override_must_leave_the_default_in_use() -> None:
    check = ENTRIES["ia.overrides.expired_ignored"].expected
    context = _chat_context()
    assert context.chat is not None
    context.responses.append(_ndjson({"type": "meta"}, {"type": "done"}))
    context.chat.search.requests.append({"index": "docchat-x-g1"})
    check(context)

    context.chat.search.requests[0] = {"index": "docchat-w-g1"}
    with pytest.raises(AssertionError):
        check(context)


def test_alices_override_must_leave_bobs_resolution_alone() -> None:
    check = ENTRIES["ia.overrides.other_user_unchanged"].expected
    context = _chat_context()
    assert context.chat is not None
    context.names["W"] = "docchat-w-g1"
    context.responses += [_ndjson({"type": "done"}), _ndjson({"type": "done"})]
    context.chat.search.requests += [{"index": "docchat-w-g1"}, {"index": "docchat-x-g1"}]
    check(context)

    context.chat.search.requests[1] = {"index": "docchat-w-g1"}
    with pytest.raises(AssertionError):
        check(context)
