"""`validate_item`, the Validate step (`14-ingestion.md` sections 6.2, 5.4, 5.7 and 4.8; ADRs
0040, 0041, 0046 and 0152).

One transaction re-reads every row the item depends on and decides: skip (the target was
repointed or retired, the document deleted, the version superseded or not ready, a newer commit,
the result already served) or build. A build reuses the chunk set of its build key or inserts
one, links it to the item under the item's claim, moves a `pending` version to `ingesting` and
plans the conversion of a DOCX or PPTX source, or the reuse of its stored converted PDF. The
claim's scan gate admitted the version (section 5.4 item 4): a version still `pending` here is
`scan_required = 0` or `clean`, and one that left `pending` is decided by its state, so Validate
never reads a scan tag.

A unique violation of `UX_chunk_sets_build_key` (two builds racing) rolls back and the
transaction runs once more, which finds the row; a second violation is `internal_error`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, Self
from uuid import UUID

from opentelemetry.trace import Span
from pydantic import Field, model_validator
from sqlalchemy import Connection, and_, insert, or_, select, update

from docchat_core.errors import ITEM_CODES, ItemCodeKind
from docchat_core.ingestion.methods import MethodSnapshot, descriptor_for
from docchat_core.ingestion.models import ActivityResult, ContentType, ItemEnvelope, ItemStage
from docchat_core.ingestion.names import converted_path, document_title, work_prefix
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.sql.engine import SqlUniqueViolation
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_ingestion.activities.documents import (
    DEADLINES,
    OFFICE_TYPES,
    ActivityOutput,
    BuildPlan,
    ConversionPlan,
    DocumentServices,
    LeaseLost,
    PhysicalIndexPlan,
    ServicesProvider,
    blueprint,
    document_services,
    fence,
    run_document_activity,
)
from docchat_ingestion.activities.support import literal, literals

NAME: Final = "validate_item"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
LOCK_ROW: Final = "WITH (UPDLOCK, ROWLOCK)"
BUILD_KEY_INDEX: Final = "UX_chunk_sets_build_key"

_items = ingestion_run_items
_versions = document_versions

type SkipCode = Literal[
    "already_served",
    "superseded_by_newer",
    "version_not_ready",
    "document_deleted",
    "target_retired",
    "target_repointed",
]


def _skip_code(value: str | None) -> str | None:
    if value is None:
        return None
    entry = ITEM_CODES.get(value)
    if entry is None or entry.kind not in {ItemCodeKind.SKIP, ItemCodeKind.SKIP_OR_CANCEL}:
        raise ValueError("a skip names a skip code")
    return value


class ValidateResult(ActivityResult):
    """`ValidatePlan` with the `ActivityResult` fields: a skip carries its code, a build every
    field of the plan (section 6.2)."""

    action: Literal["build", "skip"] | None = None
    skip_code: SkipCode | None = None
    chunk_set_id: UUID | None = None
    index_id: UUID | None = None
    document_id: UUID | None = None
    version_id: UUID | None = None
    version_number: int | None = Field(default=None, ge=1)
    content_type: ContentType | None = None
    source_blob_path: str | None = None
    source_sha256: str | None = None
    conversion: ConversionPlan | None = None
    title: str | None = None
    work_prefix: str | None = None
    physical_index: PhysicalIndexPlan | None = None
    method: MethodSnapshot | None = None

    @model_validator(mode="after")
    def _plan_or_skip(self) -> Self:
        if not self.ok:
            return self
        if self.action == "skip":
            if self.skip_code is None:
                raise ValueError("a skip names its code")
        elif self.action == "build":
            BuildPlan.model_validate(self.plan_fields())
        else:
            raise ValueError("a successful Validate builds or skips")
        return self

    def plan_fields(self) -> dict[str, Any]:
        """The plan as the later steps receive it, without the `ActivityResult` fields."""
        dumped = self.model_dump(mode="json")
        for name in ActivityResult.model_fields:
            dumped.pop(ActivityResult.model_fields[name].alias or name, None)
        return dumped

    @classmethod
    def skip(cls, code: SkipCode) -> Self:
        return cls.success(action="skip", skip_code=code)

    @classmethod
    def build(cls, plan: BuildPlan) -> Self:
        return cls.success(**{name: getattr(plan, name) for name in BuildPlan.model_fields})


# --- Rows read ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemRow:
    run_id: UUID
    search_index_id: UUID
    physical_index_id: UUID
    document_id: UUID
    document_version_id: UUID
    chunk_set_id: UUID | None


@dataclass(frozen=True, slots=True)
class VersionRow:
    state: str
    committed_at: Any
    version_number: int
    sha256: bytes
    content_type: ContentType
    source_name: str
    blob_path: str
    converter_version: str | None
    converted_sha256: bytes | None


@dataclass(frozen=True, slots=True)
class TargetRow:
    is_deleted: bool
    current_version_id: UUID | None
    status: str
    active_physical_index_id: UUID | None
    method_key: str
    method_version: int
    method_parameters: str
    method_parameter_hash: bytes
    state: str
    name: str
    schema_version: int
    embedding_model: str
    embedding_dimensions: int
    embedding_encoding: str
    embedding_max_input_tokens: int
    ingestion_embedding_deployment: str


def read_item(connection: Connection, item_id: UUID) -> ItemRow:
    row = connection.execute(
        select(
            _items.c.run_id,
            _items.c.search_index_id,
            _items.c.physical_index_id,
            _items.c.document_id,
            _items.c.document_version_id,
            _items.c.chunk_set_id,
        ).where(_items.c.id == item_id)
    ).one()
    return ItemRow(
        row.run_id,
        row.search_index_id,
        row.physical_index_id,
        row.document_id,
        row.document_version_id,
        row.chunk_set_id,
    )


def read_version(connection: Connection, version_id: UUID) -> VersionRow:
    row = connection.execute(
        select(
            _versions.c.state,
            _versions.c.committed_at,
            _versions.c.version_number,
            _versions.c.sha256,
            _versions.c.content_type,
            _versions.c.source_name,
            _versions.c.blob_path,
            _versions.c.converter_version,
            _versions.c.converted_sha256,
        )
        .with_hint(_versions, LOCK_ROW, "mssql")
        .where(_versions.c.id == version_id)
    ).one()
    return VersionRow(
        row.state,
        row.committed_at,
        row.version_number,
        bytes(row.sha256),
        ContentType(row.content_type),
        row.source_name,
        row.blob_path,
        row.converter_version,
        None if row.converted_sha256 is None else bytes(row.converted_sha256),
    )


def read_target(connection: Connection, document_id: UUID, physical_id: UUID) -> TargetRow:
    """The document, its logical index and the item's physical index, in one read."""
    row = connection.execute(
        select(
            documents.c.is_deleted,
            documents.c.current_version_id,
            search_indexes.c.status,
            search_indexes.c.active_physical_index_id,
            search_indexes.c.method_key,
            search_indexes.c.method_version,
            search_indexes.c.method_parameters,
            search_indexes.c.method_parameter_hash,
            physical_indexes.c.state,
            physical_indexes.c.name,
            physical_indexes.c.schema_version,
            physical_indexes.c.embedding_model,
            physical_indexes.c.embedding_dimensions,
            physical_indexes.c.embedding_encoding,
            physical_indexes.c.embedding_max_input_tokens,
            physical_indexes.c.ingestion_embedding_deployment,
        )
        .select_from(documents)
        .join(search_indexes, search_indexes.c.id == documents.c.search_index_id)
        .join(physical_indexes, physical_indexes.c.id == physical_id)
        .where(documents.c.id == document_id)
    ).one()
    return TargetRow(
        is_deleted=bool(row.is_deleted),
        current_version_id=row.current_version_id,
        status=row.status,
        active_physical_index_id=row.active_physical_index_id,
        method_key=row.method_key,
        method_version=row.method_version,
        method_parameters=row.method_parameters,
        method_parameter_hash=bytes(row.method_parameter_hash),
        state=row.state,
        name=row.name,
        schema_version=row.schema_version,
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        embedding_encoding=row.embedding_encoding,
        embedding_max_input_tokens=row.embedding_max_input_tokens,
        ingestion_embedding_deployment=row.ingestion_embedding_deployment,
    )


