"""The method catalogue and interface (`14-ingestion.md` section 9; `80-data-model.md` section 2.9;
ADRs 0046, 0047 and 0048).

Descriptors and parameter models live in core, because the API validates parameters and serves
their JSON Schemas; implementations live in the ingestion app, which registers each against its
descriptor at import. Every descriptor equals its `app.ingestion_methods` row (key, version,
display name, description, schema text, defaults and content types; T-088), and every parameter
model agrees with its hand-written JSON Schema on every bound and default (T-180), so the admin
form, the API and the build read one definition.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StrictBool
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError
from sqlalchemy import Connection, select

from docchat_core.errors import ITEM_CODES, ItemCodeKind
from docchat_core.ingestion.models import (
    ContentType,
    IngestionRefusedError,
    ItemSettings,
    ItemWarning,
)
from docchat_core.ingestion.names import BlobContainer, validate_blob_path, work_prefix
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.models import Sha256Hex
from docchat_core.sql.tables.ingestion import ingestion_methods
from docchat_core.sql.tables.schema import BIT_TRUE

METHOD_KEY_PATTERN: Final = r"^[a-z0-9_]{1,64}$"
_METHOD_KEY_RE: Final = re.compile(METHOD_KEY_PATTERN)


def compact_json(value: object) -> str:
    """Compact JSON in the given key order: the text the catalogue rows and an index's
    `method_parameters` store."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# --- Parameter models -----------------------------------------------------------------------------


def _json_integer(value: object) -> object:
    # JSON Schema's `integer` admits a number with a zero fraction and never a boolean or a
    # string, so the model accepts exactly what the schema accepts.
    if isinstance(value, bool):
        raise PydanticCustomError("int_type", "Input should be a valid integer")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


JsonInteger = Annotated[int, BeforeValidator(_json_integer), Field(strict=True)]


def _unique_items[T](values: list[T]) -> list[T]:
    if len(set(values)) != len(values):
        raise PydanticCustomError("duplicate", "Items must be unique")
    return values


