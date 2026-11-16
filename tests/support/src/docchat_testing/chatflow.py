"""What the chat pipeline's integration tests share (`12-chat-pipeline.md` acceptance criteria;
`84-testing.md` sections 2.3 and 2.9).

- `ChatFlow`: a seeded world (`isolation.IsolationContext`, so the same `seed_project` and
  `seed_served_index` lay it out) with the `createChatTurn` requests the tests send and the stored
  turn, trace and session they read back from the emulator.
- `stream_events(response)`: a finished stream's events as JSON objects, its framing and order
  checked by `streaming.collect_stream`.
- `StubControl`: the model stub's inspection and scripting endpoints (`/_stub/requests`,
  `/_stub/scripts/{name}`, `/_stub/stats`), for the tests that run the API's own clients against
  the stub of `compose.yaml`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast
from uuid import UUID, uuid4

import httpx

from docchat_core.cosmos.conversations import turn_trace_id
from docchat_testing.fakes.chat import ChatFakes, search_result
from docchat_testing.fixtures.identities import PrincipalName
from docchat_testing.isolation import IsolationContext
from docchat_testing.streaming import collect_stream

CHAT_TURNS_PATH: Final = "/v1/chat/turns"
QUESTION: Final = "How many days do I have to submit an expense claim?"


@dataclass
class ChatFlow:
    """A seeded world and the calls the chat tests make in it."""

    world: IsolationContext
    conversations: Any
    """The emulator's `conversations` container without the audit, for the tests' reads."""

    @property
    def client(self) -> httpx.AsyncClient:
        return self.world.client

    @property
    def fakes(self) -> ChatFakes:
        if self.world.chat is None:
            raise AssertionError("the world runs over the chat doubles")
        return self.world.chat

    def headers(self, caller: PrincipalName = "alice") -> dict[str, str]:
        return self.world.headers(caller)

    def oid(self, caller: PrincipalName = "alice") -> str:
        return str(self.world.principals[caller].oid)

    def body(
        self,
        project: str = "A",
        *,
        session: str | UUID | None = None,
        question: str = QUESTION,
        client_turn_id: UUID | None = None,
    ) -> dict[str, Any]:
        """A `ChatTurnRequest` in project `project` (a label of the world)."""
        return {
            "projectId": str(self.world.ids[project]),
            "sessionId": None if session is None else str(session),
            "clientTurnId": str(client_turn_id or uuid4()),
            "question": question,
        }

    async def ask(
        self,
        caller: PrincipalName = "alice",
        project: str = "A",
        *,
        headers: Mapping[str, str] | None = None,
        **fields: Any,
    ) -> httpx.Response:
        """One `createChatTurn`, read to its end."""
        return await self.client.post(
            CHAT_TURNS_PATH,
            json=self.body(project, **fields),
            headers=dict(headers) if headers is not None else self.headers(caller),
        )

    def result(self, text: str, *, label: str = "X", **extra: Any) -> dict[str, Any]:
        """A search result of index `label`'s seeded document holding `text`."""
        return search_result(
            document_id=self.world.ids[f"D{label}"],
            chunk_set_id=self.world.ids[f"C{label}"],
            version_id=self.world.ids[f"V{label}"],
            text=text,
            **extra,
        )

    async def turn(
        self, meta: Mapping[str, Any], caller: PrincipalName = "alice", *, trace: bool = False
    ) -> dict[str, Any]:
        """The stored turn, or its trace, that a stream's `meta` names."""
        turn = UUID(str(meta["turnId"]))
        item = str(turn_trace_id(turn)) if trace else str(turn)
        partition = [self.oid(caller), str(meta["sessionId"])]
        return cast("dict[str, Any]", await self.conversations.read_item(item, partition))

    async def session(self, session_id: str, caller: PrincipalName = "alice") -> dict[str, Any]:
        partition = [self.oid(caller), session_id]
        return cast("dict[str, Any]", await self.conversations.read_item(session_id, partition))


async def stream_events(response: httpx.Response) -> list[dict[str, Any]]:
    """A finished `createChatTurn` stream's events as JSON objects, framing and order checked."""
    if response.status_code != httpx.codes.OK:
        raise AssertionError(f"the turn answered {response.status_code}, not a stream")
    return [
        event.model_dump(by_alias=True, mode="json") for event in await collect_stream(response)
    ]


@dataclass
class StubControl:
    """The model stub's inspection and scripting endpoints (`84-testing.md` section 2.3), over a
    client of the stub's base URL."""

    client: httpx.AsyncClient
    seen: int = 0
    scripted: list[str] = field(default_factory=list)

    async def mark(self) -> None:
        """Remember the newest recorded request, so `requests()` returns only later ones."""
        recorded = await self.requests()
        if recorded:
            self.seen = int(recorded[-1]["seq"])

    async def requests(self) -> list[dict[str, Any]]:
        response = await self.client.get("/_stub/requests", params={"after": self.seen})
        response.raise_for_status()
        return cast("list[dict[str, Any]]", response.json()["requests"])

    async def chat_requests(self, deployment: str | None = None) -> list[dict[str, Any]]:
        """The recorded chat completion requests, of one deployment when given."""
        return [
            entry
            for entry in await self.requests()
            if str(entry["path"]).endswith("/chat/completions")
            and (deployment is None or entry["deployment"] == deployment)
        ]

    async def json_requests(self, name: str) -> list[dict[str, Any]]:
        """The recorded structured-output requests for the response schema `name`."""
        return [
            entry
            for entry in await self.chat_requests()
            if (entry["body"].get("response_format") or {}).get("json_schema", {}).get("name")
            == name
        ]

    async def screening_requests(self) -> list[dict[str, Any]]:
        return [
            entry
            for entry in await self.requests()
            if str(entry["path"]).endswith("text:shieldPrompt")
        ]

    async def script(self, name: str, responses: list[dict[str, Any]]) -> None:
        """Queue scripted responses for `name` (a schema name, or `shield_prompt`)."""
        response = await self.client.put(f"/_stub/scripts/{name}", json={"responses": responses})
        response.raise_for_status()
        self.scripted.append(name)

    async def clear_scripts(self) -> None:
        if self.scripted:
            response = await self.client.delete("/_stub/scripts")
            response.raise_for_status()
            self.scripted.clear()

    async def stats(self) -> dict[str, Any]:
        response = await self.client.get("/_stub/stats")
        response.raise_for_status()
        return cast("dict[str, Any]", response.json())
