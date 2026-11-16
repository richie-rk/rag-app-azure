from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest

from docchat_core.cosmos.conversations.ids import (
    SESSION_NAMESPACE,
    TURN_NAMESPACE,
    new_session_id,
    turn_id,
    turn_trace_id,
)

# The reference vectors of 13-conversations-and-memory.md section 1.4.
_OID = "0b8f3c1e-6a2d-4f7b-9c55-1d2e3f405162"
_OTHER_OID = "9a1b2c3d-4e5f-4061-8a7b-8c9d0e1f2a3b"
_CLIENT_TURN_ID = "7d9e2b4a-1c3f-4e5d-8a6b-9f0e1d2c3b4a"
_SESSION_ID = UUID("b6061e08-59bc-576e-a78d-b2d375c588e7")
_TURN_ID = UUID("445c1c2a-be4c-5d17-a92d-c4efcb094740")
_TRACE_ID = UUID("fe3744f8-fb5b-5f1e-8cd7-9da279920e68")
_OTHER_SESSION_ID = UUID("12d8a54e-8d16-5277-bc56-e13ab4a5d9ad")


def test_the_namespaces_never_change() -> None:
    assert UUID("60191fe1-b79c-4c47-995d-e0a0e20cb164") == SESSION_NAMESPACE
    assert UUID("fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701") == TURN_NAMESPACE


@pytest.mark.parametrize("given", [str.lower, str.upper, UUID], ids=["lower", "upper", "uuid"])
def test_t094_the_reference_vectors(given: Callable[[str], UUID | str]) -> None:
    session = new_session_id(given(_OID), given(_CLIENT_TURN_ID))
    turn = turn_id(given(str(session)), given(_CLIENT_TURN_ID))

    assert session == _SESSION_ID
    assert turn == _TURN_ID
    assert turn_trace_id(given(str(turn))) == _TRACE_ID
    assert new_session_id(given(_OTHER_OID), given(_CLIENT_TURN_ID)) == _OTHER_SESSION_ID


def test_ids_are_version_5() -> None:
    assert {_SESSION_ID.version, _TURN_ID.version, _TRACE_ID.version} == {5}


@pytest.mark.parametrize(
    "spelling",
    ["{" + _OID + "}", "urn:uuid:" + _OID, _OID.replace("-", ""), " " + _OID, ""],
)
def test_other_spellings_of_a_uuid_are_refused(spelling: str) -> None:
    with pytest.raises(ValueError, match="canonical"):
        new_session_id(spelling, _CLIENT_TURN_ID)