class MethodParameters(BaseModel):
    """Base of every parameter model: unknown names refused, typed as the method's JSON Schema
    types them, every field defaulted so stored parameters of an earlier version gain the fields
    a new version adds. Stored and requested parameters are read by their camelCase names only
    (`MethodDescriptor.validate_parameters`); code builds a model by field name."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    def dumped(self) -> dict[str, Any]:
        """`model_dump(mode="json", by_alias=True)`: the parameters as stored and hashed."""
        return self.model_dump(mode="json", by_alias=True)


type OcrLanguage = Literal["eng", "fra", "deu", "spa", "ita", "nld", "por"]


def _default_ocr_languages() -> list[OcrLanguage]:
    return ["eng"]


class PagewiseParametersV1(MethodParameters):
    """`pagewise` version 1 (`14-ingestion.md` section 10.1)."""

    summarize_tables: StrictBool = True
    ocr_languages: Annotated[
        list[OcrLanguage], Field(min_length=1, max_length=3), AfterValidator(_unique_items)
    ] = Field(default_factory=_default_ocr_languages)


class RecursiveParametersV1(MethodParameters):
    """`recursive` version 1 (`14-ingestion.md` section 11.1)."""

    target_tokens: Annotated[JsonInteger, Field(ge=128, le=2048)] = 512
    # An integer percentage, so validation never depends on floating-point remainders.
    overlap_percent: Annotated[JsonInteger, Field(ge=0, le=50, multiple_of=5)] = 25
    table_max_tokens: Annotated[JsonInteger, Field(ge=256, le=6000)] = 1600


# --- Parameter schemas ----------------------------------------------------------------------------

PAGEWISE_V1_SCHEMA: Final = compact_json(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["summarizeTables", "ocrLanguages"],
        "properties": {
            "summarizeTables": {
                "type": "boolean",
                "title": "Summarise tables",
                "description": (
                    "Add a short written summary before each table so it can be found by meaning."
                ),
                "default": True,
            },
            "ocrLanguages": {
                "type": "array",
                "title": "Languages in scanned pages",
                "description": "Used to read text from scanned pages. Choose up to three.",
                "minItems": 1,
                "maxItems": 3,
                "uniqueItems": True,
                "default": ["eng"],
                "items": {
                    "type": "string",
                    "oneOf": [
                        {"const": "eng", "title": "English"},
                        {"const": "fra", "title": "French"},
                        {"const": "deu", "title": "German"},
                        {"const": "spa", "title": "Spanish"},
                        {"const": "ita", "title": "Italian"},
                        {"const": "nld", "title": "Dutch"},
                        {"const": "por", "title": "Portuguese"},
                    ],
                },
            },
        },
    }
)
"""The hand-written JSON Schema (draft 2020-12) of `pagewise` 1, as the catalogue row stores it."""

RECURSIVE_V1_SCHEMA: Final = compact_json(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["targetTokens", "overlapPercent", "tableMaxTokens"],
        "properties": {
            "targetTokens": {
                "type": "integer",
                "title": "Passage size (tokens)",
                "minimum": 128,
                "maximum": 2048,
                "default": 512,
            },
            "overlapPercent": {
                "type": "integer",
                "title": "Overlap between passages (%)",
                "description": (
                    "Share of each passage repeated at the start of the next one in the same "
                    "section."
                ),
                "minimum": 0,
                "maximum": 50,
                "multipleOf": 5,
                "default": 25,
            },
            "tableMaxTokens": {
                "type": "integer",
                "title": "Largest table kept whole (tokens)",
                "description": "Longer tables are split between rows, repeating the header rows.",
                "minimum": 256,
                "maximum": 6000,
                "default": 1600,
            },
        },
    }
)
"""The hand-written JSON Schema (draft 2020-12) of `recursive` 1, as the catalogue row stores it."""


# --- Descriptors ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MethodDescriptor:
    """One shipped (`key`, `version`) of a method (section 9.1 item 1)."""

    key: str
    version: int
    display_name: str
    description: str
    parameters_model: type[MethodParameters]
    parameter_schema: str
    supported_content_types: tuple[ContentType, ...]

    def __post_init__(self) -> None:
        if not _METHOD_KEY_RE.fullmatch(self.key):
            raise ValueError("a method key is 1 to 64 of a to z, 0 to 9 and _")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("a method version is an integer of at least 1")
        if self.parameters_model.model_config.get("extra") != "forbid":
            raise ValueError("a parameter model forbids unknown fields")
        if not isinstance(json.loads(self.parameter_schema), dict):
            raise TypeError("a parameter schema is a JSON object")
        if not self.supported_content_types:
            raise ValueError("a method supports at least one media type")

    def default_parameters(self) -> MethodParameters:
        """The parameters an index gets when it names none."""
        return self.parameters_model()

    def validate_parameters(self, parameters: Mapping[str, Any] | None) -> MethodParameters:
        """The parameters validated by the method's model, defaults filled; `None` gives the
        defaults. Raises `pydantic.ValidationError`, one error per failing field, which the API
        maps to `422 invalid_method_parameters` at `body.methodParameters.<name>`."""
        if parameters is None:
            return self.default_parameters()
        return self.parameters_model.model_validate(dict(parameters), by_name=False)

    def parameter_hash(self, parameters: MethodParameters) -> bytes:
        """`parameter_hash()` of this method's key and version (section 9.4)."""
        return parameter_hash(self.key, self.version, parameters)

    def snapshot(self, parameters: MethodParameters) -> MethodSnapshot:
        """The `method` member of an item's plan for these parameters."""
        if not isinstance(parameters, self.parameters_model):
            raise TypeError("parameters of another method's model")
        return MethodSnapshot(
            key=self.key,
            version=self.version,
            parameters=parameters.dumped(),
            parameter_hash=self.parameter_hash(parameters).hex(),
        )


