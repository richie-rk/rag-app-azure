"""Unit tests of the method catalogue and interface (T-088, T-141, T-180, T-181; `14-ingestion.md`
section 9; `80-data-model.md` section 2.9; ADR 0046)."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, ValidationError
from sqlalchemy import Connection
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.ingestion.methods import (
    METHOD_DESCRIPTORS,
    PAGEWISE_V1,
    RECURSIVE_V1,
    BuildKey,
    ChunkResult,
    DuplicateMethodError,
    EnrichResult,
    ExtractPlan,
    ExtractResult,
    ExtractSource,
    IngestionMethod,
    MethodContext,
    MethodDescriptor,
    MethodFailedError,
    MethodParameters,
    MethodRegistry,
    MethodSnapshot,
    PageRange,
    PagewiseParametersV1,
    RecursiveParametersV1,
    catalogue_row,
    compact_json,
    descriptor_for,
    parameter_hash,
    read_active_methods,
    resolve_method,
)
from docchat_core.ingestion.models import ContentType, IngestionRefusedError, ItemSettings
from docchat_core.ingestion.names import BlobContainer, source_path, work_prefix
from docchat_core.ingestion.settings import TokenEncoding

REVISION = Path("db", "migrations", "versions", "0001_initial_schema.py")


def _revision_tree() -> ast.Module:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / REVISION
        if candidate.is_file():
            return ast.parse(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError(str(REVISION))


def migration_rows() -> list[dict[str, Any]]:
    """The `app.ingestion_methods` rows revision 0001 seeds, as the stored text: its
    `INGESTION_METHODS` tuple with the schema constants it names, every JSON value compacted in
    key order as the revision's `_compact` does, and `CONTENT_TYPES` as each row's types."""
    constants: dict[str, Any] = {}
    methods: ast.expr | None = None
    for node in _revision_tree().body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            if node.target.id == "INGESTION_METHODS":
                methods = node.value
            else:
                try:
                    constants[node.target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    assert isinstance(methods, ast.Tuple)
    rows: list[dict[str, Any]] = []
    for entry in methods.elts:
        assert isinstance(entry, ast.Dict)
        row: dict[str, Any] = {}
        for key, value in zip(entry.keys, entry.values, strict=True):
            assert isinstance(key, ast.Constant)
            assert isinstance(key.value, str)
            row[key.value] = (
                constants[value.id] if isinstance(value, ast.Name) else ast.literal_eval(value)
            )
        row["parameter_schema"] = compact_json(row["parameter_schema"])
        row["default_parameters"] = compact_json(row["default_parameters"])
        row["supported_content_types"] = compact_json(constants["CONTENT_TYPES"])
        rows.append(row)
    return rows


# --- Catalogue rows (T-088) -----------------------------------------------------------------------


def test_t088_descriptors_equal_their_migration_rows() -> None:
    rows = migration_rows()
    assert [catalogue_row(descriptor) for descriptor in METHOD_DESCRIPTORS.values()] == rows
    assert list(METHOD_DESCRIPTORS) == [("pagewise", 1), ("recursive", 1)]


def test_the_seeded_defaults_are_the_documented_ones() -> None:
    assert catalogue_row(PAGEWISE_V1)["default_parameters"] == (
        '{"summarizeTables":true,"ocrLanguages":["eng"]}'
    )
    assert catalogue_row(RECURSIVE_V1)["default_parameters"] == (
        '{"targetTokens":512,"overlapPercent":25,"tableMaxTokens":1600}'
    )
    assert descriptor_for("recursive", 1) is RECURSIVE_V1
    with pytest.raises(KeyError):
        descriptor_for("recursive", 2)


# --- Parameter hash (T-141) -----------------------------------------------------------------------


def test_t141_the_hash_is_the_documented_sha256() -> None:
    parameters = RECURSIVE_V1.default_parameters()
    expected = json.dumps(
        {
            "key": "recursive",
            "version": 1,
            "parameters": {"targetTokens": 512, "overlapPercent": 25, "tableMaxTokens": 1600},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = parameter_hash("recursive", 1, parameters)
    assert len(digest) == 32
    assert digest == hashlib.sha256(expected.encode("utf-8")).digest()
    assert RECURSIVE_V1.parameter_hash(parameters) == digest


def test_t141_equal_parameters_hash_equally_in_any_key_order() -> None:
    first = RECURSIVE_V1.validate_parameters(
        {"targetTokens": 600, "overlapPercent": 10, "tableMaxTokens": 2000}
    )
    second = RECURSIVE_V1.validate_parameters(
        {"tableMaxTokens": 2000, "overlapPercent": 10, "targetTokens": 600}
    )
    assert RECURSIVE_V1.parameter_hash(first) == RECURSIVE_V1.parameter_hash(second)


def test_t141_the_key_and_version_change_the_hash() -> None:
    parameters = RECURSIVE_V1.default_parameters()
    assert parameter_hash("recursive", 1, parameters) != parameter_hash("recursive", 2, parameters)
    assert parameter_hash("recursive", 1, parameters) != parameter_hash("other", 1, parameters)


def test_t141_the_hash_is_computed_after_defaults_are_filled() -> None:
    partial = RECURSIVE_V1.validate_parameters({"targetTokens": 512})
    assert RECURSIVE_V1.parameter_hash(partial) == RECURSIVE_V1.parameter_hash(
        RECURSIVE_V1.validate_parameters(None)
    )
    assert PAGEWISE_V1.parameter_hash(
        PAGEWISE_V1.validate_parameters({})
    ) == PAGEWISE_V1.parameter_hash(PAGEWISE_V1.default_parameters())


# --- Schemas against models (T-180, T-181) --------------------------------------------------------

PAGEWISE_CASES: list[dict[str, Any]] = [
    {"summarizeTables": False},
    {"summarizeTables": "true"},
    {"summarizeTables": 1},
    {"summarizeTables": None},
    {"ocrLanguages": ["eng", "fra", "deu"]},
    {"ocrLanguages": ["por"]},
    {"ocrLanguages": []},
    {"ocrLanguages": ["eng", "fra", "deu", "spa"]},
    {"ocrLanguages": ["eng", "eng"]},
    {"ocrLanguages": ["xxx"]},
    {"ocrLanguages": "eng"},
    {"ocrLanguages": [1]},
    {"ocrLanguages": ["ENG"]},
    {"extra": 1},
]
RECURSIVE_CASES: list[dict[str, Any]] = [
    {"targetTokens": 128},
    {"targetTokens": 2048},
    {"targetTokens": 127},
    {"targetTokens": 2049},
    {"targetTokens": 512.0},
    {"targetTokens": 512.5},
    {"targetTokens": "512"},
    {"targetTokens": True},
    {"targetTokens": None},
    {"overlapPercent": 0},
    {"overlapPercent": 50},
    {"overlapPercent": 5},
    {"overlapPercent": 3},
    {"overlapPercent": -5},
    {"overlapPercent": 55},
    {"overlapPercent": 25.0},
    {"tableMaxTokens": 256},
    {"tableMaxTokens": 6000},
    {"tableMaxTokens": 255},
    {"tableMaxTokens": 6001},
    {"target_tokens": 512},
]


def _model_accepts(descriptor: MethodDescriptor, value: dict[str, Any]) -> bool:
    try:
        descriptor.validate_parameters(value)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize(
    ("descriptor", "case"),
    [(PAGEWISE_V1, case) for case in PAGEWISE_CASES]
    + [(RECURSIVE_V1, case) for case in RECURSIVE_CASES],
)
def test_t180_schema_and_model_agree_on_bounds(
    descriptor: MethodDescriptor, case: dict[str, Any]
) -> None:
    schema = json.loads(descriptor.parameter_schema)
    validator = Draft202012Validator(schema)
    # The form always sends every field, so each case changes one field of the defaults.
    value = {**descriptor.default_parameters().dumped(), **case}
    assert validator.is_valid(value) == _model_accepts(descriptor, value), case


@pytest.mark.parametrize("descriptor", list(METHOD_DESCRIPTORS.values()))
def test_t180_schema_and_model_agree_on_defaults(descriptor: MethodDescriptor) -> None:
    schema = json.loads(descriptor.parameter_schema)
    Draft202012Validator.check_schema(schema)
    defaults = descriptor.default_parameters().dumped()
    assert {name: spec["default"] for name, spec in schema["properties"].items()} == defaults
    assert list(schema["properties"]) == list(defaults)
    assert Draft202012Validator(schema).is_valid(defaults)


_PROPERTY_KEYS = {"type", "title", "description", "default"}
_KEYS_BY_TYPE = {
    "integer": {"minimum", "maximum", "multipleOf"},
    "number": {"minimum", "maximum", "multipleOf"},
    "boolean": set(),
    "string": {"enum", "oneOf", "minLength", "maxLength"},
    "array": {"items", "minItems", "maxItems", "uniqueItems"},
}


def _enumerated_string(schema: dict[str, Any]) -> bool:
    if schema.get("type") != "string":
        return False
    if set(schema) == {"type", "enum"}:
        return all(isinstance(value, str) for value in schema["enum"])
    if set(schema) == {"type", "oneOf"}:
        return all(
            set(option) == {"const", "title"} and isinstance(option["const"], str)
            for option in schema["oneOf"]
        )
    return False


def subset_violations(schema: dict[str, Any]) -> list[str]:
    """Where a parameter schema leaves the admin form's subset of `14-ingestion.md` 9.3."""
    found: list[str] = []
    if set(schema) - {"type", "properties", "required", "additionalProperties"}:
        found.append("root keys")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        found.append("root shape")
    for name, spec in schema.get("properties", {}).items():
        kind = spec.get("type")
        if kind not in _KEYS_BY_TYPE:
            found.append(f"{name}: type")
            continue
        if set(spec) - _PROPERTY_KEYS - _KEYS_BY_TYPE[kind]:
            found.append(f"{name}: keys")
        if kind == "string" and not ({"enum", "oneOf"} & set(spec) or "minLength" in spec):
            found.append(f"{name}: string form")
        if kind == "array" and (
            spec.get("uniqueItems") is not True or not _enumerated_string(spec.get("items", {}))
        ):
            found.append(f"{name}: array form")
    return found


