"""The request-size and content-type guard (`11-api.md` sections 1.3 step 1(c), 1.7 and 1.11;
`82-security.md` SEC-23).

`BodyGuardMiddleware` wraps `receive` and acts only at the first read of the body, after the route
class has run the pre-body checks and placed the matched route's options in the scope, so a
refused request's body is never read and a request without a token answers `401`, never `413`.
At that first read it refuses a `Content-Type` outside the route's `content_types` with
`415 unsupported_media_type` (`application/json` only with the charset parameter absent or
`utf-8`), compares `Content-Length` with the route's `body_limit` before reading, and counts the
bytes as they arrive; either size breach is `413 request_too_large`. A body sent with no
`Content-Type` at all is refused too; an empty one passes, so a missing required body is the
validator's `422`.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from docchat_api.http.context import ROUTE_OPTIONS_KEY
from docchat_api.http.routing import DEFAULT_ROUTE_OPTIONS, JSON_MEDIA_TYPE, RouteOptions
from docchat_core.errors import DocChatProblem

UTF_8: Final = "utf-8"


class RequestTooLarge(DocChatProblem):
    """`413 request_too_large`: the body exceeds the route's limit, declared or streamed."""

    code = "request_too_large"


class UnsupportedMediaType(DocChatProblem):
    """`415 unsupported_media_type`, with one text for every route."""

    code = "unsupported_media_type"


def media_type_allowed(content_type: str, allowed: tuple[str, ...]) -> bool:
    """`content_type` names one of the route's media types; a JSON body may name only the
    `charset` parameter, as `utf-8`."""
    media_type, *parameters = (part.strip() for part in content_type.split(";"))
    if media_type.lower() not in allowed:
        return False
    named: dict[str, str] = {}
    for parameter in parameters:
        name, equals, value = parameter.partition("=")
        if not equals or not name.strip():
            return False
        named[name.strip().lower()] = value.strip().strip('"').lower()
    if media_type.lower() == JSON_MEDIA_TYPE:
        return set(named) <= {"charset"} and named.get("charset", UTF_8) == UTF_8
    return True


def _declared_length(headers: Headers) -> int | None:
    value = headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class _GuardedReceive:
    def __init__(self, scope: MutableMapping[str, Any], receive: Receive) -> None:
        self._scope = scope
        self._receive = receive
        self._headers = Headers(scope=scope)
        self._started = False
        self._received = 0
        self._options: RouteOptions = DEFAULT_ROUTE_OPTIONS

    def _first_read(self) -> None:
        options = self._scope.get(ROUTE_OPTIONS_KEY)
        if isinstance(options, RouteOptions):
            self._options = options
        content_type = self._headers.get("content-type")
        if content_type is not None and not media_type_allowed(
            content_type, self._options.content_types
        ):
            raise UnsupportedMediaType()
        declared = _declared_length(self._headers)
        limit = self._options.body_limit
        if limit is not None and declared is not None and declared > limit:
            raise RequestTooLarge()

    async def __call__(self) -> Message:
        first = not self._started
        if first:
            self._started = True
            self._first_read()
        message = await self._receive()
        if message["type"] != "http.request":
            return message
        chunk = message.get("body", b"")
        if first and chunk and "content-type" not in self._headers:
            raise UnsupportedMediaType()
        self._received += len(chunk)
        limit = self._options.body_limit
        if limit is not None and self._received > limit:
            raise RequestTooLarge()
        return message


class BodyGuardMiddleware:
    """Installs the guarded `receive` on every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self.app(scope, _GuardedReceive(scope, receive), send)
