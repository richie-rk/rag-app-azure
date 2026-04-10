"""Pydantic request/response models for the chat service."""

from pydantic import BaseModel, Field


class ChatTurn(BaseModel):
    user: str
    bot: str | None = None


class ChatOverrides(BaseModel):
    temperature: float = 0.0
    top_k: int | None = None
    suggest_followup_questions: bool = True
    prompt_template: str | None = None
    file_name: str | None = None
    rewrite_query: bool = False


class ChatRequest(BaseModel):
    history: list[ChatTurn] = Field(..., min_length=1)
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