@pytest.mark.parametrize("descriptor", list(METHOD_DESCRIPTORS.values()))
def test_t181_every_schema_stays_in_the_forms_subset(descriptor: MethodDescriptor) -> None:
    assert subset_violations(json.loads(descriptor.parameter_schema)) == []


def test_t181_the_subset_check_finds_violations() -> None:
    assert subset_violations({"type": "object", "properties": {}, "$defs": {}}) == [
        "root keys",
        "root shape",
    ]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "a": {"type": "object"},
            "b": {"type": "integer", "pattern": "x"},
            "c": {"type": "string"},
            "d": {"type": "array", "items": {"type": "string"}},
        },
    }
    assert subset_violations(schema) == ["a: type", "b: keys", "c: string form", "d: array form"]
    assert _enumerated_string({"type": "string", "enum": ["a"]})
    assert not _enumerated_string({"type": "integer"})


# --- Descriptors and parameters -------------------------------------------------------------------


def test_parameters_are_read_by_camel_case_names_only() -> None:
    assert RECURSIVE_V1.validate_parameters(None) == RecursiveParametersV1()
    with pytest.raises(ValidationError):
        RECURSIVE_V1.validate_parameters({"target_tokens": 600})
    parameters = PAGEWISE_V1.validate_parameters({"ocrLanguages": ["fra", "eng"]})
    assert isinstance(parameters, PagewiseParametersV1)
    assert parameters.dumped() == {"summarizeTables": True, "ocrLanguages": ["fra", "eng"]}
    assert RecursiveParametersV1(target_tokens=600).target_tokens == 600


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"key": "Bad-Key"}, ValueError),
        ({"key": "x" * 65}, ValueError),
        ({"version": 0}, ValueError),
        ({"version": True}, ValueError),
        ({"parameter_schema": "[]"}, TypeError),
        ({"supported_content_types": ()}, ValueError),
    ],
)
def test_a_descriptor_checks_its_fields(change: dict[str, Any], error: type[Exception]) -> None:
    fields: dict[str, Any] = {
        "key": "pagewise",
        "version": 1,
        "display_name": "x",
        "description": "y",
        "parameters_model": PagewiseParametersV1,
        "parameter_schema": PAGEWISE_V1.parameter_schema,
        "supported_content_types": (ContentType.PDF,),
    }
    with pytest.raises(error):
        MethodDescriptor(**{**fields, **change})


