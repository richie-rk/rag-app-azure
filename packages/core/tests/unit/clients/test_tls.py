from __future__ import annotations

import ssl
import time

import httpx

from docchat_core.clients.tls import TLS_CONTEXT


def test_the_context_verifies_certificates_and_host_names() -> None:
    assert isinstance(TLS_CONTEXT, ssl.SSLContext)
    assert TLS_CONTEXT.verify_mode is ssl.CERT_REQUIRED
    assert TLS_CONTEXT.check_hostname is True


def test_a_client_on_the_shared_context_is_cheap_to_build() -> None:
    started = time.perf_counter()
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0), follow_redirects=False, verify=TLS_CONTEXT
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05
    assert client._transport._pool._ssl_context is TLS_CONTEXT  # type: ignore[attr-defined]
