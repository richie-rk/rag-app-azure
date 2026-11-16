"""T-113, the version pin unit test (`80-data-model.md` section 1.3 and acceptance criterion 42).

`uv.lock` and the image tags of `compose.yaml` and the CI workflows carry exactly the versions of
section 1.3. The table is read from `docs/spec/80-data-model.md` when the test runs, so a change
to the specification is a change to the test.

The lock resolves members' `>=<confirmed>,<next major>` ranges (`01-repository.md` section 3.3),
and five packages resolved a newer release than section 1.3 names;
those rows are marked `xfail(strict=True)`, so each one fails the test the day the lock and the
table agree.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

import pytest
import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
DATA_MODEL: Final = REPOSITORY_ROOT / "docs" / "spec" / "80-data-model.md"
WORKFLOWS: Final = REPOSITORY_ROOT / ".github" / "workflows"

PACKAGES: Final = {
    "SQLAlchemy": "sqlalchemy",
    "Alembic": "alembic",
    "pyodbc": "pyodbc",
    "Pydantic": "pydantic",
    "azure-cosmos": "azure-cosmos",
    "azure-storage-blob": "azure-storage-blob",
    "azure-storage-queue": "azure-storage-queue",
    "azure-search-documents": "azure-search-documents",
}
"""Section 1.3's Python components and their distribution names in `uv.lock`."""

IMAGES: Final = {
    "SQL Server image (local and CI)": "sqlserver",
    "Cosmos DB emulator (local and CI)": "cosmos",
    "Azurite (local and CI)": "azurite",
}
"""Section 1.3's images and the `compose.yaml` services that run them."""

LOCK_DEVIATION: Final = (
    "known deviation (locked versions above the confirmed pins): members declare "
    ">=<confirmed>,<next major> and the lock resolved a newer release"
)
DEVIATING: Final = frozenset(
    {"sqlalchemy", "alembic", "pydantic", "azure-storage-blob", "azure-storage-queue"}
)

_CODE_RE: Final = re.compile(r"`([^`]+)`")
_VERSION_RE: Final = re.compile(r"\A(\d+(?:\.\d+)+)")


def versions_table() -> dict[str, str]:
    """Section 1.3: component → its version cell."""
    lines = DATA_MODEL.read_text(encoding="utf-8").splitlines()
    start = lines.index("### 1.3 Versions")
    rows: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.startswith("### "):
            break
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] != "Component":
            rows[cells[0]] = cells[1]
    return rows


def locked_versions() -> dict[str, str]:
    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package.get("version", "") for package in lock["package"]}


def _package_rows() -> list[object]:
    table = versions_table()
    rows: list[object] = []
    for component, distribution in PACKAGES.items():
        match = _VERSION_RE.match(table[component])
        assert match is not None, component
        marks = (
            [pytest.mark.xfail(strict=True, reason=LOCK_DEVIATION)]
            if distribution in DEVIATING
            else []
        )
        rows.append(pytest.param(distribution, match.group(1), marks=marks, id=distribution))
    return rows


def _image_rows() -> list[tuple[str, str]]:
    table = versions_table()
    return [
        (service, _CODE_RE.findall(table[component])[0]) for component, service in IMAGES.items()
    ]


def _repository_and_tag(reference: str) -> tuple[str, str]:
    """`registry/repository:tag[@digest]` → (`registry/repository`, `tag`)."""
    without_digest = reference.split("@", 1)[0]
    repository, _, tag = without_digest.rpartition(":")
    return repository, tag


# T-113


def test_t113_the_table_names_every_component_the_test_checks() -> None:
    assert set(PACKAGES) | set(IMAGES) <= set(versions_table())


@pytest.mark.parametrize(("distribution", "version"), _package_rows())
def test_t113_the_lock_pins_each_package_at_its_version(distribution: str, version: str) -> None:
    assert locked_versions().get(distribution) == version


@pytest.mark.parametrize(("service", "image"), _image_rows())
def test_t113_each_compose_image_carries_its_tag(service: str, image: str) -> None:
    compose = yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert _repository_and_tag(compose["services"][service]["image"]) == _repository_and_tag(image)


@pytest.mark.parametrize(("service", "image"), _image_rows())
def test_t113_every_workflow_reference_to_an_image_carries_its_tag(
    service: str, image: str
) -> None:
    repository, tag = _repository_and_tag(image)
    pattern = re.compile(rf"{re.escape(repository)}:(?P<tag>[A-Za-z0-9._-]+)")
    references = [
        (path.name, match.group("tag"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    ]

    assert [(name, found) for name, found in references if found != tag] == [], service
