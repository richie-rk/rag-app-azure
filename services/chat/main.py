"""FastAPI chat service. POST /chat streams an NDJSON response."""

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from services.shared.auth import extract_bearer_token, validate_jwt
from services.shared.authz import user_can_access_index
from services.shared.config import get_settings
from services.shared.database import get_session_factory

from .pipeline.citations import extract_followup_questions
from .pipeline.llm import stream_completion
from .pipeline.prompt import build_messages
from .pipeline.search import hybrid_search
from .schemas import ChatRequest
from .streaming import (
    format_ndjson_stream,
    make_content_chunk,
    make_followup_chunk,
    make_metadata_chunk,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="rag-app-azure Chat Service", version="1.0.0")

# CORS: configurable origins, NOT "*"
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.allowed_origins.split(",") if origin.strip()],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)


# Auth dependency


async def require_auth(request: Request) -> dict:
    """Validate JWT and require a role."""
    token = extract_bearer_token(request.headers.get("Authorization"))
    if not token:
        raise HTTPException(status_code=401, detail="Missing authorization token")
    try:
        claims = validate_jwt(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not claims.get("role"):
        # AAD-authenticated but in neither configured group; see ADR-0003.
        raise HTTPException(status_code=403, detail="Not authorized: no group membership")
    return claims


async def require_non_guest(claims: dict = Depends(require_auth)) -> dict:
    """Reject guest (view-only) users. Chat is not available to them."""
    if claims.get("role") == "guest":
        raise HTTPException(
            status_code=403,
            detail="Read-only access: chat is not available to guest users",
        )
    return claims


# Routes


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/chat")
async def chat(body: ChatRequest, claims: dict = Depends(require_non_guest)):
    """RAG chat endpoint. Returns NDJSON streaming response."""

    query = body.history[-1].user
    overrides = body.overrides
    deployment = body.deployment or settings.default_llm_deployment

    # Step 0: Authorize the requested index. The index name arrives in the
    # request body, so without this check any authenticated user could read
    # any project's documents by guessing/enumerating index names.
    factory = get_session_factory()
    with factory() as session:
        if not user_can_access_index(
            session,
            email=claims["sub"],
            index_name=body.search_index,
            role=claims.get("role"),
        ):
            raise HTTPException(status_code=403, detail="No access to this project")

    # Step 1: Hybrid search
    data_points = hybrid_search(
        query=query,
        index_name=body.search_index,
        top_k=overrides.top_k,
        file_filter=overrides.file_name,
    )

    # Step 2: Build messages
    history_dicts = [{"user": t.user, "bot": t.bot} for t in body.history]
    messages = build_messages(
        user_query=query,
        data_points=data_points,
        history=history_dicts,
        system_prompt_template=overrides.prompt_template,
        suggest_followup=overrides.suggest_followup_questions,
    )

    # Step 3: Stream response
    async def generate():
        # First chunk: metadata + data_points
        yield make_metadata_chunk(data_points, query, deployment)

        # Middle chunks: LLM content deltas. Text before the first "<<"
        # follow-up marker streams out; everything from the marker on is
        # withheld (the final chunk carries the parsed follow-up questions
        # instead). `pending` holds text not yet emitted so that a marker
        # split across two deltas ("<" then "<") is still caught, the text
        # preceding a marker inside a delta is never lost, and a reply that
        # *starts* with "<<" still engages buffering.
        full_content = ""
        pending = ""
        buffering_followup = False

        async for token in stream_completion(
            messages=messages,
            deployment=deployment,
            temperature=overrides.temperature,
        ):
            full_content += token
            if buffering_followup:
                continue

            pending += token
            marker = pending.find("<<")
            if marker != -1:
                if pending[:marker]:
                    yield make_content_chunk(pending[:marker])
                buffering_followup = True
                pending = ""
                continue

            # Hold back a trailing "<" in case the next delta completes "<<".
            if pending.endswith("<"):
                emit, pending = pending[:-1], "<"
            else:
                emit, pending = pending, ""
            if emit:
                yield make_content_chunk(emit)

        # Stream ended without a follow-up marker: flush anything held back.
        if not buffering_followup and pending:
            yield make_content_chunk(pending)

        # Final chunk: follow-up questions parsed from the full content
        _, followup_questions = extract_followup_questions(full_content)

        retrieved_docs = [
            {"sourcepage": dp["sourcepage"], "id": dp["id"], "sourcefile": dp["sourcefile"]}
            for dp in data_points
        ]

        yield make_followup_chunk(followup_questions, retrieved_docs)

    return StreamingResponse(
        format_ndjson_stream(generate()),
        media_type="application/x-ndjson",
    )
