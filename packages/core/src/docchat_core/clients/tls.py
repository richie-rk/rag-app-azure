"""The process's one TLS context for first-party HTTP clients.

Building a TLS context loads the certificate store, which costs tens of milliseconds; a client
built on its own context does that inside whatever coroutine creates it, long enough to trip the
event-loop guard (`84-testing.md` section 2.8) when a few are created in one step. The context is
built once, at import, outside every event loop, with httpx's default verification (the certifi
store, or `SSL_CERT_FILE` / `SSL_CERT_DIR` when set), and every first-party client passes it as
`verify=`.
"""

from __future__ import annotations

import ssl
from typing import Final

import httpx

TLS_CONTEXT: Final[ssl.SSLContext] = httpx.create_ssl_context()
