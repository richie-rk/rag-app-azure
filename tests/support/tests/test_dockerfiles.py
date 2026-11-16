"""T-524, the Dockerfile unit test (`01-repository.md` sections 1.12 and 4; `14-ingestion.md`
section 15.2).

Every Dockerfile's `FROM` lines are digest-pinned; the API and model-stub final stages run as UID
10001; the ingestion image defines the 16 parse users with their UIDs and GIDs, one root-owned
lock file each with mode 0600, and the scratch parent with owner root and mode 0711, removes write
access for others from every path but `/tmp` and `/var/tmp`, and installs `libseccomp2` and
`procps`; every final stage ends with its smoke `RUN`; and `.dockerignore` excludes the paths of
section 1.12. The parse-user step is run by `sh` with `groupadd`, `useradd` and `install`
replaced by recorders, so the test checks what the loop expands to, not how it is spelled.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
API: Final = REPOSITORY_ROOT / "apps" / "api" / "Dockerfile"
INGESTION: Final = REPOSITORY_ROOT / "apps" / "ingestion" / "Dockerfile"
MODEL_STUB: Final = REPOSITORY_ROOT / "tests" / "model-stub" / "Dockerfile"
DOCKERFILES: Final = (API, INGESTION, MODEL_STUB)
REPOSITORY_SPEC: Final = REPOSITORY_ROOT / "docs" / "spec" / "01-repository.md"

PARSE_USERS: Final = 16
FIRST_PARSE_UID: Final = 10002
WORLD_WRITABLE_STEP: Final = (
    r"find / -xdev \( -type d -o -type f \) -perm -0002 -not -path /tmp -not -path /var/tmp "
    "-exec chmod o-w {} +"
)

_FROM_RE: Final = re.compile(
    r"\A(?:--platform=\S+\s+)?(?P<image>[^\s@]+:[^\s@]+)@sha256:[0-9a-f]{64}(?:\s+AS\s+\S+)?\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Instruction:
    keyword: str
    arguments: str


@dataclass(slots=True)
class Stage:
    base: str
    instructions: list[Instruction] = field(default_factory=list)

    def all(self, keyword: str) -> list[str]:
        return [item.arguments for item in self.instructions if item.keyword == keyword]


def instructions(path: Path) -> list[Instruction]:
    """The Dockerfile's instructions, continuation lines joined and comments left out."""
    found: list[Instruction] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if pending and line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending += (line[:-1] if continued else line) + (" " if continued else "")
        if not continued:
            keyword, _, arguments = pending.strip().partition(" ")
            found.append(Instruction(keyword.upper(), " ".join(arguments.split())))
            pending = ""
    return found


def stages(path: Path) -> list[Stage]:
    result: list[Stage] = []
    for item in instructions(path):
        if item.keyword == "FROM":
            result.append(Stage(item.arguments))
        elif result:
            result[-1].instructions.append(item)
    return result


def run_with_recorders(script: str) -> list[list[str]]:
    """Run a `RUN` step's shell text with the user and file commands replaced by recorders."""
    prelude = (
        'groupadd() { echo groupadd "$@"; }\n'
        'useradd() { echo useradd "$@"; }\n'
        'install() { echo install "$@"; }\n'
        "set -e\n"
    )
    shell = shutil.which("sh")
    assert shell is not None
    completed = subprocess.run(  # noqa: S603 - the repository's own Dockerfile text, no input
        [shell, "-c", prelude + script], capture_output=True, text=True, check=True, timeout=30
    )
    return [line.split() for line in completed.stdout.splitlines()]


def dockerignore_lines() -> list[str]:
    """The `.dockerignore` block of `01-repository.md` section 1.12."""
    text = REPOSITORY_SPEC.read_text(encoding="utf-8")
    after = text[text.index("`.dockerignore`, exactly") :]
    block = after[after.index("```text\n") + len("```text\n") :]
    return block[: block.index("```")].splitlines()


# T-524


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda path: path.parent.name)
def test_t524_every_from_line_is_pinned_by_digest(path: Path) -> None:
    bases = [stage.base for stage in stages(path)]

    assert len(bases) == 2
    assert [base for base in bases if _FROM_RE.match(base) is None] == []


