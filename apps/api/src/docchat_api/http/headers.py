"""The response headers of every route and the file responses' `Content-Disposition`
(`11-api.md` section 1.10; `82-security.md` SEC-59 and SEC-60).

`SecurityHeadersMiddleware` adds to every response the app sends `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and
`Strict-Transport-Security: max-age=31536000`, and to every JSON, problem and NDJSON response
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`. A file response sets its own
`Content-Security-Policy: sandbox`, which the middleware keeps. The app sends no `Server`, `ETag`,
`Location`, CORS or `Content-Encoding` header: the worker class turns the server header off, version
tokens travel in bodies, platform CORS answers preflights, and no compression middleware exists.

`content_disposition(name)` is the only builder of a file response's `Content-Disposition`
(RFC 6266 with RFC 8187), always `attachment`, so a response opened by direct navigation never
renders in the API's origin.
"""

from __future__ import annotations

import unicodedata
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COMMON_HEADERS: Final = (
    ("cache-control", "no-store"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("strict-transport-security", "max-age=31536000"),
)
DOCUMENT_CSP: Final = "default-src 'none'; frame-ancestors 'none'"
FILE_CSP: Final = "sandbox"
CSP_MEDIA_TYPES: Final = frozenset(
    {"application/json", "application/problem+json", "application/x-ndjson"}
)
"""The media types of JSON, problem and NDJSON responses."""

FILENAME_MAX_CODE_POINTS: Final = 200
# RFC 8187 `attr-char`: ALPHA, DIGIT and these, sent as they are; every other byte is %XX.
_ATTR_CHAR_SYMBOLS: Final = frozenset("!#$&+-.^_`|~")
_FALLBACK_REPLACED: Final = frozenset('"\\/;%')
_PRINTABLE_ASCII: Final = range(0x20, 0x7F)


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


class SecurityHeadersMiddleware:
    """Adds the headers of section 1.10 at each response start."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in COMMON_HEADERS:
                    headers[name] = value
                if (
                    "content-security-policy" not in headers
                    and _media_type(headers.get("content-type", "")) in CSP_MEDIA_TYPES
                ):
                    headers["content-security-policy"] = DOCUMENT_CSP
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _cut(name: str) -> str:
    """The NFC name cut to 200 code points, shortening the part before the last `.` so the
    extension is kept; a name without `.` is cut at 200."""
    if len(name) <= FILENAME_MAX_CODE_POINTS:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or len(extension) + 1 >= FILENAME_MAX_CODE_POINTS:
        return name[:FILENAME_MAX_CODE_POINTS]
    return stem[: FILENAME_MAX_CODE_POINTS - len(extension) - 1] + dot + extension


def _encoded(name: str) -> str:
    return "".join(
        chr(byte)
        if chr(byte).isascii() and (chr(byte).isalnum() or chr(byte) in _ATTR_CHAR_SYMBOLS)
        else f"%{byte:02X}"
        for byte in name.encode("utf-8")
    )


def _fallback(name: str) -> str:
    return "".join(
        "_" if ord(char) not in _PRINTABLE_ASCII or char in _FALLBACK_REPLACED else char
        for char in name
    )


def content_disposition(name: str) -> str:
    """`attachment; filename="<fallback>"; filename*=UTF-8''<encoded>` for a file named `name`."""
    cut = _cut(unicodedata.normalize("NFC", name))
    return f"attachment; filename=\"{_fallback(cut)}\"; filename*=UTF-8''{_encoded(cut)}"
