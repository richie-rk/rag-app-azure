"""FastAPI chat service. POST /chat streams an NDJSON response."""

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from services.shared.auth import extract_bearer_token, validate_jwt
from services.shared.config import get_settings

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


# Routes


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/chat")
async def chat(body: ChatRequest, claims: dict = Depends(require_auth)):
    """RAG chat endpoint. Returns NDJSON streaming response."""

    query = body.history[-1].user
    overrides = body.overrides
    deployment = body.deployment or settings.default_llm_deployment

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

        # Middle chunks: LLM content deltas
        full_content = ""
        followup_buffer = ""
        buffering_followup = False

        async for token in stream_completion(
            messages=messages,
            deployment=deployment,
            temperature=overrides.temperature,
        ):
            full_content += token

            # Check for follow-up question markers
            if "<<" in token and not buffering_followup:
                # Split at the marker
                pre, _, post = full_content.rpartition("<<")
                if pre and not buffering_followup:
                    buffering_followup = True
                    followup_buffer = "<<" + post
                    continue

            if buffering_followup:
                followup_buffer += token
                continue

            yield make_content_chunk(token)

        # Final chunk: follow-up questions
        if followup_buffer:
            full_for_followup = full_content
        else:
            full_for_followup = full_content

        _, followup_questions = extract_followup_questions(full_for_followup)

        retrieved_docs = [
            {"sourcepage": dp["sourcepage"], "id": dp["id"], "sourcefile": dp["sourcefile"]}
            for dp in data_points
        ]

        yield make_followup_chunk(followup_questions, retrieved_docs)

    return StreamingResponse(
        format_ndjson_stream(generate()),
        media_type="application/x-ndjson",
    )
