"""The model stub's settings and FastAPI application (84-testing.md section 2.3).

An app reaches the stub only because its endpoint setting points here; the stub imports no
first-party package. `StubSettings` is read once, at import, so an invalid value stops Uvicorn
before it binds its port (81-config.md section 6.1).

Every model and screening request is handled in this order: (1) `401` without a bearer header;
(2) parse the body and collect the directives that apply to the request's kind (screening: the
service's limits); (3) record the request; (4) throttle; (5) a queued `shield_prompt` response,
else a `status=` directive; (6) parameter checks and `filter=prompt`; (7) build the output.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import TYPE_CHECKING, Annotated, Any, Final, override

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from docchat_model_stub import chat, directives, embeddings, schema_fill, scripts, shields
from docchat_model_stub.recorder import Recorder, Stats
from docchat_model_stub.throttle import Throttle, retry_headers

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from docchat_model_stub.recorder import DeploymentStats

# The ASGI types, written out so the stub reaches Starlette only through FastAPI.
type _Message = MutableMapping[str, Any]
type _Scope = MutableMapping[str, Any]
type _Receive = Callable[[], Awaitable[_Message]]
type _Send = Callable[[_Message], Awaitable[None]]

CHAT_PATH: Final = "/openai/v1/chat/completions"
EMBEDDINGS_PATH: Final = "/openai/v1/embeddings"
SHIELD_PATH: Final = "/contentsafety/text:shieldPrompt"

DEFAULT_SOURCE_ID_PATTERN: Final = re.compile(r"\[\[BEGIN SOURCE (S\d{1,2}) ")
# The deployment-name rule of the apps' own deployment settings (81-config.md section 2.4).
_DEPLOYMENT_NAME = re.compile(r"[A-Za-z0-9._-]{2,64}")
_MS_PER_SECOND = 1000
_THROTTLED = 429


class StubSettings(BaseSettings):
    """The thirteen `DOCCHAT_STUB_*` settings (81-config.md section 2.6)."""

    model_config = SettingsConfigDict(env_prefix="DOCCHAT_STUB_", extra="forbid", frozen=True)

    workers: int = Field(default=1, ge=1, le=8)
    record: bool = True
    first_token_delay_ms: int = Field(default=300, ge=0, le=60_000)
    token_delay_ms: int = Field(default=10, ge=0, le=5_000)
    tokens_per_chunk: int = Field(default=1, ge=1, le=16)
    answer_tokens: int = Field(default=60, ge=1, le=4_000)
    text_tokens: int = Field(default=40, ge=1, le=4_000)
    json_delay_ms: int = Field(default=20, ge=0, le=60_000)
    embedding_delay_ms: int = Field(default=5, ge=0, le=10_000)
    throttle_every_n: int = Field(default=0, ge=0, le=1_000)
    # A comma-separated list in the environment, not the JSON pydantic-settings expects.
    throttle_deployments: Annotated[tuple[str, ...], NoDecode] = ()
    retry_after_ms: int = Field(default=200, ge=0, le=60_000)
    source_id_pattern: re.Pattern[str] = DEFAULT_SOURCE_ID_PATTERN

    @field_validator("throttle_deployments", mode="before")
    @classmethod
    def _split_names(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(name.strip() for name in value.split(",")) if value.strip() else ()
        return value

    @field_validator("throttle_deployments")
    @classmethod
    def _check_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not _DEPLOYMENT_NAME.fullmatch(name):
                raise ValueError("each entry must be a deployment name")
        return value

    @field_validator("source_id_pattern")
    @classmethod
    def _one_group(cls, value: re.Pattern[str]) -> re.Pattern[str]:
        if value.groups != 1:
            raise ValueError("the pattern must have exactly one capture group")
        return value


class RefusedError(Exception):
    """A request the stub answers with an error before building any output."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__(response.status_code)
        self.response = response


class HangResponse(StreamingResponse):
    """Headers, then nothing until the client disconnects.

    A streaming response sends no `content-length`, so the client keeps waiting for a body.
    """

    def __init__(self, media_type: str, on_disconnect: Callable[[], None]) -> None:
        super().__init__(content=(), media_type=media_type)
        self._on_disconnect = on_disconnect

    @override
    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        # The request body was read before this response was built, so the server has nothing
        # left to deliver but empty end-of-body messages and, at last, the disconnect.
        while (await receive())["type"] != "http.disconnect":
            continue
        self._on_disconnect()


def _error(
    status: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
    **members: Any,
) -> JSONResponse:
    """The Azure-shaped error body `{"error": {"code", "message", ...}}`."""
    return JSONResponse(
        {"error": {"code": code, "message": message, **members}},
        status_code=status,
        headers=headers,
    )


