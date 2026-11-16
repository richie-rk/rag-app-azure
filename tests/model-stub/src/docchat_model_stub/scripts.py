"""Scripted responses queued by `PUT /_stub/scripts/{name}` (84-testing.md section 2.3).

A structured-output call consumes the next response queued under its schema name before the
schema filler runs; a screening call consumes the next response queued under `shield_prompt`
before any directive applies. Queues live in the worker process, which is why local and CI runs
use one worker.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter

SHIELD_PROMPT = "shield_prompt"

_MIN_ERROR_STATUS = 400
_MAX_ERROR_STATUS = 599


class ScriptBody(BaseModel):
    """The body of `PUT /_stub/scripts/{name}`."""

    model_config = ConfigDict(extra="forbid", strict=True)

    responses: list[Any]


class ScriptedError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: StrictStr
    message: StrictStr


class ScriptedStatus(BaseModel):
    """The status form of a queued `shield_prompt` response: an HTTP error the call returns."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: StrictInt = Field(ge=_MIN_ERROR_STATUS, le=_MAX_ERROR_STATUS)
    error: ScriptedError


_SHIELD_RESPONSES = TypeAdapter(list[dict[str, Any]], config=ConfigDict(strict=True))


def validate_responses(name: str, responses: list[Any]) -> None:
    """Refuse a `shield_prompt` script whose entries the screening endpoint couldn't return.

    Raises a Pydantic `ValidationError`, which is a `ValueError`.
    """
    if name != SHIELD_PROMPT:
        return
    for response in _SHIELD_RESPONSES.validate_python(responses):
        if "status" in response:
            ScriptedStatus.model_validate(response)


class ScriptStore:
    """Queues of scripted responses by name."""

    def __init__(self) -> None:
        self._queues: dict[str, deque[Any]] = {}

    def put(self, name: str, responses: list[Any]) -> None:
        """Replace the queue of `name` with `responses`, consumed first to last."""
        self._queues[name] = deque(responses)

    def pop(self, name: str) -> tuple[bool, Any]:
        """Take the next response of `name`: `(True, response)`, or `(False, None)` when empty."""
        queue = self._queues.get(name)
        if not queue:
            return False, None
        return True, queue.popleft()

    def clear(self) -> None:
        self._queues.clear()
