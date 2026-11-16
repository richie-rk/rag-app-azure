"""T-523: `check_image_contents.py` over exported image file systems built in memory
(`84-testing.md` section 5.11; `01-repository.md` section 4.1 rule 7)."""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_image_contents  # noqa: E402

SITE = "opt/venv/lib/python3.13/site-packages"
CLEAN_API = (
    "app/gunicorn.conf.py",
    f"{SITE}/docchat_api/app.py",
    f"{SITE}/docchat_api/chat/stream.py",
    f"{SITE}/docchat_core/clients/tls.py",
    f"{SITE}/docchat_api-0.1.0.dist-info/RECORD",
    # Third-party distributions keep their own tests; the rule is about the build context.
    f"{SITE}/anyio/tests/test_taskgroups.py",
    f"{SITE}/pydantic/conftest.py",
    f"{SITE}/somepackage/static/app.js.map",
    "opt/docchat/tiktoken/9b5ad71b2ce5302211f9c61530b329a4922fc6a4",
    "usr/lib/python3/dist-packages/test_helper.py",
)


def exported(paths: tuple[str, ...] | list[str]) -> io.BytesIO:
    """A tar stream like the one `docker export` writes, one empty file per path."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in paths:
            archive.addfile(tarfile.TarInfo(path), io.BytesIO(b""))
    buffer.seek(0)
    return buffer


def test_t523_an_image_holding_only_what_its_build_copies_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_image_contents.main(["--image", "docchat/api"], exported(CLEAN_API)) == 0
    assert f"among {len(CLEAN_API)} paths" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("app/.env", ".env"),
        ("home/site/wwwroot/.env", ".env"),
        ("root/.azure/dev/.env", ".env"),
        ("root/.azure/config", ".azure/"),
        (f"{SITE}/docchat_testing/plugin.py", "the test-support package"),
        (f"{SITE}/docchat_testing-0.1.0.dist-info/METADATA", "the test-support package"),
        (f"{SITE}/docchat_api/chat/stream.js.map", "a source map"),
        ("app/static/main.css.map", "a source map"),
        (f"{SITE}/docchat_core/tests/unit/test_models.py", "a test file"),
        (f"{SITE}/docchat_ingestion/test_dispatcher.py", "a test file"),
        (f"{SITE}/docchat_api/conftest.py", "a test file"),
        ("home/site/wwwroot/tests/fixtures/tables.pdf", "a test file"),
        ("app/routes_test.py", "a test file"),
    ],
)
def test_t523_each_forbidden_path_is_named_with_its_reason(path: str, reason: str) -> None:
    assert check_image_contents.find_forbidden([*CLEAN_API, path]) == [(path, reason)]


def test_t523_a_forbidden_path_fails_the_image_naming_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = exported([*CLEAN_API, "./app/.env", f"{SITE}/docchat_api/tests/test_app.py"])
    assert check_image_contents.main(["--image", "docchat/api"], stream) == 1
    errors = capsys.readouterr().err
    assert "::error::docchat/api holds .env: /app/.env" in errors
    assert f"holds a test file: /{SITE}/docchat_api/tests/test_app.py" in errors


def test_t523_input_that_isnt_a_tar_stream_exits_2() -> None:
    stream = io.BytesIO(b"not a tar stream")
    assert check_image_contents.main(["--image", "docchat/api"], stream) == 2
