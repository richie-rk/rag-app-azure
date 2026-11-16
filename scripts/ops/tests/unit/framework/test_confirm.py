"""The dry-run summary and the confirmation prompt (`86-operations.md` section 2.4)."""

from __future__ import annotations

import io
from typing import override

import pytest

from docchat_ops.framework.confirm import confirm, require_terminal
from docchat_ops.framework.exitcodes import UsageError


class _Stdin(io.StringIO):
    def __init__(self, text: str, *, terminal: bool) -> None:
        super().__init__(text)
        self._terminal = terminal

    @override
    def isatty(self) -> bool:
        return self._terminal


async def _confirm(
    environment: str, answer: str, *, terminal: bool = False, assume_yes: bool = False
) -> str:
    stderr = io.StringIO()
    await confirm(
        ["target: x", "count: 2"],
        environment=environment,
        assume_yes=assume_yes,
        stdin=_Stdin(answer, terminal=terminal),
        stderr=stderr,
    )
    return stderr.getvalue()


async def test_yes_flag_skips_the_prompt_outside_prod() -> None:
    printed = await _confirm("dev", "", assume_yes=True)
    assert printed == "Dry run in dev:\n  target: x\n  count: 2\n"


async def test_typed_yes_continues() -> None:
    printed = await _confirm("load", "yes\n")
    assert printed.endswith("Type yes to continue: \n")


@pytest.mark.parametrize("answer", ["", "no\n", "y\n", "YES\n", "prod\n"])
async def test_any_other_answer_is_declined(answer: str) -> None:
    with pytest.raises(UsageError, match="wasn't confirmed"):
        await _confirm("dev", answer)


async def test_prod_needs_the_environment_name_even_with_yes() -> None:
    printed = await _confirm("prod", "prod\n", terminal=True, assume_yes=True)
    assert printed.endswith("Type the environment name to continue: ")


async def test_prod_refuses_yes() -> None:
    with pytest.raises(UsageError):
        await _confirm("prod", "yes\n", terminal=True)


async def test_prod_without_a_terminal_stops_before_the_summary() -> None:
    stderr = io.StringIO()
    with pytest.raises(UsageError, match="needs a terminal"):
        await confirm(
            ["target: x"],
            environment="prod",
            assume_yes=True,
            stdin=_Stdin("prod\n", terminal=False),
            stderr=stderr,
        )
    assert stderr.getvalue() == ""


def test_require_terminal_outside_prod_accepts_a_pipe() -> None:
    require_terminal("dev", _Stdin("", terminal=False))