# --- Decisions ------------------------------------------------------------------------------------


def covered_elsewhere(
    connection: Connection, version_id: UUID, physical_id: UUID, item_id: UUID
) -> bool:
    """An item of any run other than this one covers the version in that physical index."""
    return (
        connection.execute(
            select(literal("1"))
            .select_from(_items)
            .where(
                _items.c.document_version_id == version_id,
                _items.c.physical_index_id == physical_id,
                _items.c.id != item_id,
                _items.c.state.in_(literals(("queued", "running", "succeeded"))),
            )
            .limit(1)
        ).first()
        is not None
    )


def retarget(connection: Connection, envelope: ItemEnvelope, active_id: UUID) -> None:
    """Re-target the item in place to the active physical index, under its claim."""
    changed = connection.execute(
        update(_items)
        .where(
            _items.c.id == envelope.item_id,
            _items.c.claim_count == envelope.claim_number,
            _items.c.state == literal("running"),
        )
        .values(physical_index_id=active_id)
    ).rowcount
    if changed != 1:
        raise LeaseLost


def newer_commit_exists(
    connection: Connection, document_id: UUID, version_id: UUID, version: VersionRow
) -> bool:
    """A later commit of the document (a later `committed_at`, or the same and a higher version
    number) among `pending`, `ingesting` and `ingested` versions (section 5.7 item 3)."""
    newer = _versions.alias("n")
    return (
        connection.execute(
            select(literal("1"))
            .select_from(newer)
            .where(
                newer.c.document_id == document_id,
                newer.c.id != version_id,
                newer.c.state.in_(literals(("pending", "ingesting", "ingested"))),
                or_(
                    newer.c.committed_at > version.committed_at,
                    and_(
                        newer.c.committed_at == version.committed_at,
                        newer.c.version_number > version.version_number,
                    ),
                ),
            )
            .limit(1)
        ).first()
        is not None
    )


