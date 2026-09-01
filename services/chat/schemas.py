"""Pydantic request/response models for the chat service."""

from pydantic import BaseModel, Field


# Per-turn text is capped so a caller cannot inflate the outbound Azure OpenAI
# payload (and worker memory) with multi-megabyte turns. ~32k chars is ample
# for a real question or answer while bounding cost.
_MAX_TURN_CHARS = 32_000


class ChatTurn(BaseModel):
    user: str = Field(..., max_length=_MAX_TURN_CHARS)
    bot: str | None = Field(None, max_length=_MAX_TURN_CHARS)


class ChatOverrides(BaseModel):
    # Bounded: these go straight into Azure OpenAI / AI Search calls, so an
    # unbounded top_k is a cost and latency lever for any caller.
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    top_k: int | None = Field(None, ge=1, le=50)
    suggest_followup_questions: bool = True
    prompt_template: str | None = None
    file_name: str | None = None
    rewrite_query: bool = False


class ChatRequest(BaseModel):
    # Bounded turn count: history is duplicated into both role messages and the
    # {chat_history} block, and the whole list is sent to Azure OpenAI on every
    # call, so an unbounded array is a cost/latency/memory lever for any caller.
    history: list[ChatTurn] = Field(..., min_length=1, max_length=100)
    overrides: ChatOverrides = ChatOverrides()
    search_index: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    app: str = "rag-app-azure"
    deployment: str | None = None


class DataPoint(BaseModel):
    content: str
    sourcepage: str
    sourcefile: str
    id: str
    score: float | None = None