def _invalid_body(message: str = "The request body is invalid.") -> RefusedError:
    # A Pydantic error's text quotes the offending input, which may be test content, so the
    # answer names the problem only in general terms.
    return RefusedError(_error(400, "InvalidRequestBody", message))


def _require_bearer(request: Request) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise RefusedError(_error(401, "401", "A bearer token is required."))


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except ValueError as exc:
        raise _invalid_body("The request body is not JSON.") from exc
    if not isinstance(body, dict):
        raise _invalid_body("The request body is not a JSON object.")
    return body


def _validated[M: BaseModel](model: type[M], body: dict[str, Any]) -> M:
    try:
        return model.model_validate(body)
    except ValueError as exc:
        raise _invalid_body() from exc


def _collect(texts: Iterable[str], kind: directives.Kind) -> directives.Directives:
    try:
        return directives.collect(texts, kind)
    except directives.UnknownDirectiveError as exc:
        raise RefusedError(
            _error(400, "unknown_stub_directive", f"Unknown stub directive {exc.token}.")
        ) from exc


async def _wait_ms(milliseconds: int) -> None:
    await asyncio.sleep(milliseconds / _MS_PER_SECOND)


def _count_disconnect(stats: DeploymentStats) -> None:
    stats.client_disconnected += 1


def _ignore_disconnect() -> None:
    """Screening requests have no deployment, so no counter records their disconnects."""


