from __future__ import annotations

import unicodedata
from urllib.parse import unquote

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from docchat_api.http.headers import (
    DOCUMENT_CSP,
    FILE_CSP,
    SecurityHeadersMiddleware,
    content_disposition,
)
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

COMMON = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "strict-transport-security": "max-age=31536000",
}


def _assert_common(response: httpx.Response) -> None:
    for name, value in COMMON.items():
        assert response.headers[name] == value, name
    for absent in ("server", "etag", "content-encoding", "location"):
        assert absent not in response.headers, absent
    assert not any(name.startswith("access-control-") for name in response.headers)


async def test_t057_every_response_carries_the_common_headers(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    gzip = {"Accept-Encoding": "gzip, br"}
    ok = await started.client.get("/v1/me", headers=alice.authorization() | gzip)
    refused = await started.client.get("/v1/me", headers=gzip)
    failed = await started.client.get("/v1/probe/boom", headers=alice.authorization())
    health = await started.client.get("/healthz")

    assert [ok.status_code, refused.status_code, failed.status_code, health.status_code] == [
        200,
        401,
        500,
        200,
    ]
    for response in (ok, refused, failed, health):
        _assert_common(response)
    for response in (ok, refused, failed):
        assert response.headers["content-security-policy"] == DOCUMENT_CSP
    assert "content-security-policy" not in health.headers


def _app() -> Starlette:
    async def file(request: Request) -> Response:
        return Response(
            b"%PDF", media_type="application/pdf", headers={"Content-Security-Policy": FILE_CSP}
        )

    async def ndjson(request: Request) -> Response:
        return Response(b"{}\n", media_type="application/x-ndjson")

    async def cached(request: Request) -> Response:
        return PlainTextResponse("x", headers={"Cache-Control": "max-age=60"})

    return Starlette(
        routes=[Route("/file", file), Route("/ndjson", ndjson), Route("/cached", cached)]
    )


async def test_a_file_keeps_its_sandbox_and_ndjson_gets_the_document_policy() -> None:
    transport = httpx.ASGITransport(app=SecurityHeadersMiddleware(_app()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        file = await client.get("/file")
        ndjson = await client.get("/ndjson")
        cached = await client.get("/cached")

    assert file.headers["content-security-policy"] == FILE_CSP
    assert ndjson.headers["content-security-policy"] == DOCUMENT_CSP
    assert cached.headers["cache-control"] == "no-store"
    assert "content-security-policy" not in cached.headers


def _parts(header: str) -> tuple[str, str, str]:
    disposition, filename, encoded = (part.strip() for part in header.split(";"))
    assert filename.startswith('filename="')
    assert filename.endswith('"')
    assert encoded.startswith("filename*=UTF-8''")
    return disposition, filename[len('filename="') : -1], encoded[len("filename*=UTF-8''") :]


def test_t058_the_disposition_is_always_attachment() -> None:
    disposition, fallback, encoded = _parts(content_disposition("report.pdf"))

    assert (disposition, fallback, encoded) == ("attachment", "report.pdf", "report.pdf")


def test_t058_a_long_name_keeps_its_extension_in_200_code_points() -> None:
    name = "é" * 256 + ".pdf"

    _, fallback, encoded = _parts(content_disposition(name))
    decoded = unquote(encoded, encoding="utf-8")

    assert len(decoded) == 200
    assert decoded.endswith(".pdf")
    assert len(fallback) == 200
    assert fallback.endswith(".pdf")


@pytest.mark.parametrize(
    ("name", "fallback"),
    [
        ('a"b\\c/d;e%f.pdf', "a_b_c_d_e_f.pdf"),
        ("Résumé 2026.docx", "R_sum_ 2026.docx"),
        ("tab\there.pdf", "tab_here.pdf"),
        ("smile😀.pptx", "smile_.pptx"),
    ],
)
def test_t058_the_ascii_fallback_replaces_unsafe_characters(name: str, fallback: str) -> None:
    _, sent, _ = _parts(content_disposition(name))

    assert sent == fallback


def test_t058_the_encoded_name_escapes_outside_attr_char() -> None:
    _, _, encoded = _parts(content_disposition("a b&c'(1)😀.pdf"))

    assert encoded == "a%20b&c%27%281%29%F0%9F%98%80.pdf"


def test_t058_a_name_without_a_dot_is_cut_to_200() -> None:
    _, fallback, encoded = _parts(content_disposition("x" * 260))

    assert fallback == "x" * 200
    assert encoded == "x" * 200


def test_the_name_is_normalised_to_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "é.pdf")

    _, _, encoded = _parts(content_disposition(decomposed))

    assert encoded == "%C3%A9.pdf"
