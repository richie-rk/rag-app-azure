"""The protocol between the parent and a parsing child (`14-ingestion.md` section 15.6 item 5;
ADR 0049 decision 6): the request the parent writes, the result models of every command, the
partition file's model `ExtractPartV1`, and the parent's checks of every file a child wrote.

The parent treats every file a child wrote as hostile. It opens only the names this module lists,
without following a link, accepts only a regular file of the child's UID with one link and within
the file's limit, and validates `result.json` with its command's model. No text a child wrote
reaches SQL, a log record or telemetry except a validated code and validated numbers: a refusal
raises an exception whose message is a constant, chained to nothing.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from enum import StrEnum
from typing import Annotated, Final, Literal, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, ValidationError
from pydantic.alias_generators import to_camel

from docchat_ingestion.isolation import syscall_filter

PDF: Final = "application/pdf"
DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX: Final = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
OFFICE_TYPES: Final = frozenset({DOCX, PPTX})

# The limits on files the parent reads from a child (section 15.6 item 5.3; section 18.3).
RESULT_MAX_BYTES: Final = 1_048_576
PARTITION_MAX_BYTES: Final = 134_217_728
CONVERT_MAX_BYTES: Final = 209_715_200
RENDER_MAX_PAGE_BYTES: Final = 20_971_520
RENDER_MAX_DOCUMENT_BYTES: Final = 104_857_600
RENDER_MAX_PAGES_ON_DISK: Final = 4
RENDER_MIN_TOTAL_BYTES: Final = 524_288_000
ZIP_MAX_ENTRIES: Final = 10_000
ZIP_MAX_BYTES: Final = 536_870_912
PAGE_COUNT_MAX: Final = 1_000_000
CODE_MAX_LENGTH: Final = 64
TEXT_MAX_CHARACTERS: Final = 8_388_608
ELEMENTS_PER_PAGE_MAX: Final = 100_000

# The names the parent ever opens in a child's directory (section 15.8).
SOURCE: Final = "source.bin"
REQUEST: Final = "request.json"
RESULT: Final = "result.json"
PARTITION: Final = "partition.json"
CONVERTED: Final = "converted.pdf"
DOCUMENT: Final = "document.pdf"
DOCUMENT_VERIFIED: Final = "document.verified.pdf"

_PAGE_NAME: Final = re.compile(r"[0-9]{1,4}\.pdf")


class Command(StrEnum):
    INSPECT = "inspect"
    CONVERT = "convert"
    PARTITION = "partition"
    RENDER = "render"
    ASSEMBLE = "assemble"
    VERIFY_RENDER = "verify-render"
    PROBE = "probe"


def render_max_total_bytes(pdf_size: int) -> int:
    """`renderMaxTotalBytes`: the larger of 10 times the size of the version's PDF and 500 MiB."""
    return max(10 * pdf_size, RENDER_MIN_TOTAL_BYTES)


def page_name(number: int) -> str:
    return f"{number}.pdf"


def verified_name(number: int) -> str:
    return f"{number}.verified.pdf"


def page_number(name: str, page_count: int) -> int | None:
    """The page `n` a `render` child's entry names, or `None` for every other name: the name must
    match `^[0-9]{1,4}\\.pdf$` and equal `f"{n}.pdf"` with `n` from 1 to `page_count`, so
    `0.pdf`, `05.pdf` and `{n}.pdf.part` are never opened."""
    if _PAGE_NAME.fullmatch(name) is None:
        return None
    number = int(name.removesuffix(".pdf"))
    if page_name(number) != name or not 1 <= number <= page_count:
        return None
    return number


def _true(value: bool) -> bool:
    if value is not True:
        raise ValueError("must be true")
    return value


def _false(value: bool) -> bool:
    if value is not False:
        raise ValueError("must be false")
    return value


