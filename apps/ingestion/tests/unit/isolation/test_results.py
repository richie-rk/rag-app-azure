"""`docchat_ingestion.isolation.results`: the child protocol's models and the parent's checks of
every file a child wrote (`14-ingestion.md` section 15.6 item 5; T-598, and the file-form half of
T-210)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from docchat_ingestion.isolation import results
from docchat_ingestion.isolation.results import (
    ChildFileError,
    ChildFileMissingError,
    ChildFileTooLargeError,
    ChildRequest,
    Command,
    ConvertProbeOk,
    InspectOk,
    Limits,
    RenderOk,
    parse_partition,
    parse_result,
    read_child_file,
)

CANARY = "cnry-result-0a1b2c3d"


def encoded(value: object) -> bytes:
    return json.dumps(value).encode()


def refused(command: Command, value: object, **context: object) -> str:
    with pytest.raises(ChildFileError) as caught:
        parse_result(command, encoded(value), **context)  # type: ignore[arg-type]
    return str(caught.value)


def test_t598_valid_results_of_every_command() -> None:
    probe = {
        "ok": True,
        "filterInstalled": True,
        "inetSocketRefused": True,
        "unixSocketRefused": True,
        "socketpairRefused": True,
        "ioUringRefused": True,
        "tracemeRefused": True,
        "vmReadRefused": True,
        "clone3Refused": True,
        "threadFiltered": True,
    }

    assert parse_result(Command.INSPECT, encoded({"ok": True, "pageCount": 3})) == InspectOk(
        ok=True, pageCount=3
    )
    assert parse_result(
        Command.INSPECT, encoded({"ok": True, "pageCount": None}), office_source=True
    ).ok
    assert parse_result(Command.CONVERT, encoded({"ok": True})).ok
    assert not parse_result(
        Command.PARTITION, encoded({"ok": False, "code": "corrupt_document"})
    ).ok
    assert parse_result(
        Command.RENDER, encoded({"ok": True, "pageCount": 4}), page_count=4
    ) == RenderOk(ok=True, pageCount=4)
    assert parse_result(
        Command.ASSEMBLE,
        encoded({"ok": True, "pageCount": 4, "documentPdf": "too_large"}),
        page_count=4,
    ).ok
    assert not parse_result(
        Command.VERIFY_RENDER, encoded({"ok": False, "code": "render_failed"})
    ).ok
    assert parse_result(Command.PROBE, encoded(probe)).ok


CONVERT_PROBE = {
    "ok": True,
    "filterInstalled": True,
    "inetSocketRefused": True,
    "inet6SocketRefused": True,
    "netlinkSocketRefused": True,
    "ioUringRefused": True,
    "tracemeRefused": True,
    "vmReadRefused": True,
    "clone3Refused": True,
    "threadFiltered": True,
}


def test_a_convert_mode_probe_result_is_validated_with_its_own_model() -> None:
    """A probe started with the `convert` filter reports the other families refused
    (`14-ingestion.md` section 15.11 item 7.3); neither probe's result passes for the other."""
    assert parse_result(Command.PROBE, encoded(CONVERT_PROBE), filter_name="convert") == (
        ConvertProbeOk.model_validate(CONVERT_PROBE)
    )
    assert not parse_result(
        Command.PROBE,
        encoded({"ok": False, "code": "parse_isolation_failed"}),
        filter_name="convert",
    ).ok
    assert refused(Command.PROBE, CONVERT_PROBE) == "child result invalid"
    default_probe = {
        "ok": True,
        "filterInstalled": True,
        "inetSocketRefused": True,
        "unixSocketRefused": True,
        "socketpairRefused": True,
        "ioUringRefused": True,
        "tracemeRefused": True,
        "vmReadRefused": True,
        "clone3Refused": True,
        "threadFiltered": True,
    }
    assert parse_result(Command.PROBE, encoded(default_probe)).ok
    assert refused(Command.PROBE, default_probe, filter_name="convert") == "child result invalid"
    assert (
        refused(
            Command.PROBE, {**CONVERT_PROBE, "netlinkSocketRefused": False}, filter_name="convert"
        )
        == "child result invalid"
    )
    # The filter changes only the probe's model: a convert child's result is still `{"ok": true}`.
    assert parse_result(Command.CONVERT, encoded({"ok": True}), filter_name="convert").ok