def supersede_pending(connection: Connection, version_id: UUID) -> None:
    connection.execute(
        update(_versions)
        .where(_versions.c.id == version_id, _versions.c.state == literal("pending"))
        .values(state=literal("superseded"), superseded_at=SYSUTCDATETIME)
    )


def already_served(
    connection: Connection, item: ItemRow, physical_id: UUID, target: TargetRow
) -> bool:
    """The target serves this version with this method, version and parameter hash."""
    return (
        connection.execute(
            select(chunk_sets.c.id)
            .select_from(served_chunk_sets)
            .join(chunk_sets, chunk_sets.c.id == served_chunk_sets.c.chunk_set_id)
            .where(
                served_chunk_sets.c.document_id == item.document_id,
                served_chunk_sets.c.physical_index_id == physical_id,
                served_chunk_sets.c.document_version_id == item.document_version_id,
                chunk_sets.c.method_key == target.method_key,
                chunk_sets.c.method_version == target.method_version,
                chunk_sets.c.parameter_hash == target.method_parameter_hash,
            )
        ).first()
        is not None
    )


def build_chunk_set(
    connection: Connection,
    *,
    envelope: ItemEnvelope,
    item: ItemRow,
    physical_id: UUID,
    target: TargetRow,
    new_id: Callable[[], UUID],
) -> UUID:
    """Decision 7: reuse the build key's live chunk set or insert one, retire the set an earlier
    claim linked when the plan now names another build, move a `pending` version to `ingesting`
    and link the set to the item under its claim."""
    found = connection.execute(
        select(chunk_sets.c.id, chunk_sets.c.state)
        .with_hint(chunk_sets, LOCK_ROW, "mssql")
        .where(
            chunk_sets.c.document_version_id == item.document_version_id,
            chunk_sets.c.physical_index_id == physical_id,
            chunk_sets.c.method_key == target.method_key,
            chunk_sets.c.method_version == target.method_version,
            chunk_sets.c.parameter_hash == target.method_parameter_hash,
            chunk_sets.c.state.in_(literals(("building", "ready", "served"))),
        )
    ).first()
    if found is not None:
        chunk_set_id: UUID = found.id
    else:
        chunk_set_id = new_id()
        connection.execute(
            insert(chunk_sets).values(
                id=chunk_set_id,
                document_id=item.document_id,
                document_version_id=item.document_version_id,
                physical_index_id=physical_id,
                method_key=target.method_key,
                method_version=target.method_version,
                parameter_hash=target.method_parameter_hash,
                method_parameters=target.method_parameters,
                embedding_model=target.embedding_model,
                embedding_dimensions=target.embedding_dimensions,
                state=literal("building"),
                build_item_id=envelope.item_id,
            )
        )
    if item.chunk_set_id is not None and item.chunk_set_id != chunk_set_id:
        connection.execute(
            update(chunk_sets)
            .where(
                chunk_sets.c.id == item.chunk_set_id,
                chunk_sets.c.id != chunk_set_id,
                chunk_sets.c.state.in_(literals(("building", "ready"))),
            )
            .values(state=literal("retired"), retired_at=SYSUTCDATETIME)
        )
    connection.execute(
        update(_versions)
        .where(_versions.c.id == item.document_version_id, _versions.c.state == literal("pending"))
        .values(state=literal("ingesting"), ingest_started_at=SYSUTCDATETIME)
    )
    linked = connection.execute(
        update(_items)
        .where(
            _items.c.id == envelope.item_id,
            _items.c.claim_count == envelope.claim_number,
            _items.c.state == literal("running"),
        )
        .values(chunk_set_id=chunk_set_id)
    ).rowcount
    if linked != 1:
        raise LeaseLost
    return chunk_set_id


