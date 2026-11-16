"""The dry-run summary and the confirmation prompt (`86-operations.md` section 2.4; ADR 0139
item 6 step 4).

The summary names the environment, the targets by identifier, counts and, for ops config, the old
and new values, never a name, an e-mail address or content. It goes to standard error with the
prompt, so standard output keeps only the report (one JSON object under `--json`).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from docchat_ops.framework.exitcodes import UsageError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

PROD: Final = "prod"
YES: Final = "yes"
YES_PROMPT: Final = "Type yes to continue: "
PROD_PROMPT: Final = "Type the environment name to continue: "


def require_terminal(environment: str, stdin: TextIO) -> None:
    """A `prod` change is confirmed by a person at a terminal: without one it stops before its
    summary (exit 2)."""
    if environment == PROD and not stdin.isatty():
        raise UsageError(
            "a prod change needs a terminal to confirm it; nothing was written",
            "run the command from an interactive terminal",
        )


async def confirm(
    summary: Sequence[str],
    *,
    environment: str,
    assume_yes: bool,
    stdin: TextIO,
    stderr: TextIO,
) -> None:
    """Print the summary, then ask: outside `prod` for `yes` unless `--yes` was given; in `prod`
    always for the environment name. Any other answer is exit 2 with nothing written."""
    require_terminal(environment, stdin)
    stderr.write(f"Dry run in {environment}:\n")
    for line in summary:
        stderr.write(f"  {line}\n")
    if environment != PROD and assume_yes:
        return
    expected, prompt = (PROD, PROD_PROMPT) if environment == PROD else (YES, YES_PROMPT)
    stderr.write(prompt)
    stderr.flush()
    # The read blocks until the operator answers, so it waits in a thread, off the event loop.
    answer = await asyncio.to_thread(stdin.readline)
    if not stdin.isatty():
        # Piped input isn't echoed, so the prompt's line is ended here.
        stderr.write("\n")
    if answer.strip() != expected:
        raise UsageError(
            "the change wasn't confirmed; nothing was written",
            "run the command again and confirm it",
        )
