"""`local init` (`86-operations.md` section 2.8.1; `81-config.md` section 2.11)."""

from __future__ import annotations

import stat
import string
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from docchat_core.settings import LocalSettings
from docchat_ops.commands.local import (
    PASSWORD_ALPHABET,
    PASSWORD_SYMBOLS,
    new_password,
    render,
    write_private,
)
from docchat_ops.framework.exitcodes import ExitCode

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from ops.tests.unit.conftest import OpsHarness

PASSWORD_LINE = "DOCCHAT_LOCAL_SQL_SA_PASSWORD="


def _password(ops: OpsHarness) -> str:
    for line in (ops.root / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(PASSWORD_LINE):
            return line.removeprefix(PASSWORD_LINE)
    raise AssertionError("no password line")


def test_password_has_24_characters_of_each_class() -> None:
    for _ in range(200):
        password = new_password()
        assert len(password) == 24
        assert set(password) <= set(PASSWORD_ALPHABET)
        for characters in (
            string.ascii_uppercase,
            string.ascii_lowercase,
            string.digits,
            PASSWORD_SYMBOLS,
        ):
            assert any(char in characters for char in password)
        assert LocalSettings(local_sql_sa_password=SecretStr(password)).local_sql_sa_password


def test_password_is_redrawn_until_every_class_is_present() -> None:
    draws = iter(["a"] * 24 + ["A", "b", "1", "!"] * 6)

    def choice(alphabet: Sequence[str]) -> str:
        assert alphabet == PASSWORD_ALPHABET
        return next(draws)

    assert new_password(choice) == "Ab1!" * 6


def test_symbols_are_the_spec_ones() -> None:
    assert PASSWORD_SYMBOLS == "!#%+-=?@^_"


def test_render_fills_the_password_line() -> None:
    example = "DOCCHAT_LOCAL_EMULATORS=\nDOCCHAT_LOCAL_SQL_SA_PASSWORD=\n"
    assert render(example, "Pw1!") == (
        "DOCCHAT_LOCAL_EMULATORS=\nDOCCHAT_LOCAL_SQL_SA_PASSWORD=Pw1!\n"
    )


def test_render_appends_a_missing_password_line() -> None:
    assert render("DOCCHAT_LOCAL_EMULATORS=\n", "Pw1!") == (
        "DOCCHAT_LOCAL_EMULATORS=\nDOCCHAT_LOCAL_SQL_SA_PASSWORD=Pw1!\n"
    )


def test_write_private_writes_mode_0600_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o644)
    write_private(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env"]


def test_write_private_removes_its_temporary_file_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "missing-directory" / ".env"
    with pytest.raises(FileNotFoundError):
        write_private(target, "x\n")
    assert list(tmp_path.iterdir()) == []


def test_write_private_cleans_up_when_the_move_fails(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.mkdir()
    (target / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(OSError, match=r".*"):
        write_private(target, "x\n")
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env"]


def test_local_init_writes_the_env_file(ops: OpsHarness) -> None:
    outcome = ops.run("--env", "local", "--json", "local", "init")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    env_file = ops.root / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    password = _password(ops)
    assert len(password) == 24
    assert env_file.read_text(encoding="utf-8").startswith("DOCCHAT_LOCAL_EMULATORS=\n")
    assert password not in outcome.stdout
    assert password not in outcome.stderr
    document = outcome.json()
    assert document["result"] == "written"
    assert document["replaced"] is False
    assert ops.az.calls == []
    assert ops.credentials_made == 0


def test_local_init_asks_before_replacing(ops: OpsHarness) -> None:
    (ops.root / ".env").write_text("DOCCHAT_LOCAL_SQL_SA_PASSWORD=keep\n", encoding="utf-8")
    ops.answer("no\n")
    outcome = ops.run("--env", "local", "local", "init")
    assert outcome.code == ExitCode.USAGE
    assert "Type yes to continue: " in outcome.stderr
    assert _password(ops) == "keep"


def test_local_init_replaces_after_yes(ops: OpsHarness) -> None:
    (ops.root / ".env").write_text("DOCCHAT_LOCAL_SQL_SA_PASSWORD=keep\n", encoding="utf-8")
    ops.answer("yes\n")
    outcome = ops.run("--env", "local", "local", "init")
    assert outcome.code == ExitCode.SUCCESS
    assert _password(ops) != "keep"
    assert "replaced: true" in outcome.stdout


def test_local_init_yes_flag_skips_the_prompt(ops: OpsHarness) -> None:
    (ops.root / ".env").write_text("DOCCHAT_LOCAL_SQL_SA_PASSWORD=keep\n", encoding="utf-8")
    outcome = ops.run("--env", "local", "local", "init", "--yes")
    assert outcome.code == ExitCode.SUCCESS
    assert "Type yes" not in outcome.stderr


def test_local_init_needs_the_template(ops: OpsHarness) -> None:
    (ops.root / ".env.example").unlink()
    outcome = ops.run("--env", "local", "local", "init")
    assert outcome.code == ExitCode.USAGE
    assert ".env.example is missing" in outcome.failure


def test_local_init_needs_a_checkout(ops: OpsHarness) -> None:
    (ops.root / "azure.yaml").unlink()
    outcome = ops.run("--env", "local", "local", "init")
    assert outcome.code == ExitCode.USAGE
    assert "no checkout" in outcome.failure
