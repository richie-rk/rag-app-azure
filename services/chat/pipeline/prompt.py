"""Prompt templates and the LLM message builder.

A custom system-prompt template must keep the {sources} placeholder, or the
renderer leaves the retrieved context out of the prompt.
"""

import re

DEFAULT_SYSTEM_PROMPT = """You are an intelligent assistant helping users find information.
Use the following sources to answer the user's question. Each source is identified by a filename.
When you reference information from a source, cite it using square brackets, e.g. [filename.pdf].
If multiple sources support a statement, cite all of them, e.g. [file1.pdf][file2.pdf].
If you cannot find the answer in the sources, say you don't have enough information.
Do not fabricate answers or cite sources that are not provided.

Sources:
{sources}

{chat_history}
"""

FOLLOWUP_PROMPT = """Generate 3 brief follow-up questions that the user might ask next based on the conversation.
Wrap each question in double angle brackets, e.g. <<What are the exclusion criteria?>>.
Only output the questions, nothing else."""


def build_context_string(data_points: list[dict]) -> str:
    """Format search results as a context string for the prompt."""
    parts = []
    for dp in data_points:
        sourcepage = dp.get("sourcepage", "")
        content = dp.get("content", "")
        parts.append(f"{sourcepage}:{content}")
    return "\n".join(parts)


def build_chat_history_text(history: list[dict]) -> str:
    """Format chat history for injection into the prompt.

    Excludes the last turn (current question).
    """
    lines = []
    for turn in history[:-1]:
        user_msg = turn.get("user", "")
        bot_msg = turn.get("bot", "")
        if user_msg:
            lines.append(f"User: {user_msg}")
        if bot_msg:
            lines.append(f"Assistant: {bot_msg}")
    return "\n".join(lines)


# overrides.prompt_template is caller-controlled, so str.format here would let a
# payload like {sources.__class__...} walk to Python internals and would raise on
# stray braces. Substituting only the two known tokens by regex escapes both traps,
# and re.sub never re-scans replacement text, so a source that itself contains
# "{chat_history}" cannot pull the history in.
_PLACEHOLDER_RE = re.compile(r"\{(sources|chat_history)\}")


def _render_template(template: str, sources: str, chat_history: str) -> str:
    values = {"sources": sources, "chat_history": chat_history}
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def build_messages(
    user_query: str,
    data_points: list[dict],
    history: list[dict],
    system_prompt_template: str | None = None,
    suggest_followup: bool = True,
) -> list[dict[str, str]]:
    """Build the full messages list for the LLM call.

    Returns list of {role, content} dicts ready for OpenAI API.
    """
    template = system_prompt_template or DEFAULT_SYSTEM_PROMPT

    sources = build_context_string(data_points)
    chat_history = build_chat_history_text(history)

    system_content = _render_template(template, sources, chat_history)

    if suggest_followup:
        system_content += "\n\n" + FOLLOWUP_PROMPT

    messages: list[dict[str, str]] = []

    # Add chat history as proper role messages
    for turn in history[:-1]:
        if turn.get("user"):
            messages.append({"role": "user", "content": turn["user"]})
        if turn.get("bot"):
            messages.append({"role": "assistant", "content": turn["bot"]})

    # System prompt with sources
    messages.insert(0, {"role": "system", "content": system_content})

    # Current user query
    messages.append({"role": "user", "content": user_query})

    return messages