# `Literal[True]` would also accept 1 and `Literal[1]` would accept true, because they compare
# equal; a strict boolean or integer with its own check accepts the one JSON value only.
type TrueValue = Annotated[bool, AfterValidator(_true)]
type FalseValue = Annotated[bool, AfterValidator(_false)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Limits(_Strict):
    """The caps every request carries; `renderMaxTotalBytes` depends on the PDF's size."""

    max_pages: int = Field(alias="maxPages", ge=1)
    max_zip_entries: int = Field(
        alias="maxZipEntries", ge=ZIP_MAX_ENTRIES, le=ZIP_MAX_ENTRIES, default=ZIP_MAX_ENTRIES
    )
    max_zip_bytes: int = Field(
        alias="maxZipBytes", ge=ZIP_MAX_BYTES, le=ZIP_MAX_BYTES, default=ZIP_MAX_BYTES
    )
    convert_max_bytes: int = Field(
        alias="convertMaxBytes",
        ge=CONVERT_MAX_BYTES,
        le=CONVERT_MAX_BYTES,
        default=CONVERT_MAX_BYTES,
    )
    render_max_page_bytes: int = Field(
        alias="renderMaxPageBytes",
        ge=RENDER_MAX_PAGE_BYTES,
        le=RENDER_MAX_PAGE_BYTES,
        default=RENDER_MAX_PAGE_BYTES,
    )
    render_max_total_bytes: int = Field(alias="renderMaxTotalBytes", ge=RENDER_MIN_TOTAL_BYTES)
    render_max_pages_on_disk: int = Field(
        alias="renderMaxPagesOnDisk",
        ge=RENDER_MAX_PAGES_ON_DISK,
        le=RENDER_MAX_PAGES_ON_DISK,
        default=RENDER_MAX_PAGES_ON_DISK,
    )
    render_max_document_bytes: int = Field(
        alias="renderMaxDocumentBytes",
        ge=RENDER_MAX_DOCUMENT_BYTES,
        le=RENDER_MAX_DOCUMENT_BYTES,
        default=RENDER_MAX_DOCUMENT_BYTES,
    )

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_by_name=True)


class PageRange(_Strict):
    first: int = Field(ge=1, le=PAGE_COUNT_MAX)
    last: int = Field(ge=1, le=PAGE_COUNT_MAX)


class ChildRequest(_Strict):
    """`request.json`. `pages` is a part (`partition`) or a batch (`verify-render` in page mode);
    `mode` is set for `verify-render` only; `parameters` carries a command's own inputs (the
    page-wise parameters for `partition`, and the version's `pageCount` for `assemble` and the
    document-mode `verify-render`)."""

    command: Command
    content_type: str
    source_path: str | None
    pages: PageRange | None
    mode: Literal["pages", "document"] | None
    parameters: dict[str, JsonValue]
    limits: Limits
    fail_on_network: TrueValue = True

    # The camelCase keys come from the generator: two of them share their spelling with chunk
    # index fields, whose names appear only in docchat_core.search.schema (T-107).
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=False,
        alias_generator=to_camel,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    def encode(self) -> bytes:
        return self.model_dump_json(by_alias=True).encode("utf-8")


# Each command's failure codes (section 15.6 item 5.5): item codes of section 17.2, and for the
# probe the internal code of section 17.3.
INSPECT_CODES: Final = (
    "file_signature_mismatch",
    "zip_entry_limit_exceeded",
    "zip_size_limit_exceeded",
    "encrypted_document",
    "corrupt_document",
    "macro_content_declared",
)
type InspectCode = Literal[
    "file_signature_mismatch",
    "zip_entry_limit_exceeded",
    "zip_size_limit_exceeded",
    "encrypted_document",
    "corrupt_document",
    "macro_content_declared",
]

type PageCount = Annotated[int, Field(ge=0, le=PAGE_COUNT_MAX)]
type PositivePageCount = Annotated[int, Field(ge=1, le=PAGE_COUNT_MAX)]


class InspectOk(_Strict):
    ok: TrueValue
    page_count: PageCount | None = Field(alias="pageCount")


class InspectFailed(_Strict):
    ok: FalseValue
    code: InspectCode


class Ok(_Strict):
    """Success of `convert`, `partition` and `verify-render`."""

    ok: TrueValue


class ConvertFailed(_Strict):
    ok: FalseValue
    code: Literal["convert_failed"]


class PartitionFailed(_Strict):
    ok: FalseValue
    code: Literal["corrupt_document"]


class RenderOk(_Strict):
    ok: TrueValue
    page_count: PositivePageCount = Field(alias="pageCount")


class RenderFailed(_Strict):
    ok: FalseValue
    code: Literal["render_too_large", "render_failed"]


class AssembleOk(_Strict):
    ok: TrueValue
    page_count: PositivePageCount = Field(alias="pageCount")
    document_pdf: Literal["built", "too_large"] = Field(alias="documentPdf")


class RenditionFailed(_Strict):
    """Failure of `assemble` and `verify-render`."""

    ok: FalseValue
    code: Literal["render_failed"]