@pytest.mark.parametrize(
    ("command", "value", "context"),
    [
        (Command.INSPECT, {"ok": True, "pageCount": 1, "extra": CANARY}, {}),
        (Command.INSPECT, {"ok": False, "code": "render_failed"}, {}),
        (Command.CONVERT, {"ok": False, "code": "parse_timeout"}, {}),
        (Command.INSPECT, {"ok": True, "pageCount": -1}, {}),
        (Command.INSPECT, {"ok": True, "pageCount": 1_000_001}, {}),
        (Command.INSPECT, {"ok": True, "pageCount": 1.0}, {}),
        (Command.INSPECT, {"ok": 1, "pageCount": 1}, {}),
        (Command.INSPECT, {"ok": True, "pageCount": None}, {}),
        (Command.INSPECT, {"ok": True, "pageCount": 2}, {"office_source": True}),
        (Command.RENDER, {"ok": True, "pageCount": 5}, {"page_count": 4}),
        (Command.ASSEMBLE, {"ok": True, "pageCount": 5, "documentPdf": "built"}, {"page_count": 4}),
        (Command.ASSEMBLE, {"ok": True, "pageCount": 4, "documentPdf": CANARY}, {}),
        (Command.PROBE, {"ok": True, "filterInstalled": True}, {}),
        (Command.VERIFY_RENDER, {"ok": False, "code": CANARY}, {}),
        (Command.PARTITION, [CANARY], {}),
    ],
)
def test_t598_invalid_results_are_refused_without_their_text(
    command: Command, value: object, context: dict[str, object]
) -> None:
    message = refused(command, value, **context)

    assert CANARY not in message
    assert message == "child result invalid"


def test_t598_bytes_that_arent_utf8_json_and_an_oversized_result_are_refused() -> None:
    with pytest.raises(ChildFileError):
        parse_result(Command.INSPECT, b"\xff\xfe not json")
    with pytest.raises(ChildFileError):
        parse_result(Command.INSPECT, b"{not json")
    with pytest.raises(ChildFileTooLargeError):
        parse_result(Command.INSPECT, b" " * (results.RESULT_MAX_BYTES + 1))


@pytest.mark.parametrize(
    ("command", "process", "timeout", "memory"),
    [
        (Command.INSPECT, "parse_failed", "parse_timeout", "parse_memory_exceeded"),
        (Command.PARTITION, "parse_failed", "parse_timeout", "parse_memory_exceeded"),
        (Command.CONVERT, "convert_failed", "convert_timeout", "convert_memory_exceeded"),
        (Command.RENDER, "render_failed", "parse_timeout", "parse_memory_exceeded"),
        (Command.ASSEMBLE, "render_failed", "render_failed", "render_failed"),
        (Command.VERIFY_RENDER, "render_failed", "render_failed", "render_failed"),
        (
            Command.PROBE,
            "parse_isolation_failed",
            "parse_isolation_failed",
            "parse_isolation_failed",
        ),
    ],
)
def test_failure_codes_of_each_command(
    command: Command, process: str, timeout: str, memory: str
) -> None:
    assert results.process_failure_code(command) == process
    assert results.timeout_code(command) == timeout
    assert results.memory_code(command) == memory


def part(**changes: object) -> bytes:
    value: dict[str, object] = {
        "formatVersion": 1,
        "method": "pagewise",
        "pageCount": 10,
        "firstPage": 1,
        "lastPage": 2,
        "pages": [
            {
                "pageNumber": 1,
                "elements": [
                    {"kind": "title", "text": "T", "tableHtml": None, "headingLevel": 1},
                    {
                        "kind": "table",
                        "text": "",
                        "tableHtml": "<table></table>",
                        "headingLevel": None,
                    },
                ],
            }
        ],
    }
    value.update(changes)
    return encoded(value)