@pytest.mark.parametrize("path", [API, MODEL_STUB], ids=lambda path: path.parent.name)
def test_t524_the_api_and_stub_final_stages_run_as_uid_10001(path: Path) -> None:
    final = stages(path)[-1]
    runs = " ".join(final.all("RUN"))

    assert final.all("USER")[-1] == "10001:10001"
    assert "groupadd --gid 10001 docchat" in runs
    assert (
        "useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent "
        "--shell /usr/sbin/nologin docchat"
    ) in runs


def test_t524_the_ingestion_image_defines_the_parse_users_their_locks_and_the_scratch_parent() -> (
    None
):
    (step,) = [run for run in stages(INGESTION)[-1].all("RUN") if "docchat-parse" in run]

    calls = run_with_recorders(step)

    groups = [call[1:] for call in calls if call[0] == "groupadd"]
    users = [call[1:] for call in calls if call[0] == "useradd"]
    installs = [call[1:] for call in calls if call[0] == "install"]
    assert groups == [
        ["--gid", str(FIRST_PARSE_UID + n), f"docchat-parse-{n}"] for n in range(PARSE_USERS)
    ]
    assert users == [
        [
            "--uid",
            str(FIRST_PARSE_UID + n),
            "--gid",
            str(FIRST_PARSE_UID + n),
            "--no-create-home",
            "--home-dir",
            "/nonexistent",
            "--shell",
            "/usr/sbin/nologin",
            f"docchat-parse-{n}",
        ]
        for n in range(PARSE_USERS)
    ]
    assert installs == [
        ["-d", "-o", "root", "-g", "root", "-m", "0700", "/run/docchat-parse"],
        *(
            [
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0600",
                "/dev/null",
                f"/run/docchat-parse/{FIRST_PARSE_UID + n}.lock",
            ]
            for n in range(PARSE_USERS)
        ),
        ["-d", "-o", "root", "-g", "root", "-m", "0711", "/run/docchat-scratch"],
    ]


def test_t524_the_ingestion_image_leaves_only_tmp_and_var_tmp_world_writable() -> None:
    final = stages(INGESTION)[-1]

    assert WORLD_WRITABLE_STEP in final.all("RUN")
    # After the step nothing is copied in or created, so no later layer adds a writable path.
    after = final.instructions[
        [item.arguments for item in final.instructions].index(WORLD_WRITABLE_STEP) + 1 :
    ]
    assert [item.keyword for item in after] == ["RUN"]


def test_t524_the_ingestion_image_installs_libseccomp2_and_procps() -> None:
    installs = [run for run in stages(INGESTION)[-1].all("RUN") if "apt-get install" in run]

    (packages,) = installs
    words = packages.split()
    assert "libseccomp2=2.5.4-1+deb12u1" in words
    assert "procps" in words


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda path: path.parent.name)
def test_t524_every_final_stage_ends_with_its_offline_smoke_run(path: Path) -> None:
    final = stages(path)[-1]
    last_run = final.all("RUN")[-1]

    assert last_run.startswith("--network=none ")
    after = final.instructions[final.instructions.index(Instruction("RUN", last_run)) + 1 :]
    assert {item.keyword for item in after} <= {"USER", "EXPOSE", "CMD"}


def test_t524_dockerignore_excludes_the_listed_paths() -> None:
    listed = dockerignore_lines()

    assert ".git" in listed
    assert "**/tests" in listed
    assert (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() == listed


def test_t524_the_reader_joins_continuations_and_skips_comments(tmp_path: Path) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text(
        "# a comment\n"
        "FROM python:3@sha256:" + "a" * 64 + " AS build\n"
        "RUN one \\\n"
        "    # inside\n"
        "    && two\n"
        "USER 1:1\n",
        encoding="utf-8",
    )

    assert instructions(path) == [
        Instruction("FROM", "python:3@sha256:" + "a" * 64 + " AS build"),
        Instruction("RUN", "one && two"),
        Instruction("USER", "1:1"),
    ]
