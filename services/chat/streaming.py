"""NDJSON streaming helpers.

The stream isn't uniform: the first line carries metadata + data_points,
the last carries follow-up questions + retrieved_docs, and everything
between is content deltas.
"""

import json
from collections.abc import AsyncIterator
from typing import Any


def ndjson_line(data: dict) -> str:
    """Serialize a dict to an NDJSON line."""
    return json.dumps(data, ensure_ascii=False) + "\n"


def make_metadata_chunk(
    data_points: list[dict[str, Any]],
    query: str,
    model: str,
) -> str:
    """Build the first NDJSON chunk with metadata and search results."""
    chunk = {
        "choices": [
            {
                "delta": {"role": "assistant"},
                "context": {
                    "data_points": [
                        f"{dp.get('sourcepage', '')}:{dp.get('content', '')}"
                        for dp in data_points
                    ],
                    "thoughts": f"Searched for:<br>{query}",
                    "model": model,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "finish_reason": None,
                "index": 0,
            }
        ],
        "object": "chat.completion.chunk",
    }
    return ndjson_line(chunk)


def make_content_chunk(content: str) -> str:
    """Build an NDJSON chunk for an LLM content delta."""
    chunk = {
        "choices": [
            {
                "delta": {"content": content},
                "finish_reason": None,
                "index": 0,
            }
        ],
        "object": "chat.completion.chunk",
    }
    return ndjson_line(chunk)


def make_followup_chunk(
    followup_questions: list[str],
    retrieved_docs: list[dict[str, str]],
) -> str:
    """Build the final NDJSON chunk with follow-up questions."""
    chunk = {
        "choices": [
            {
                "delta": {"role": "assistant"},
                "context": {
                    "followup_questions": followup_questions,
                    "retrieved_docs": retrieved_docs,
                },
                "finish_reason": "stop",
                "index": 0,
            }
        ],
        "object": "chat.completion.chunk",
    }
    return ndjson_line(chunk)


def make_error_chunk(message: str) -> str:
    """Build an NDJSON error chunk."""
    return json.dumps({"error": message}, ensure_ascii=False)


async def format_ndjson_stream(stream: AsyncIterator[str]):
    """Yield NDJSON lines from an async stream, catching errors."""
    try:
        async for line in stream:
            yield line
    except Exception as exc:
        yield make_error_chunk(str(exc))