class StubService:
    """The handlers, holding the per-process state every request shares."""

    def __init__(self, settings: StubSettings) -> None:
        self.settings = settings
        self.recorder = Recorder(enabled=settings.record)
        self.stats = Stats()
        self.throttle = Throttle(
            every_n=settings.throttle_every_n, deployments=frozenset(settings.throttle_deployments)
        )
        self.scripts = scripts.ScriptStore()

    def _refuse_by_status(
        self, found: directives.Directives, stats: DeploymentStats | None
    ) -> None:
        if found.status is None:
            return
        headers = None
        if found.status == _THROTTLED:
            headers = retry_headers(self.settings.retry_after_ms)
            if stats is not None:
                stats.throttled += 1
        raise RefusedError(
            _error(found.status, str(found.status), "stub directive", headers=headers)
        )

    def _admit(
        self, path: str, deployment: str, body: dict[str, Any], found: directives.Directives
    ) -> DeploymentStats:
        """Steps 3 to 5 of a model request: record, count, throttle and the `status=` directive."""
        self.recorder.record(path, deployment, body)
        stats = self.stats.of(deployment)
        stats.started += 1
        if self.throttle.hit(deployment):
            stats.throttled += 1
            raise RefusedError(
                _error(
                    _THROTTLED,
                    str(_THROTTLED),
                    "stub throttle",
                    headers=retry_headers(self.settings.retry_after_ms),
                )
            )
        self._refuse_by_status(found, stats)
        return stats

    async def chat_completions(self, request: Request) -> Response:
        try:
            return await self._chat_completions(request)
        except RefusedError as refused:
            return refused.response

    async def _chat_completions(self, request: Request) -> Response:
        _require_bearer(request)
        body = await _json_object(request)
        chat_request = _validated(chat.ChatRequest, body)
        kind = chat_request.kind()
        found = _collect((chat_request.last_user_text(),), kind)
        stats = self._admit(CHAT_PATH, chat_request.model, body, found)
        problem = chat.parameter_problem(body)
        if problem is not None:
            raise RefusedError(_error(400, problem.code, problem.message, param=problem.param))
        if found.content_filter == "prompt":
            raise RefusedError(
                _error(
                    400,
                    "content_filter",
                    "stub filter",
                    innererror={"code": "ResponsibleAIPolicyViolation"},
                )
            )
        if found.hang:
            media_type = "text/event-stream" if kind == "stream" else "application/json"
            return HangResponse(media_type, functools.partial(_count_disconnect, stats))
        if kind == "stream":
            return self._stream(request, chat_request, found, stats)
        await _wait_ms(self.settings.json_delay_ms if found.first_ms is None else found.first_ms)
        content, completion_tokens = self._content(chat_request)
        stats.completed += 1
        return JSONResponse(chat.completion_body(chat_request, content, completion_tokens))

    def _content(self, chat_request: chat.ChatRequest) -> tuple[str, int]:
        """A structured answer (a queued script, else a schema fill) or a plain-text answer."""
        schema = chat_request.json_schema()
        if schema is None:
            return chat.text_content(self.settings.text_tokens)
        queued, scripted = self.scripts.pop(schema.name)
        if queued:
            return chat.json_content(scripted)
        try:
            return chat.json_content(schema_fill.fill(schema.schema_))
        except schema_fill.SchemaError as exc:
            raise _invalid_body() from exc

    def _stream(
        self,
        request: Request,
        chat_request: chat.ChatRequest,
        found: directives.Directives,
        stats: DeploymentStats,
    ) -> StreamingResponse:
        settings = self.settings
        plan = chat.StreamPlan(
            deployment=chat_request.model,
            prompt_tokens=chat_request.prompt_tokens(),
            words=settings.answer_tokens if found.tokens is None else found.tokens,
            words_per_chunk=settings.tokens_per_chunk,
            first_delay_ms=(
                settings.first_token_delay_ms if found.first_ms is None else found.first_ms
            ),
            word_delay_ms=settings.token_delay_ms if found.delay_ms is None else found.delay_ms,
            citations=tuple(chat.cited_sources(chat_request, settings.source_id_pattern)),
            include_usage=chat_request.include_usage() and not found.no_usage,
            finish_reason="content_filter" if found.content_filter == "completion" else "stop",
            cut_after=found.cut,
        )
        headers = {"cache-control": "no-cache"}
        if found.cut is not None:
            # The cut stream ends without its final chunk, and the server then closes the
            # connection instead of keeping it alive.
            headers["connection"] = "close"
        return StreamingResponse(
            chat.stream_events(plan, request.is_disconnected, stats),
            media_type="text/event-stream",
            headers=headers,
        )

    async def create_embeddings(self, request: Request) -> Response:
        try:
            return await self._create_embeddings(request)
        except RefusedError as refused:
            return refused.response

    async def _create_embeddings(self, request: Request) -> Response:
        _require_bearer(request)
        body = await _json_object(request)
        embedding_request = _validated(embeddings.EmbeddingRequest, body)
        found = _collect(embedding_request.texts(), "embed")
        stats = self._admit(EMBEDDINGS_PATH, embedding_request.model, body, found)
        if found.hang:
            return HangResponse("application/json", functools.partial(_count_disconnect, stats))
        await _wait_ms(self.settings.embedding_delay_ms)
        stats.completed += 1
        return JSONResponse(embeddings.response_body(embedding_request))

    async def shield_prompt(self, request: Request) -> Response:
        try:
            return await self._shield_prompt(request)
        except RefusedError as refused:
            return refused.response

    async def _shield_prompt(self, request: Request) -> Response:
        _require_bearer(request)
        body = await _json_object(request)
        shield_request = _validated(shields.ShieldRequest, body)
        found = _collect(shield_request.texts(), "shield")
        try:
            shields.check_limits(shield_request)
        except shields.ShieldLimitError as exc:
            raise _invalid_body(str(exc)) from exc
        self.recorder.record(SHIELD_PATH, None, body)
        queued, scripted = self.scripts.pop(scripts.SHIELD_PROMPT)
        if queued:
            if "status" in scripted:
                return JSONResponse({"error": scripted["error"]}, status_code=scripted["status"])
            return JSONResponse(scripted)
        self._refuse_by_status(found, None)
        if found.hang:
            return HangResponse("application/json", _ignore_disconnect)
        return JSONResponse(shields.analysis(shield_request))

    async def healthz(self) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def get_stats(self) -> JSONResponse:
        return JSONResponse(self.stats.to_json())

    async def get_requests(self, after: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
        return JSONResponse({"requests": [entry.to_json() for entry in self.recorder.after(after)]})

    async def delete_requests(self) -> Response:
        self.recorder.clear()
        return Response(status_code=204)

    async def put_script(self, name: str, request: Request) -> Response:
        try:
            script = _validated(scripts.ScriptBody, await _json_object(request))
            try:
                scripts.validate_responses(name, script.responses)
            except ValueError as exc:
                raise _invalid_body() from exc
        except RefusedError as refused:
            return refused.response
        self.scripts.put(name, script.responses)
        return Response(status_code=204)

    async def delete_scripts(self) -> Response:
        self.scripts.clear()
        return Response(status_code=204)


def create_app(settings: StubSettings) -> FastAPI:
    """The stub application over `settings`; tests build one per settings they need."""
    service = StubService(settings)
    app = FastAPI(title="DocChat model stub", openapi_url=None, docs_url=None, redoc_url=None)
    app.add_api_route(CHAT_PATH, service.chat_completions, methods=["POST"])
    app.add_api_route(EMBEDDINGS_PATH, service.create_embeddings, methods=["POST"])
    app.add_api_route(SHIELD_PATH, service.shield_prompt, methods=["POST"])
    app.add_api_route("/healthz", service.healthz, methods=["GET"])
    app.add_api_route("/_stub/stats", service.get_stats, methods=["GET"])
    app.add_api_route("/_stub/requests", service.get_requests, methods=["GET"])
    app.add_api_route("/_stub/requests", service.delete_requests, methods=["DELETE"])
    app.add_api_route("/_stub/scripts/{name}", service.put_script, methods=["PUT"])
    app.add_api_route("/_stub/scripts", service.delete_scripts, methods=["DELETE"])
    return app


app = create_app(StubSettings())