def test_t598_a_valid_partition_file() -> None:
    parsed = parse_partition(part(), page_count=10, first=1, last=2)

    assert parsed.pages[0].elements[1].table_html == "<table></table>"


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": CANARY},
        {"pageCount": "10"},
        {"pageCount": 11},
        {"firstPage": 2},
        {"formatVersion": True},
        {"method": "recursive"},
        {"pages": [{"pageNumber": 3, "elements": []}]},
        {"pages": [{"pageNumber": 1, "elements": []}, {"pageNumber": 1, "elements": []}]},
        {
            "pages": [
                {
                    "pageNumber": 1,
                    "elements": [
                        {"kind": "image", "text": "", "tableHtml": None, "headingLevel": None}
                    ],
                }
            ]
        },
        {
            "pages": [
                {
                    "pageNumber": 1,
                    "elements": [
                        {"kind": "title", "text": "", "tableHtml": None, "headingLevel": 101}
                    ],
                }
            ]
        },
    ],
)
def test_t598_an_invalid_partition_file_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ChildFileError) as caught:
        parse_partition(part(**changes), page_count=10, first=1, last=2)

    assert CANARY not in str(caught.value)


def test_t598_a_partition_file_of_128_mib_and_one_byte_is_refused() -> None:
    with pytest.raises(ChildFileTooLargeError):
        parse_partition(b" " * (results.PARTITION_MAX_BYTES + 1), page_count=1, first=1, last=1)


@pytest.fixture
def directory(tmp_path: Path) -> Iterator[int]:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    yield fd
    os.close(fd)


def test_t210_a_regular_file_of_the_childs_uid_is_read(tmp_path: Path, directory: int) -> None:
    (tmp_path / "result.json").write_bytes(b"{}")

    assert read_child_file(directory, "result.json", uid=os.getuid(), limit=10) == b"{}"


def test_t210_a_missing_file_a_link_a_hard_link_a_fifo_and_another_owner_are_refused(
    tmp_path: Path, directory: int
) -> None:
    (tmp_path / "target").write_bytes(b"secret")
    (tmp_path / "5.pdf").symlink_to(tmp_path / "target")
    os.link(tmp_path / "target", tmp_path / "hard.pdf")
    os.mkfifo(tmp_path / "fifo.pdf")
    (tmp_path / "mine.pdf").write_bytes(b"x")
    uid = os.getuid()

    with pytest.raises(ChildFileMissingError):
        read_child_file(directory, "absent.json", uid=uid, limit=10)
    with pytest.raises(ChildFileError, match="link"):
        read_child_file(directory, "5.pdf", uid=uid, limit=10)
    with pytest.raises(ChildFileError):
        read_child_file(directory, "hard.pdf", uid=uid, limit=10)
    with pytest.raises(ChildFileError):
        read_child_file(directory, "fifo.pdf", uid=uid, limit=10)
    with pytest.raises(ChildFileError):
        read_child_file(directory, "mine.pdf", uid=uid + 1, limit=10)


def test_t210_a_file_above_its_limit_is_too_large(tmp_path: Path, directory: int) -> None:
    (tmp_path / "1.pdf").write_bytes(b"x" * 11)

    with pytest.raises(ChildFileTooLargeError):
        read_child_file(directory, "1.pdf", uid=os.getuid(), limit=10)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("1.pdf", 1),
        ("12.pdf", 12),
        ("0.pdf", None),
        ("05.pdf", None),
        ("13.pdf", None),
        ("5.pdf.part", None),
        ("12345.pdf", None),
        ("a.pdf", None),
        ("1.verified.pdf", None),
    ],
)
def test_t210_only_page_names_of_the_version_are_opened(name: str, expected: int | None) -> None:
    assert results.page_number(name, 12) == expected


def test_the_request_round_trips_and_carries_its_caps() -> None:
    request = ChildRequest.model_validate(
        {
            "command": "render",
            "contentType": results.PDF,
            "sourcePath": results.SOURCE,
            "pages": None,
            "mode": None,
            "parameters": {},
            "limits": Limits(
                max_pages=2000, render_max_total_bytes=results.render_max_total_bytes(1)
            ),
        }
    )
    value = json.loads(request.encode())

    assert value["failOnNetwork"] is True
    assert value["limits"] == {
        "maxPages": 2000,
        "maxZipEntries": 10000,
        "maxZipBytes": 536870912,
        "convertMaxBytes": 209715200,
        "renderMaxPageBytes": 20971520,
        "renderMaxTotalBytes": 524288000,
        "renderMaxPagesOnDisk": 4,
        "renderMaxDocumentBytes": 104857600,
    }
    assert ChildRequest.model_validate_json(request.encode()) == request
    assert results.render_max_total_bytes(100_000_000) == 1_000_000_000