def parameter_hash(method_key: str, method_version: int, parameters: MethodParameters) -> bytes:
    """The SHA-256 (32 bytes, `BINARY(32)`) of `json.dumps({"key", "version", "parameters"},
    sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8, where
    `parameters` is the validated model dumped with defaults filled (section 9.4). Stored on the
    logical index (`method_parameter_hash`) and on every chunk set (`parameter_hash`)."""
    text = json.dumps(
        {"key": method_key, "version": method_version, "parameters": parameters.dumped()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).digest()


ALL_CONTENT_TYPES: Final = (ContentType.PDF, ContentType.DOCX, ContentType.PPTX)

PAGEWISE_V1: Final = MethodDescriptor(
    key="pagewise",
    version=1,
    display_name="Page by page",
    description=(
        "One passage per page, with tables summarised and kept as structured tables. "
        "Uses the built-in parser."
    ),
    parameters_model=PagewiseParametersV1,
    parameter_schema=PAGEWISE_V1_SCHEMA,
    supported_content_types=ALL_CONTENT_TYPES,
)

RECURSIVE_V1: Final = MethodDescriptor(
    key="recursive",
    version=1,
    display_name="By headings and paragraphs",
    description=(
        "Splits by headings, then paragraphs and sentences, into passages of a target size. "
        "Uses Azure AI Document Intelligence."
    ),
    parameters_model=RecursiveParametersV1,
    parameter_schema=RECURSIVE_V1_SCHEMA,
    supported_content_types=ALL_CONTENT_TYPES,
)

METHOD_DESCRIPTORS: Final[Mapping[tuple[str, int], MethodDescriptor]] = MappingProxyType(
    {(descriptor.key, descriptor.version): descriptor for descriptor in (PAGEWISE_V1, RECURSIVE_V1)}
)
"""Every shipped descriptor by (`key`, `version`): one per catalogue row."""


def descriptor_for(method_key: str, method_version: int) -> MethodDescriptor:
    """The shipped descriptor of a catalogue row; `KeyError` for one this release lacks."""
    return METHOD_DESCRIPTORS[(method_key, method_version)]


def catalogue_row(descriptor: MethodDescriptor) -> dict[str, Any]:
    """The `app.ingestion_methods` values a descriptor's migration row holds (section 9.2)."""
    return {
        "method_key": descriptor.key,
        "method_version": descriptor.version,
        "display_name": descriptor.display_name,
        "description": descriptor.description,
        "parameter_schema": descriptor.parameter_schema,
        "default_parameters": compact_json(descriptor.default_parameters().dumped()),
        "supported_content_types": compact_json(
            [str(content_type) for content_type in descriptor.supported_content_types]
        ),
    }


class MethodSnapshot(BaseModel):
    """The method a build runs with, as an item's plan carries it: `{key, version, parameters,
    parameterHash}`, read from the logical index inside Validate's transaction, so an edit to
    the index during a run never changes a build in progress (ADR 0046 item 7)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    key: Annotated[str, Field(pattern=METHOD_KEY_PATTERN)]
    version: Annotated[int, Field(ge=1)]
    parameters: dict[str, Any]
    parameter_hash: Sha256Hex

    def descriptor(self) -> MethodDescriptor:
        return descriptor_for(self.key, self.version)

    def validated_parameters(self) -> MethodParameters:
        """The parameters read through the method's model again, so an implementation receives
        a typed value; refuses a snapshot whose hash doesn't match its parameters."""
        descriptor = self.descriptor()
        parameters = descriptor.validate_parameters(self.parameters)
        if descriptor.parameter_hash(parameters).hex() != self.parameter_hash:
            raise ValueError("the snapshot's hash doesn't match its parameters")
        return parameters


@dataclass(frozen=True, slots=True)
class BuildKey:
    """A chunk set's build key: the columns of `UX_chunk_sets_build_key` (section 9.4). One
    build in `building`, `ready` or `served` exists per key, so a retry reuses its row and a
    re-ingest whose key matches the served set is skipped."""

    document_version_id: UUID
    physical_index_id: UUID
    method_key: str
    method_version: int
    parameter_hash: bytes

    @classmethod
    def of(cls, document_version_id: UUID, physical_index_id: UUID, method: MethodSnapshot) -> Self:
        return cls(
            document_version_id=document_version_id,
            physical_index_id=physical_index_id,
            method_key=method.key,
            method_version=method.version,
            parameter_hash=bytes.fromhex(method.parameter_hash),
        )

    def columns(self) -> dict[str, Any]:
        """The key as `app.chunk_sets` column values."""
        return {
            "document_version_id": self.document_version_id,
            "physical_index_id": self.physical_index_id,
            "method_key": self.method_key,
            "method_version": self.method_version,
            "parameter_hash": self.parameter_hash,
        }


# --- The method interface -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageRange:
    """Pages `first_page` to `last_page` of the version's PDF, 1-based and inclusive."""

    first_page: int
    last_page: int

    def __post_init__(self) -> None:
        if type(self.first_page) is not int or type(self.last_page) is not int:
            raise TypeError("page numbers are integers")
        if not 1 <= self.first_page <= self.last_page:
            raise ValueError("a page range runs from page 1 up, first before last")


@dataclass(frozen=True, slots=True)
class ExtractSource:
    """What `extract_plan` needs: the version's PDF page count (counted by `inspect`) and the
    item's `pagewisePagesPerExtract`."""

    page_count: int
    pages_per_extract: int

    def __post_init__(self) -> None:
        if self.page_count < 1 or self.pages_per_extract < 1:
            raise ValueError("a PDF has at least one page and a part at least one page")


@dataclass(frozen=True, slots=True)
class ExtractPlan:
    """The parts Extract parses, in order: the whole document as one part, or page ranges that
    cover pages 1 to `page_count` without a gap."""

    parts: tuple[PageRange, ...]

    def __post_init__(self) -> None:
        if not self.parts or self.parts[0].first_page != 1:
            raise ValueError("a plan starts at page 1")
        for before, after in zip(self.parts, self.parts[1:], strict=False):
            if after.first_page != before.last_page + 1:
                raise ValueError("a plan's parts follow each other without a gap")

    @classmethod
    def whole_document(cls, source: ExtractSource) -> Self:
        return cls(parts=(PageRange(1, source.page_count),))

    @classmethod
    def in_parts(cls, source: ExtractSource) -> Self:
        """Parts of `pages_per_extract` pages, the last one shorter (section 6.3 step 7)."""
        size = source.pages_per_extract
        return cls(
            parts=tuple(
                PageRange(first, min(first + size - 1, source.page_count))
                for first in range(1, source.page_count + 1, size)
            )
        )

    @property
    def page_count(self) -> int:
        return self.parts[-1].last_page

    def first_part(self) -> PageRange:
        return self.parts[0]

    def next_part(self, part: PageRange) -> PageRange | None:
        """The part after `part`, or `None` after the last (Extract's `nextPart`)."""
        index = self.parts.index(part)
        return self.parts[index + 1] if index + 1 < len(self.parts) else None


@dataclass(frozen=True, slots=True)
class MethodContext:
    """What an implementation receives about the build: blob paths, IDs and settings, never
    document content (section 9.1 item 2). `pdf_path` names the PDF the build reads in
    `pdf_container` (the version's source, or a work or stored converted PDF) and `pdf_sha256`
    the bytes expected there."""

    run_id: UUID
    item_id: UUID
    index_id: UUID
    document_id: UUID
    version_id: UUID
    version_number: int
    chunk_set_id: UUID
    work_prefix: str
    pdf_container: BlobContainer
    pdf_path: str
    pdf_sha256: str
    page_count: int
    title: str
    encoding: TokenEncoding
    settings: ItemSettings

    def __post_init__(self) -> None:
        # Paths reach an implementation from a plan, so they are checked again before use.
        if self.work_prefix != work_prefix(self.run_id, self.document_id, self.version_number):
            raise ValueError("the work prefix belongs to this run, document and version")
        validate_blob_path(self.pdf_container, self.pdf_path)
        if self.page_count < 1:
            raise ValueError("a PDF has at least one page")


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """One Extract call's result: the pages Document Intelligence analysed and the warnings."""

    doc_intel_pages: int = 0
    warnings: tuple[ItemWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class EnrichResult:
    """One Enrich call's counts and tokens; `done` once every table has its summary line."""

    done: bool
    tables: int = 0
    summarised: int = 0
    filtered: int = 0
    discarded: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    warnings: tuple[ItemWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """Chunk's result: the number of chunks written to the chunks file."""

    chunk_count: int


_METHOD_FAILURE_KINDS: Final = frozenset({ItemCodeKind.FAILURE, ItemCodeKind.FAILURE_AFTER_RETRIES})


class MethodFailedError(Exception):
    """An expected failure of an implementation, with its item code (`no_extractable_text`,
    `docintel_throttled` and the others of section 17.2) and the `Retry-After` the service gave;
    the activity returns it as its `ActivityResult`. The message is the code alone."""

    def __init__(self, code: str, *, retry_after_seconds: int | None = None) -> None:
        entry = ITEM_CODES.get(code)
        if entry is None or entry.kind not in _METHOD_FAILURE_KINDS:
            raise ValueError("a method fails with an item failure code")
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("Retry-After is never negative")
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@runtime_checkable
class IngestionMethod(Protocol):
    """An implementation in the ingestion app (section 9.1 item 2). Extract calls `extract_plan`
    and `extract`, Enrich calls `enrich`, Chunk calls `chunk`; each reads and writes its work
    files through the paths of `ctx`."""

    def extract_plan(self, source: ExtractSource) -> ExtractPlan: ...

    async def extract(
        self, ctx: MethodContext, params: MethodParameters, part: PageRange
    ) -> ExtractResult: ...

    async def enrich(self, ctx: MethodContext, params: MethodParameters) -> EnrichResult: ...

    async def chunk(self, ctx: MethodContext, params: MethodParameters) -> ChunkResult: ...


class DuplicateMethodError(ValueError):
    """A second registration of one (`key`, `version`)."""


@dataclass(frozen=True, slots=True)
class RegisteredMethod:
    descriptor: MethodDescriptor
    implementation: IngestionMethod


@dataclass(slots=True)
class MethodRegistry:
    """The implementations a process runs, keyed by (`key`, `version`) (section 9.1 item 3)."""

    _methods: dict[tuple[str, int], RegisteredMethod] = field(default_factory=dict)

    def register(self, descriptor: MethodDescriptor, implementation: IngestionMethod) -> None:
        """Register an implementation against its descriptor; a duplicate raises."""
        key = (descriptor.key, descriptor.version)
        if key in self._methods:
            raise DuplicateMethodError(f"{descriptor.key} {descriptor.version} is registered")
        if not isinstance(implementation, IngestionMethod):
            raise TypeError("an implementation provides the IngestionMethod interface")
        self._methods[key] = RegisteredMethod(descriptor, implementation)

    def get(self, method_key: str, method_version: int) -> RegisteredMethod:
        """The registration of (`key`, `version`); `KeyError` when none."""
        return self._methods[(method_key, method_version)]

    def __contains__(self, key: object) -> bool:
        return key in self._methods

    def missing(self, rows: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
        """The catalogue rows that have no registered implementation (start-up check 5)."""
        return [row for row in rows if row not in self._methods]


# --- Catalogue reads ------------------------------------------------------------------------------


def read_active_methods(connection: Connection) -> list[tuple[str, int]]:
    """`SELECT method_key, method_version FROM app.ingestion_methods WHERE is_active = 1`."""
    statement = select(ingestion_methods.c.method_key, ingestion_methods.c.method_version).where(
        ingestion_methods.c.is_active == BIT_TRUE
    )
    return [(row.method_key, row.method_version) for row in connection.execute(statement)]


def resolve_method(
    connection: Connection, method_key: str, method_version: int | None
) -> MethodDescriptor:
    """The descriptor an index create or update names: with no version, the key's highest
    active version. Refuses `method_not_found` for no catalogue row (or none this release ships)
    and `method_inactive` for a row that can't be chosen (section 8.3)."""
    statement = select(ingestion_methods.c.method_version, ingestion_methods.c.is_active).where(
        ingestion_methods.c.method_key == method_key
    )
    rows = {row.method_version: bool(row.is_active) for row in connection.execute(statement)}
    if method_version is None:
        active = [version for version, is_active in rows.items() if is_active]
        if not active:
            raise IngestionRefusedError("method_inactive" if rows else "method_not_found")
        method_version = max(active)
    elif method_version not in rows:
        raise IngestionRefusedError("method_not_found")
    elif not rows[method_version]:
        raise IngestionRefusedError("method_inactive")
    descriptor = METHOD_DESCRIPTORS.get((method_key, method_version))
    if descriptor is None:
        raise IngestionRefusedError("method_not_found")
    return descriptor
