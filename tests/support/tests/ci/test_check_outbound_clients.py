"""T-514: the `check_outbound_clients.py` fixture tests (01-repository.md sections 2.3 and 6.4)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_CI = str(REPOSITORY_ROOT / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_outbound_clients  # noqa: E402

PYPROJECT = """
[tool.uv.workspace]
members = ["packages/core", "apps/api", "apps/ingestion", "scripts/ops", "tests/support",
           "tests/model-stub", "db"]
"""

GOOD = {
    "packages/core/src/docchat_core/clients/__init__.py": """
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import ManagedIdentityCredential
        from azure.storage.blob.aio import BlobServiceClient

        def build(url: str, credential: ManagedIdentityCredential) -> object:
            blobs = BlobServiceClient(url, credential=credential, connection_timeout=10,
                                      read_timeout=60)
            cosmos = CosmosClient(url, credential=credential, connection_timeout=5)
            return blobs, cosmos
        """,
    "packages/core/src/docchat_core/clients/openai.py": """
        import openai

        def profile(url: str) -> object:
            return openai.AsyncOpenAI(base_url=url, max_retries=0)
        """,
    "packages/core/src/docchat_core/sql/engine.py": """
        from azure.identity.aio import ManagedIdentityCredential
        """,
    "packages/core/src/docchat_core/search/filters.py": """
        from azure.search.documents.models import VectorizedQuery

        def query(vector: list[float]) -> object:
            return VectorizedQuery(vector=vector, k_nearest_neighbors=50, fields="vector")
        """,
    "apps/api/src/docchat_api/auth_keys.py": """
        import httpx

        def client() -> httpx.AsyncClient:
            return httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                     follow_redirects=False)
        """,
    "scripts/ops/src/docchat_ops/framework/auth.py": """
        from azure.identity.aio import AzureCliCredential
        from azure.keyvault.secrets.aio import SecretClient

        def secrets(url: str) -> SecretClient:
            return SecretClient(url, AzureCliCredential())
        """,
    "scripts/ci/fetch.py": """
        from urllib.request import urlopen

        def get(url: str) -> bytes:
            with urlopen(url, timeout=15) as response:
                return response.read()
        """,
    "tests/load/aci/run.py": """
        import urllib.request

        def download(url: str) -> bytes:
            return urllib.request.urlopen(url, timeout=120).read()
        """,
}

BAD = [
    (
        "apps/api/src/docchat_api/shields.py",
        """
        import httpx

        CLIENT = httpx.AsyncClient()
        """,
        3,
        "httpx.AsyncClient( without timeout=",
    ),
    (
        "apps/ingestion/src/docchat_ingestion/fetch.py",
        """
        import httpx as h

        CLIENT = h.AsyncClient(timeout=5.0, follow_redirects=True)
        """,
        3,
        "httpx.AsyncClient( must not follow redirects",
    ),
    (
        "packages/core/src/docchat_core/auth/keys.py",
        """
        from httpx import AsyncClient

        def make(options: dict[str, object]) -> AsyncClient:
            return AsyncClient(**options)
        """,
        4,
        "httpx.AsyncClient( without timeout=",
    ),
    (
        "scripts/ci/download.py",
        """
        import urllib.request

        def get(url: str) -> bytes:
            return urllib.request.urlopen(url).read()
        """,
        4,
        "urlopen( without timeout=",
    ),
    (
        "tests/load/aci/run.py",
        """
        from urllib import request

        def get(url: str) -> bytes:
            return request.urlopen(url).read()
        """,
        4,
        "urlopen( without timeout=",
    ),
    (
        "packages/core/src/docchat_core/clients/search.py",
        """
        from azure.search.documents.aio import SearchClient

        def search_client(endpoint: str, index: str, credential: object) -> SearchClient:
            return SearchClient(endpoint, index, credential, connection_timeout=10)
        """,
        4,
        "azure.search.documents.aio.SearchClient built without read_timeout=",
    ),
    (
        "packages/core/src/docchat_core/clients/cosmos.py",
        """
        import azure.cosmos.aio

        def cosmos(url: str, credential: object) -> object:
            return azure.cosmos.aio.CosmosClient(url, credential=credential)
        """,
        4,
        "azure.cosmos.aio.CosmosClient built without connection_timeout=",
    ),
    (
        "apps/api/src/docchat_api/routes/citations.py",
        """
        from azure.storage.blob.aio import BlobServiceClient

        def blobs(url: str, credential: object) -> BlobServiceClient:
            return BlobServiceClient(url, credential=credential, connection_timeout=10,
                                     read_timeout=60)
        """,
        4,
        "azure.storage.blob.aio.BlobServiceClient built outside docchat_core.clients",
    ),
    (
        "apps/ingestion/src/docchat_ingestion/activities/upload.py",
        """
        from azure.storage.blob.aio import ContainerClient

        def container(text: str) -> ContainerClient:
            return ContainerClient.from_connection_string(text, "documents")
        """,
        4,
        "azure.storage.blob.aio.ContainerClient built outside docchat_core.clients",
    ),
    (
        "packages/core/src/docchat_core/chat/models.py",
        """
        from openai import AsyncOpenAI

        def model() -> AsyncOpenAI:
            return AsyncOpenAI(max_retries=0)
        """,
        4,
        "openai.AsyncOpenAI built outside docchat_core.clients",
    ),
    (
        "apps/api/src/docchat_api/routes/me.py",
        """
        from azure.identity.aio import ManagedIdentityCredential
        """,
        1,
        "docchat_api.routes.me imports azure.identity.aio; only docchat_core.clients",
    ),
    (
        "packages/core/src/docchat_core/access/cache.py",
        """
        import azure.identity
        """,
        1,
        "docchat_core.access.cache imports azure.identity;",
    ),
    (
        "tests/model-stub/src/docchat_model_stub/app.py",
        """
        from azure import identity
        """,
        1,
        "docchat_model_stub.app imports azure.identity;",
    ),
]


def _write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _workspace(root: Path) -> Path:
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    for path, source in GOOD.items():
        _write(root, path, source)
    return root


def test_t514_calls_written_as_required_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)

    assert check_outbound_clients.main(["--root", str(root)]) == 0
    assert "import contract 6 holds" in capsys.readouterr().out


@pytest.mark.parametrize("case", BAD, ids=[case[0] for case in BAD])
def test_t514_each_breach_fails_naming_file_and_line(
    tmp_path: Path, case: tuple[str, str, int, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path, source, line, message = case
    root = _workspace(tmp_path)
    _write(root, path, source)

    assert check_outbound_clients.main(["--root", str(root)]) == 1
    output = capsys.readouterr().out.splitlines()
    assert any(entry.startswith(f"{path}:{line}: {message}") for entry in output), output
    assert len(output) == 1


def test_t514_the_repository_passes() -> None:
    assert check_outbound_clients.main(["--root", str(REPOSITORY_ROOT)]) == 0


def test_a_file_that_does_not_parse_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _write(root, "apps/api/src/docchat_api/broken.py", "def broken(:\n")

    breaches = check_outbound_clients.check_repository(root)

    assert [(breach.path, breach.message.split(":")[0]) for breach in breaches] == [
        ("apps/api/src/docchat_api/broken.py", "can't parse")
    ]


def test_tests_and_tools_may_build_their_own_clients(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _write(
        root,
        "tests/support/src/docchat_testing/tokens.py",
        """
        from azure.identity.aio import ClientAssertionCredential
        from azure.keyvault.secrets.aio import SecretClient

        def vault(url: str, credential: ClientAssertionCredential) -> SecretClient:
            return SecretClient(url, credential, connection_timeout=5, read_timeout=30)
        """,
    )

    assert check_outbound_clients.check_repository(root) == []
