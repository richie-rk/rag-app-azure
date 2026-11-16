"""`apps/ingestion/image/check_readable_files.py`: the build-time check that no file a parse user
can read holds a secret (`14-ingestion.md` section 15.2 stage `runtime` step 5; T-619). It runs
over a test layer standing in for the image's root; the script is loaded by its path."""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "image" / "check_readable_files.py"
SECRET = "AKIAIOSFODNN7EXAMPLE"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_readable_files", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_readable_files"] = module
    spec.loader.exec_module(module)
    return module


check = load()


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


@pytest.fixture
def layer(tmp_path: Path) -> Path:
    """An image-like tree: packages, code and models below the allowed directories, a few
    harmless files elsewhere, and a secret only root can read."""
    root = tmp_path / "root"
    write(root / "usr" / "lib" / "x.pem", "-----BEGIN PRIVATE KEY-----")
    write(root / "opt" / "venv" / "lib" / "credentials", "anything")
    write(root / "home" / "site" / "wwwroot" / "host.json", "{}")
    write(root / "etc" / "hostname", "container")
    write(root / "etc" / "passwd", "root:x:0:0::/root:/bin/sh")
    write(root / "root" / ".ssh" / "id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----", 0o600)
    write(root / "var" / "log" / "build.log", "nothing to see")
    (root / "proc").mkdir()
    (root / "etc" / "link.pem").symlink_to(root / "usr" / "lib" / "x.pem")
    return root


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "gitleaks.toml"
    path.write_text(
        "[[rules]]\n"
        'id = "aws-access-token"\n'
        "regex = '''(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}'''\n"
        'keywords = ["akia"]\n'
    )
    return path


def run(root: Path, config: Path, *extra: str) -> int:
    code: int = check.main(["--root", str(root), "--config", str(config), *extra])
    return code


def test_t619_the_image_file_list_passes(layer: Path, config: Path) -> None:
    assert run(layer, config) == 0


def test_t619_a_world_readable_pem_under_srv_fails_and_is_named(
    layer: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(layer / "srv" / "tls.pem", "certificate")

    assert run(layer, config) == 1
    assert "/srv/tls.pem (key file)" in capsys.readouterr().err


def test_t619_a_world_readable_file_matching_a_gitleaks_rule_fails(
    layer: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(layer / "var" / "cache" / "notes.txt", f"aws key {SECRET} kept here")

    assert run(layer, config) == 1
    assert "/var/cache/notes.txt (gitleaks rule aws-access-token)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("relative", "text", "reason"),
    [
        ("srv/app/.env", "A=1", ".env file"),
        ("srv/app/prod.env", "A=1", ".env file"),
        ("srv/app/credentials", "x", "credentials file"),
        ("srv/keys/site.p12", "x", "key file"),
        ("srv/keys/site.KEY", "x", "key file"),
        ("srv/keys/service", "-----BEGIN RSA PRIVATE KEY-----\nabc", "private key"),
    ],
)
def test_t619_keys_env_files_and_credentials_fail(  # noqa: PLR0917 - fixtures and cases
    layer: Path,
    config: Path,
    capsys: pytest.CaptureFixture[str],
    relative: str,
    text: str,
    reason: str,
) -> None:
    write(layer / relative, text)

    assert run(layer, config) == 1
    assert f"/{relative} ({reason})" in capsys.readouterr().err


def test_a_file_others_cant_read_and_an_excluded_directory_are_left_out(
    layer: Path, config: Path
) -> None:
    write(layer / "srv" / "private.pem", "x", 0o600)
    write(layer / "src" / "repository" / "test.pem", "x")

    assert run(layer, config, "--exclude", str(layer / "src")) == 0


def test_a_configuration_that_extends_the_defaults_needs_the_gitleaks_binary(
    layer: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / ".gitleaks.toml"
    repository.write_text("[extend]\nuseDefault = true\n")

    assert run(layer, repository) == 2
    assert "pass --gitleaks" in capsys.readouterr().err


def test_the_gitleaks_binary_scans_each_listed_file(layer: Path, tmp_path: Path) -> None:
    repository = tmp_path / ".gitleaks.toml"
    repository.write_text("[extend]\nuseDefault = true\n")
    binary = tmp_path / "gitleaks"
    binary.write_text(
        '#!/bin/sh\nfor last; do :; done\ncase "$last" in */passwd) exit 2;; esac\nexit 0\n'
    )
    binary.chmod(stat.S_IRWXU)

    assert run(layer, repository, "--gitleaks", str(binary)) == 1
    binary.write_text("#!/bin/sh\nexit 1\n")
    assert run(layer, repository, "--gitleaks", str(binary)) == 2


def test_an_unreadable_configuration_or_rule_is_an_error(layer: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("[[rules]\n")
    bad_rule = tmp_path / "bad.toml"
    bad_rule.write_text("[[rules]]\nid = 'x'\nregex = '''(unclosed'''\n")

    assert run(layer, broken) == 2
    assert run(layer, bad_rule) == 2
    assert run(layer, tmp_path / "absent.toml") == 2


def test_the_repository_configuration_loads() -> None:
    repository = SCRIPT.parents[3] / ".gitleaks.toml"

    assert check.load_rules(repository) == []
    assert check.extends_defaults(repository)


def test_rules_without_keywords_and_other_devices() -> None:
    rule = check.Rule("r", __import__("re").compile(rb"secret-\d+"), ())

    assert rule.matches(b"a secret-123")
    assert not rule.matches(b"nothing")
    assert check._allowed(check.PurePosixPath("/usr"))
    assert not check._allowed(check.PurePosixPath("/usrx/file"))
    assert SCRIPT.is_file()