def test_a_parameter_model_must_forbid_unknown_fields() -> None:
    class Loose(MethodParameters):
        model_config = ConfigDict(extra="ignore")

    with pytest.raises(ValueError, match="forbids"):
        MethodDescriptor(
            key="loose",
            version=1,
            display_name="x",
            description="y",
            parameters_model=Loose,
            parameter_schema="{}",
            supported_content_types=(ContentType.PDF,),
        )


def test_a_snapshot_round_trips_and_checks_its_hash() -> None:
    parameters = RECURSIVE_V1.validate_parameters({"targetTokens": 1024})
    snapshot = RECURSIVE_V1.snapshot(parameters)
    assert snapshot.model_dump(mode="json") == {
        "key": "recursive",
        "version": 1,
        "parameters": {"targetTokens": 1024, "overlapPercent": 25, "tableMaxTokens": 1600},
        "parameterHash": RECURSIVE_V1.parameter_hash(parameters).hex(),
    }
    assert MethodSnapshot.model_validate(snapshot.model_dump(mode="json")) == snapshot
    assert snapshot.descriptor() is RECURSIVE_V1
    assert snapshot.validated_parameters() == parameters
    tampered = snapshot.model_copy(update={"parameters": {"targetTokens": 2048}})
    with pytest.raises(ValueError, match="hash"):
        tampered.validated_parameters()
    with pytest.raises(TypeError):
        RECURSIVE_V1.snapshot(PAGEWISE_V1.default_parameters())