class ProbeOk(_Strict):
    ok: TrueValue
    filter_installed: TrueValue = Field(alias="filterInstalled")
    inet_socket_refused: TrueValue = Field(alias="inetSocketRefused")
    unix_socket_refused: TrueValue = Field(alias="unixSocketRefused")
    socketpair_refused: TrueValue = Field(alias="socketpairRefused")
    io_uring_refused: TrueValue = Field(alias="ioUringRefused")
    traceme_refused: TrueValue = Field(alias="tracemeRefused")
    vm_read_refused: TrueValue = Field(alias="vmReadRefused")
    clone3_refused: TrueValue = Field(alias="clone3Refused")
    thread_filtered: TrueValue = Field(alias="threadFiltered")


class ConvertProbeOk(_Strict):
    """The `convert`-mode probe's success (section 15.11 item 7.3): under the `convert` filter,
    which allows `AF_UNIX` sockets, `socket()` for `AF_INET`, `AF_INET6` and `AF_NETLINK` is still
    refused, and every check of the other rules holds."""

    ok: TrueValue
    filter_installed: TrueValue = Field(alias="filterInstalled")
    inet_socket_refused: TrueValue = Field(alias="inetSocketRefused")
    inet6_socket_refused: TrueValue = Field(alias="inet6SocketRefused")
    netlink_socket_refused: TrueValue = Field(alias="netlinkSocketRefused")
    io_uring_refused: TrueValue = Field(alias="ioUringRefused")
    traceme_refused: TrueValue = Field(alias="tracemeRefused")
    vm_read_refused: TrueValue = Field(alias="vmReadRefused")
    clone3_refused: TrueValue = Field(alias="clone3Refused")
    thread_filtered: TrueValue = Field(alias="threadFiltered")


class ProbeFailed(_Strict):
    ok: FalseValue
    code: Literal["parse_isolation_failed"]


type ChildResult = (
    InspectOk
    | InspectFailed
    | Ok
    | ConvertFailed
    | PartitionFailed
    | RenderOk
    | RenderFailed
    | AssembleOk
    | RenditionFailed
    | ProbeOk
    | ConvertProbeOk
    | ProbeFailed
)

_RESULT_MODELS: Final[dict[Command, tuple[type[_Strict], type[_Strict]]]] = {
    Command.INSPECT: (InspectOk, InspectFailed),
    Command.CONVERT: (Ok, ConvertFailed),
    Command.PARTITION: (Ok, PartitionFailed),
    Command.RENDER: (RenderOk, RenderFailed),
    Command.ASSEMBLE: (AssembleOk, RenditionFailed),
    Command.VERIFY_RENDER: (Ok, RenditionFailed),
    Command.PROBE: (ProbeOk, ProbeFailed),
}

# The item code of a child that crashed, ran out of time or memory, or left no valid result
# (section 15.6 items 5 and 6). The probe's only failure is `parse_isolation_failed`.
_PROCESS_FAILURE: Final = {
    Command.INSPECT: "parse_failed",
    Command.PARTITION: "parse_failed",
    Command.CONVERT: "convert_failed",
    Command.RENDER: "render_failed",
    Command.ASSEMBLE: "render_failed",
    Command.VERIFY_RENDER: "render_failed",
    Command.PROBE: "parse_isolation_failed",
}
_TIMEOUT: Final = {
    Command.INSPECT: "parse_timeout",
    Command.PARTITION: "parse_timeout",
    Command.RENDER: "parse_timeout",
    Command.CONVERT: "convert_timeout",
    Command.ASSEMBLE: "render_failed",
    Command.VERIFY_RENDER: "render_failed",
    Command.PROBE: "parse_isolation_failed",
}
_MEMORY: Final = {
    Command.INSPECT: "parse_memory_exceeded",
    Command.PARTITION: "parse_memory_exceeded",
    Command.RENDER: "parse_memory_exceeded",
    Command.CONVERT: "convert_memory_exceeded",
    Command.ASSEMBLE: "render_failed",
    Command.VERIFY_RENDER: "render_failed",
    Command.PROBE: "parse_isolation_failed",
}


def process_failure_code(command: Command) -> str:
    """The code of a crash, a non-zero exit, a missing or invalid result, or a refused file."""
    return _PROCESS_FAILURE[command]


def timeout_code(command: Command) -> str:
    return _TIMEOUT[command]


def memory_code(command: Command) -> str:
    return _MEMORY[command]


class ChildFileError(Exception):
    """A child's file failed a check: absent, a link, not a regular file, another owner, a second
    link, or invalid content. The message is constant; nothing the child wrote is in it."""

    def __init__(self, reason: str = "child file refused") -> None:
        super().__init__(reason)


class ChildFileMissingError(ChildFileError):
    def __init__(self) -> None:
        super().__init__("child file missing")


