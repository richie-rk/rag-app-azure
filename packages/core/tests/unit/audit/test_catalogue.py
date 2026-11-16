"""Unit tests of the audit catalogues (`80-data-model.md` section 2.21; T-078 unit part)."""

from __future__ import annotations

import re
from pathlib import Path

from docchat_core.audit.catalogue import (
    ACTIONS_WITHOUT_METADATA,
    ACTIONS_WITHOUT_TARGET_ID,
    AUDIT_ACTIONS,
    AUDIT_ACTOR_TYPES,
    AUDIT_TARGET_TYPES,
)

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def catalogue_section() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return text[text.index("### 2.21 ") : text.index("### 2.22 ")]
    raise FileNotFoundError("80-data-model.md")


def test_forty_three_actions_of_the_data_model() -> None:
    section = catalogue_section()
    documented = re.findall(r"^\| `([a-z_]+\.[a-z_]+)` \|", section, flags=re.MULTILINE)
    assert len(AUDIT_ACTIONS) == 43
    assert tuple(documented) == AUDIT_ACTIONS
    for action in AUDIT_ACTIONS:
        assert _ACTION_RE.fullmatch(action)
        assert len(action) <= 64


def test_eighteen_target_types_of_the_data_model() -> None:
    section = catalogue_section()
    line = next(row for row in section.splitlines() if row.startswith("**Target types** (18)"))
    documented = re.findall(r"`([a-z_]+)`", line)
    # The line names `user` and `document` twice (shared by two areas).
    assert set(documented) == set(AUDIT_TARGET_TYPES)
    assert len(AUDIT_TARGET_TYPES) == len(set(AUDIT_TARGET_TYPES)) == 18
    for target in AUDIT_TARGET_TYPES:
        assert re.fullmatch(r"[a-z][a-z_]*", target)
        assert len(target) <= 32


def test_actor_types_and_special_actions() -> None:
    assert AUDIT_ACTOR_TYPES == ("user", "operator", "system")
    assert set(AUDIT_ACTIONS) >= ACTIONS_WITHOUT_METADATA | ACTIONS_WITHOUT_TARGET_ID