def test_a_build_key_is_the_unique_index_columns() -> None:
    version, physical = uuid4(), uuid4()
    snapshot = PAGEWISE_V1.snapshot(PAGEWISE_V1.default_parameters())
    key = BuildKey.of(version, physical, snapshot)
    assert key.columns() == {
        "document_version_id": version,
        "physical_index_id": physical,
        "method_key": "pagewise",
        "method_version": 1,
        "parameter_hash": bytes.fromhex(snapshot.parameter_hash),
    }


# --- The interface --------------------------------------------------------------------------------


def test_page_ranges_and_plans() -> None:
    source = ExtractSource(page_count=95, pages_per_extract=40)
    plan = ExtractPlan.in_parts(source)
    assert plan.parts == (PageRange(1, 40), PageRange(41, 80), PageRange(81, 95))
    assert plan.page_count == 95
    assert plan.first_part() == PageRange(1, 40)
    assert plan.next_part(PageRange(41, 80)) == PageRange(81, 95)
    assert plan.next_part(PageRange(81, 95)) is None
    whole = ExtractPlan.whole_document(source)
    assert whole.parts == (PageRange(1, 95),)
    assert ExtractPlan.in_parts(ExtractSource(page_count=40, pages_per_extract=40)).parts == (
        PageRange(1, 40),
    )


@pytest.mark.parametrize(("first", "last"), [(0, 1), (3, 2), (-1, 4)])
def test_a_page_range_runs_forward_from_page_one(first: int, last: int) -> None:
    with pytest.raises(ValueError, match="page range"):
        PageRange(first, last)


def test_page_numbers_are_integers() -> None:
    with pytest.raises(TypeError):
        PageRange(1.0, 2)  # type: ignore[arg-type]


def test_a_plan_covers_its_pages_without_a_gap() -> None:
    with pytest.raises(ValueError, match="page 1"):
        ExtractPlan(parts=())
    with pytest.raises(ValueError, match="page 1"):
        ExtractPlan(parts=(PageRange(2, 3),))
    with pytest.raises(ValueError, match="gap"):
        ExtractPlan(parts=(PageRange(1, 3), PageRange(5, 6)))
    with pytest.raises(ValueError, match="page"):
        ExtractSource(page_count=0, pages_per_extract=40)


def item_settings() -> ItemSettings:
    return ItemSettings(
        activity_time_budget_seconds=1200,
        max_pages_per_document=2000,
        pagewise_pages_per_extract=40,
        max_parse_seconds_per_document=7200,
        embedding_batch_max_inputs=64,
        embedding_batch_max_tokens=60000,
        embedding_concurrency=2,
        index_batch_max_documents=100,
        index_batch_max_bytes=8_000_000,
        table_summary_concurrency=4,
        table_summary_max_input_tokens=6000,
        table_summary_max_output_tokens=300,
    )


def method_context(**overrides: Any) -> MethodContext:
    run, index, document = uuid4(), uuid4(), uuid4()
    fields: dict[str, Any] = {
        "run_id": run,
        "item_id": uuid4(),
        "index_id": index,
        "document_id": document,
        "version_id": uuid4(),
        "version_number": 3,
        "chunk_set_id": uuid4(),
        "work_prefix": work_prefix(run, document, 3),
        "pdf_container": BlobContainer.DOCUMENTS,
        "pdf_path": source_path(index, document, 3),
        "pdf_sha256": "0" * 64,
        "page_count": 12,
        "title": "Report",
        "encoding": TokenEncoding.CL100K_BASE,
        "settings": item_settings(),
    }
    fields.update(overrides)
    return MethodContext(**fields)


def test_a_context_carries_checked_paths() -> None:
    context = method_context()
    assert context.page_count == 12
    with pytest.raises(ValueError, match="work prefix"):
        method_context(work_prefix=work_prefix(uuid4(), uuid4(), 3))
    with pytest.raises(ValueError, match="blob path"):
        method_context(pdf_path="elsewhere/source")
    with pytest.raises(ValueError, match="page"):
        method_context(page_count=0)


