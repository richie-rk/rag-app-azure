"""The derived identifiers of the `conversations` container (`13-conversations-and-memory.md`
section 1.4; `80-data-model.md` section 3.10; ADR 0015 item 4).

Session, turn and trace IDs are version-5 UUIDs over two constant namespaces, so a double submit
of one request derives the same IDs and its create conflicts (`409`) instead of duplicating,
with no lookup table. A session ID derived from the caller's own `oid` can't collide with
another user's. The namespaces never change, because stored IDs depend on them. Names are UTF-8,
and every UUID inside a name is canonical lower-case hyphenated, so upper-case input yields the
same ID.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid5

from docchat_core.models.identifiers import parse_uuid

SESSION_NAMESPACE: Final = UUID("60191fe1-b79c-4c47-995d-e0a0e20cb164")
TURN_NAMESPACE: Final = UUID("fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701")
"""The namespace of turn IDs and of turn trace IDs."""


def _canonical(value: UUID | str) -> str:
    # `str(UUID)` is always lower-case hyphenated; a string is accepted only in that form, in
    # either case, so a malformed ID can't name a different session.
    return str(value if isinstance(value, UUID) else parse_uuid(value))


def new_session_id(oid: UUID | str, client_turn_id: UUID | str) -> UUID:
    """The ID of the session a request without `sessionId` creates."""
    return uuid5(SESSION_NAMESPACE, f"{_canonical(oid)}:{_canonical(client_turn_id)}")


def turn_id(session_id: UUID | str, client_turn_id: UUID | str) -> UUID:
    """The ID of the turn a request creates in a session."""
    return uuid5(TURN_NAMESPACE, f"{_canonical(session_id)}:{_canonical(client_turn_id)}")


def turn_trace_id(turn: UUID | str) -> UUID:
    """The ID of a turn's `turn_trace` document."""
    return uuid5(TURN_NAMESPACE, f"{_canonical(turn)}:trace")
