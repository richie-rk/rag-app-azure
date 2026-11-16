"""The bootstrap script tests' fixture. The scripts live in scripts/bootstrap, outside every
workspace package, so their directory and this one go on sys.path before any test imports them."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
# The harness sits in its own directory so that mypy can name it without a second `conftest`.
for _path in (_HERE / "harness", _HERE.parents[3] / "scripts" / "bootstrap"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bootstrap_harness import Harness, new_harness  # noqa: E402 - needs the paths above


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return new_harness(tmp_path)