def conversion_plan(
    item: ItemRow,
    version: VersionRow,
    target: TargetRow,
    physical_id: UUID,
    converter_version: str,
) -> ConversionPlan | None:
    """Decision 8: a PDF version has none; a DOCX or PPTX version reuses its stored converted PDF
    under the deployed converter, or while it builds into the `building` index beside an active
    one (both indexes then cite one pagination); otherwise it converts."""
    if version.content_type not in OFFICE_TYPES:
        return None
    path = converted_path(item.search_index_id, item.document_id, version.version_number)
    building_beside_active = (
        target.state == "building"
        and target.active_physical_index_id is not None
        and target.active_physical_index_id != physical_id
    )
    if version.converted_sha256 is not None and (
        version.converter_version == converter_version or building_beside_active
    ):
        return ConversionPlan(
            convert=False, converted_blob_path=path, converted_sha256=version.converted_sha256.hex()
        )
    return ConversionPlan(convert=True, converted_blob_path=path, converted_sha256=None)


@dataclass(frozen=True, slots=True)
class Decision:
    skip: SkipCode | None = None
    plan: BuildPlan | None = None


def decide(  # noqa: PLR0911 - one return per decision of section 6.2
    connection: Connection,
    envelope: ItemEnvelope,
    *,
    converter_version: str,
    new_id: Callable[[], UUID] = uuid.uuid4,
) -> Decision:
    """The whole Validate transaction: the fenced lease update, the reads and the decisions of
    section 6.2, the first that applies winning."""
    fence(connection, envelope, ItemStage.VALIDATE, DEADLINE_SECONDS)
    item = read_item(connection, envelope.item_id)
    version = read_version(connection, item.document_version_id)
    physical_id = item.physical_index_id
    target = read_target(connection, item.document_id, physical_id)

    if target.state in {"retiring", "deleted"}:
        active = target.active_physical_index_id
        if active is None:
            return Decision(skip="target_retired")
        if covered_elsewhere(connection, item.document_version_id, active, envelope.item_id):
            return Decision(skip="target_repointed")
        retarget(connection, envelope, active)
        physical_id = active
        target = read_target(connection, item.document_id, physical_id)
    if target.is_deleted:
        return Decision(skip="document_deleted")
    if version.state == "superseded":
        return Decision(skip="superseded_by_newer")
    if version.state in {"failed", "uploading"}:
        return Decision(skip="version_not_ready")
    if version.state == "pending" and newer_commit_exists(
        connection, item.document_id, item.document_version_id, version
    ):
        supersede_pending(connection, item.document_version_id)
        return Decision(skip="superseded_by_newer")
    if already_served(connection, item, physical_id, target):
        return Decision(skip="already_served")

    chunk_set_id = build_chunk_set(
        connection,
        envelope=envelope,
        item=item,
        physical_id=physical_id,
        target=target,
        new_id=new_id,
    )
    descriptor = descriptor_for(target.method_key, target.method_version)
    parameters = descriptor.validate_parameters(_parameters(target.method_parameters))
    method = descriptor.snapshot(parameters)
    if method.parameter_hash != target.method_parameter_hash.hex():
        # The stored hash names other parameters than the stored ones: a first-party invariant.
        raise ValueError("the index's parameter hash doesn't match its parameters")
    plan = BuildPlan(
        chunk_set_id=chunk_set_id,
        index_id=item.search_index_id,
        document_id=item.document_id,
        version_id=item.document_version_id,
        version_number=version.version_number,
        content_type=version.content_type,
        source_blob_path=version.blob_path,
        source_sha256=version.sha256.hex(),
        conversion=conversion_plan(item, version, target, physical_id, converter_version),
        title=document_title(version.source_name),
        work_prefix=work_prefix(item.run_id, item.document_id, version.version_number),
        physical_index=PhysicalIndexPlan(
            id=physical_id,
            name=target.name,
            is_active=target.active_physical_index_id == physical_id,
            schema_version=target.schema_version,
            dimensions=target.embedding_dimensions,
            encoding=TokenEncoding(target.embedding_encoding),
            max_input_tokens=target.embedding_max_input_tokens,
            ingestion_deployment=target.ingestion_embedding_deployment,
        ),
        method=method,
    )
    return Decision(plan=plan)