class ChildFileTooLargeError(ChildFileError):
    def __init__(self) -> None:
        super().__init__("child file above its limit")


def read_child_file(dir_fd: int, name: str, *, uid: int, limit: int) -> bytes:
    """Open `name` in the directory `dir_fd` without following a link or blocking on a FIFO,
    check it with `fstat` on the open descriptor and read at most `limit` + 1 bytes."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        raise ChildFileMissingError from None
    except OSError as failure:
        # ELOOP is a symbolic link; anything else (a device, a permission) is refused alike.
        reason = "child file is a link" if failure.errno == errno.ELOOP else "child file refused"
        raise ChildFileError(reason) from None
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != uid or status.st_nlink != 1:
            raise ChildFileError
        if status.st_size > limit:
            raise ChildFileTooLargeError
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ChildFileTooLargeError
    return data


def parse_result(
    command: Command,
    raw: bytes,
    *,
    page_count: int | None = None,
    office_source: bool = False,
    filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT,
) -> ChildResult:
    """Validate `result.json` of `command`.

    `page_count` is the version's page count, which a `render` and an `assemble` result must
    repeat; `office_source` says whether an `inspect` ran on a DOCX or PPTX source, whose success
    carries `pageCount` null (and only then); `filter_name` is the filter the child was started
    with, which makes a `probe` the default probe or the `convert`-mode probe.
    """
    if len(raw) > RESULT_MAX_BYTES:
        raise ChildFileTooLargeError
    success: type[_Strict]
    failure: type[_Strict]
    if command is Command.PROBE and filter_name == syscall_filter.CONVERT:
        success, failure = ConvertProbeOk, ProbeFailed
    else:
        success, failure = _RESULT_MODELS[command]
    try:
        text = raw.decode("utf-8")
        model: _Strict
        try:
            model = success.model_validate_json(text)
        except ValidationError:
            model = failure.model_validate_json(text)
    except (UnicodeDecodeError, ValidationError):
        raise ChildFileError("child result invalid") from None
    if isinstance(model, InspectOk) and (model.page_count is None) != office_source:
        raise ChildFileError("child result invalid")
    if (
        isinstance(model, RenderOk | AssembleOk)
        and page_count is not None
        and model.page_count != page_count
    ):
        raise ChildFileError("child result invalid")
    return cast(ChildResult, model)


class ExtractElement(_Strict):
    kind: Literal["text", "title", "list_item", "table", "header", "footer", "other"]
    text: str = Field(max_length=TEXT_MAX_CHARACTERS)
    table_html: str | None = Field(alias="tableHtml", max_length=TEXT_MAX_CHARACTERS)
    heading_level: int | None = Field(alias="headingLevel", ge=1, le=100)

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_by_name=True, serialize_by_alias=True
    )


class ExtractPage(_Strict):
    page_number: int = Field(ge=1, le=PAGE_COUNT_MAX)
    elements: list[ExtractElement] = Field(max_length=ELEMENTS_PER_PAGE_MAX)

    # Generated camelCase keys, as in ChildRequest (T-107).
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        alias_generator=to_camel,
        validate_by_name=True,
        serialize_by_alias=True,
    )


class ExtractPartV1(_Strict):
    """An extract part of the page-wise method (section 6.14 item 1), the shape of
    `partition.json` and of `extract/part-{first:05d}-{last:05d}.json`."""

    format_version: int = Field(alias="formatVersion", ge=1, le=1)
    method: Literal["pagewise"]
    page_count: int = Field(alias="pageCount", ge=1, le=PAGE_COUNT_MAX)
    first_page: int = Field(alias="firstPage", ge=1, le=PAGE_COUNT_MAX)
    last_page: int = Field(alias="lastPage", ge=1, le=PAGE_COUNT_MAX)
    pages: list[ExtractPage]

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_by_name=True, serialize_by_alias=True
    )


def parse_partition(raw: bytes, *, page_count: int, first: int, last: int) -> ExtractPartV1:
    """Validate `partition.json` against the version and the request's part: the page count,
    the part's bounds, at most one entry per page and every page inside the part."""
    if len(raw) > PARTITION_MAX_BYTES:
        raise ChildFileTooLargeError
    try:
        part = ExtractPartV1.model_validate_json(raw)
    except ValidationError:
        raise ChildFileError("child partition invalid") from None
    numbers = [page.page_number for page in part.pages]
    if (
        part.page_count != page_count
        or (part.first_page, part.last_page) != (first, last)
        or len(set(numbers)) != len(numbers)
        or any(not first <= number <= last for number in numbers)
    ):
        raise ChildFileError("child partition invalid")
    return part