def test_a_method_failure_names_a_failure_code() -> None:
    failure = MethodFailedError("docintel_throttled", retry_after_seconds=20)
    assert (failure.code, failure.retry_after_seconds, str(failure)) == (
        "docintel_throttled",
        20,
        "docintel_throttled",
    )
    assert MethodFailedError("no_extractable_text").retry_after_seconds is None
    with pytest.raises(ValueError, match="failure code"):
        MethodFailedError("already_served")
    with pytest.raises(ValueError, match="never negative"):
        MethodFailedError("docintel_throttled", retry_after_seconds=-1)


class _FakeMethod:
    def extract_plan(self, source: ExtractSource) -> ExtractPlan:
        return ExtractPlan.whole_document(source)

    async def extract(
        self, ctx: MethodContext, params: MethodParameters, part: PageRange
    ) -> ExtractResult:
        del ctx, params, part
        return ExtractResult(doc_intel_pages=3)

    async def enrich(self, ctx: MethodContext, params: MethodParameters) -> EnrichResult:
        del ctx, params
        return EnrichResult(done=True)

    async def chunk(self, ctx: MethodContext, params: MethodParameters) -> ChunkResult:
        del ctx, params
        return ChunkResult(chunk_count=4)


async def test_the_registry_keys_implementations_by_key_and_version() -> None:
    registry = MethodRegistry()
    implementation = _FakeMethod()
    assert isinstance(implementation, IngestionMethod)
    registry.register(RECURSIVE_V1, implementation)
    assert ("recursive", 1) in registry
    registered = registry.get("recursive", 1)
    assert registered.descriptor is RECURSIVE_V1
    result = await registered.implementation.chunk(method_context(), RecursiveParametersV1())
    assert result.chunk_count == 4
    assert registry.missing([("recursive", 1), ("pagewise", 1)]) == [("pagewise", 1)]
    with pytest.raises(DuplicateMethodError):
        registry.register(RECURSIVE_V1, _FakeMethod())
    with pytest.raises(KeyError):
        registry.get("pagewise", 1)
    with pytest.raises(TypeError):
        registry.register(PAGEWISE_V1, object())  # type: ignore[arg-type]


async def test_the_fake_methods_other_steps() -> None:
    method = _FakeMethod()
    context = method_context()
    plan = method.extract_plan(ExtractSource(page_count=5, pages_per_extract=2))
    assert plan.parts == (PageRange(1, 5),)
    assert (
        await method.extract(context, PagewiseParametersV1(), plan.parts[0])
    ).doc_intel_pages == 3
    assert (await method.enrich(context, PagewiseParametersV1())).done


# --- Catalogue reads ------------------------------------------------------------------------------


class _Rows:
    """A connection that answers one statement with fixed rows and records its SQL."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    def execute(
        self, statement: ClauseElement, parameters: object = None
    ) -> Iterator[SimpleNamespace]:
        del parameters
        dialect = mssql.dialect()  # type: ignore[no-untyped-call]
        self.sql.append(str(statement.compile(dialect=dialect)))
        return iter(self.rows)


def _connection(rows: list[SimpleNamespace]) -> tuple[Connection, _Rows]:
    fake = _Rows(rows)
    return cast("Connection", fake), fake


def test_read_active_methods() -> None:
    connection, fake = _connection([SimpleNamespace(method_key="recursive", method_version=1)])
    assert read_active_methods(connection) == [("recursive", 1)]
    assert "is_active = 1" in fake.sql[0]


def _version(version: int, *, active: bool) -> SimpleNamespace:
    return SimpleNamespace(method_version=version, is_active=active)


def test_resolve_method_takes_the_highest_active_version() -> None:
    connection, _ = _connection([_version(1, active=True), _version(2, active=False)])
    assert resolve_method(connection, "recursive", None) is RECURSIVE_V1
    connection, _ = _connection([_version(1, active=True)])
    assert resolve_method(connection, "recursive", 1) is RECURSIVE_V1


@pytest.mark.parametrize(
    ("rows", "version", "code"),
    [
        ([], None, "method_not_found"),
        ([], 1, "method_not_found"),
        ([_version(1, active=False)], None, "method_inactive"),
        ([_version(1, active=False)], 1, "method_inactive"),
        ([_version(1, active=True)], 2, "method_not_found"),
        ([_version(7, active=True)], None, "method_not_found"),
    ],
)
def test_resolve_method_refusals(
    rows: list[SimpleNamespace], version: int | None, code: str
) -> None:
    connection, _ = _connection(rows)
    with pytest.raises(IngestionRefusedError) as refused:
        resolve_method(connection, "recursive", version)
    assert refused.value.code == code


def test_content_types_are_the_three_media_types() -> None:
    for descriptor in METHOD_DESCRIPTORS.values():
        assert descriptor.supported_content_types == (
            ContentType.PDF,
            ContentType.DOCX,
            ContentType.PPTX,
        )