def _parameters(text: str) -> dict[str, Any]:
    # Decoded inside the unit of work, which runs in a SQL worker thread (section 4.4 rule 5).
    value = json.loads(text)
    if not isinstance(value, dict):
        raise TypeError("stored method parameters are a JSON object")
    return value


# --- The activity ---------------------------------------------------------------------------------


async def validate(
    services: DocumentServices, envelope: ItemEnvelope, span: Span
) -> ValidateResult:
    del span

    def work(connection: Connection) -> Decision:
        return decide(connection, envelope, converter_version=services.converter_version)

    try:
        decision = await services.sql.run(work, name="validate_item", retry="transient")
    except SqlUniqueViolation as violation:
        if violation.constraint not in {None, BUILD_KEY_INDEX}:
            raise
        # Two builds raced for the key; the second transaction finds the row.
        decision = await services.sql.run(work, name="validate_item", retry="transient")
    if decision.plan is not None:
        return ValidateResult.build(decision.plan)
    if decision.skip is None:
        raise ValueError("Validate decides a build or a skip")
    return ValidateResult.skip(decision.skip)


def _failure(code: str, retry_after: int | None) -> ValidateResult:
    return ValidateResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        ItemEnvelope,
        validate,
        stage=ItemStage.VALIDATE,
        provider=provider,
        failure=_failure,
    )


async def validate_item(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(validate_item)
