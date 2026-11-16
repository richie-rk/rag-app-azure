"""The administrator operations on projects, members and project-index mappings (`11-api.md`
sections 3.7 and 3.9, models in section 4.4; ADR 0022).

`adminListProjects`, `adminCreateProject`, `adminGetProject`, `adminUpdateProject`,
`adminListProjectMembers`, `adminAddProjectMember`, `adminRemoveProjectMember`,
`adminListProjectIndexes`, `adminMapProjectIndex` and `adminUnmapProjectIndex`. The mapping
operations live here rather than with the index operations because they are parts of a project:
their paths sit under `/v1/admin/projects/{projectId}`, their tag is `admin-projects`, and the
project's `rowVersion` guards them.

The route class has already refused a caller without `Admin` (`403 admin_required`) before any
body or identifier is read. Each handler validates its input, then runs one unit of work of
`docchat_core.sql.repositories.projects`, which answers in the order of section 1.3: the lookups
(`404`), the caller's `If-Match` (`428`, `412`), then the state rules (`409`). Archived projects
are listed, read and restored here; on an archived project only the restore body is accepted. A
write's audit rows are committed with the change and reported on the server span afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi import Path as PathParam
from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    WrapValidator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from docchat_api.http.access import access
from docchat_api.http.audit import report_audit
from docchat_api.http.models import QueryModel, RequestModel, ResponseModel
from docchat_api.http.paging import CursorModel, CursorText, decode_cursor
from docchat_api.http.preconditions import (
    IfMatchHeader,
    PreconditionFailed,
    PreconditionRequired,
    if_match_header,
)
from docchat_api.http.problems import ValidationFailed
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process
from docchat_api.http.text import MultiLineText, SingleLineText
from docchat_api.routes.admin_common import (
    DEFAULT_LIMIT,
    INDEX_DISPLAY_NAME_MAX_LENGTH,
    IndexRef,
    PageQuery,
    UserRef,
    index_ref,
    keyset_page,
    user_ref,
)
from docchat_core.access import MAX_PAGE_LIMIT, ProjectNotFound
from docchat_core.auth.principal import DISPLAY_NAME_MAX_LENGTH, EMAIL_MAX_LENGTH
from docchat_core.errors import DocChatProblem, FieldIssue
from docchat_core.models import CanonicalUuid, Count, RowVersion, UtcDateTime, encode_row_version
from docchat_core.sql.repositories import projects as repo
from docchat_core.sql.repositories.projects import (
    AdminProjectRow,
    AdminProjectSummaryRow,
    MappingPosition,
    MemberPosition,
    NewProject,
    ProjectChanges,
    ProjectIndexMappingRow,
    ProjectMemberRow,
    Refused,
    UnknownMembers,
)

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "admin-projects"
PROJECT_NAME_MAX_LENGTH: Final = 80
DESCRIPTION_MAX_LENGTH: Final = 400
INSTRUCTIONS_MAX_LENGTH: Final = 2000
EXAMPLE_QUESTIONS_MAX: Final = 4
EXAMPLE_QUESTION_MAX_LENGTH: Final = 160
INITIAL_MEMBERS_MAX: Final = 50
SEARCH_TEXT_MAX_LENGTH: Final = 80

# Plain aliases, so each enumeration is inlined in the schemas that use it.
ProjectStatus = Literal["active", "archived"]
StatusFilter = Literal["active", "archived", "all"]
GrantSource = Literal["default", "admin"]
IndexStatus = Literal[
    "provisioning",
    "ready",
    "reingest_required",
    "reingesting",
    "rebuilding",
    "failed",
    "deleting",
    "deleted",
]


# --- Problems ------------------------------------------------------------------------------


class UserNotFound(DocChatProblem):
    """`404 user_not_found`: no `app.users` row; people appear after they first sign in."""

    code = "user_not_found"


class IndexNotFound(DocChatProblem):
    """`404 index_not_found`: no logical index, or a `deleted` one."""

    code = "index_not_found"


class ProjectNameTaken(DocChatProblem):
    """`409 project_name_taken`: a project, archived ones included, has the name, compared
    case-insensitively."""

    code = "project_name_taken"


class ProjectArchived(DocChatProblem):
    """`409 project_archived`: an archived project is read-only until restored."""

    code = "project_archived"


class IndexNotMappable(DocChatProblem):
    """`409 index_not_mappable`: a `failed` or `deleting` logical index."""

    code = "index_not_mappable"


class DefaultIndexRequired(DocChatProblem):
    """`409 default_index_required`: a project's first mapping, or its default, can't be
    non-default."""

    code = "default_index_required"


class DefaultIndexInUse(DocChatProblem):
    """`409 default_index_in_use`: the default mapping while other mappings exist."""

    code = "default_index_in_use"


_REFUSALS: Final[Mapping[str, type[DocChatProblem]]] = {
    "project_not_found": ProjectNotFound,
    "user_not_found": UserNotFound,
    "index_not_found": IndexNotFound,
    "precondition_required": PreconditionRequired,
    "precondition_failed": PreconditionFailed,
    "project_archived": ProjectArchived,
    "project_name_taken": ProjectNameTaken,
    "index_not_mappable": IndexNotMappable,
    "default_index_required": DefaultIndexRequired,
    "default_index_in_use": DefaultIndexInUse,
}


def refusal_problem(refused: Refused) -> DocChatProblem:
    """The problem a refused write answers."""
    if refused.refusal == "status_change_with_fields":
        return ValidationFailed(errors=[FieldIssue("body.status", "not_allowed")])
    return _REFUSALS[refused.refusal]()


# --- Request models ------------------------------------------------------------------------


def unique_entries(fold: Callable[[Any], Hashable]) -> WrapValidator:
    """A list whose entries, after their own validation, are unique under `fold`; a repeat is
    `duplicate` at the later entry's index."""

    def validate(value: object, handler: Callable[[object], Any]) -> object:
        items = handler(value)
        seen: set[Hashable] = set()
        errors: list[InitErrorDetails] = []
        for index, item in enumerate(items):
            key = fold(item)
            if key in seen:
                errors.append(
                    InitErrorDetails(
                        type=PydanticCustomError("duplicate", "The list holds this entry twice"),
                        loc=(index,),
                        input=item,
                    )
                )
            seen.add(key)
        if errors:
            raise ValidationError.from_exception_data("duplicate", errors)
        return items

    return WrapValidator(validate)


def _present(value: object) -> object:
    # In a PATCH an absent field is unchanged; an explicit null is no value of the field's type.
    if value is None:
        raise PydanticCustomError("invalid_type", "The field can't be null")
    return value


def _not_nullable(schema: dict[str, Any]) -> None:
    # The field is optional only by absence, which means "unchanged": its schema states neither
    # a default nor `null`, which validation refuses.
    schema.pop("default", None)
    members = [member for member in schema.pop("anyOf", ()) if member.get("type") != "null"]
    if len(members) == 1:
        schema.update(members[0])


PRESENT: Final = BeforeValidator(_present)

ProjectName = Annotated[str, SingleLineText(1, PROJECT_NAME_MAX_LENGTH)]
ProjectDescription = Annotated[str, SingleLineText(0, DESCRIPTION_MAX_LENGTH)]
ProjectInstructions = Annotated[str, MultiLineText(0, INSTRUCTIONS_MAX_LENGTH)]
ExampleQuestions = Annotated[
    tuple[Annotated[str, SingleLineText(1, EXAMPLE_QUESTION_MAX_LENGTH)], ...],
    Field(max_length=EXAMPLE_QUESTIONS_MAX),
    unique_entries(str.casefold),
]
InitialMemberIds = Annotated[
    tuple[CanonicalUuid, ...], Field(max_length=INITIAL_MEMBERS_MAX), unique_entries(lambda x: x)
]


class ProjectCreate(RequestModel):
    """`adminCreateProject`'s body."""

    name: ProjectName
    description: ProjectDescription = ""
    instructions: ProjectInstructions = ""
    example_questions: ExampleQuestions = ()
    is_default: StrictBool = False
    initial_member_ids: InitialMemberIds = ()


class ProjectUpdate(RequestModel):
    """`adminUpdateProject`'s body: at least one field; an absent field stays unchanged, and no
    field may be null."""

    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    name: Annotated[ProjectName | None, PRESENT] = Field(
        default=None, json_schema_extra=_not_nullable
    )
    description: Annotated[ProjectDescription | None, PRESENT] = Field(
        default=None, json_schema_extra=_not_nullable
    )
    instructions: Annotated[ProjectInstructions | None, PRESENT] = Field(
        default=None, json_schema_extra=_not_nullable
    )
    example_questions: Annotated[ExampleQuestions | None, PRESENT] = Field(
        default=None, json_schema_extra=_not_nullable
    )
    is_default: Annotated[StrictBool | None, PRESENT] = Field(
        default=None, json_schema_extra=_not_nullable
    )
    status: Annotated[ProjectStatus | None, PRESENT] = Field(
        default=None, json_schema_extra=_not_nullable
    )

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_fields_set:
            raise PydanticCustomError("required", "Send at least one field to change")
        return self

    def changes(self) -> ProjectChanges:
        return ProjectChanges(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            example_questions=self.example_questions,
            is_default=self.is_default,
            status=self.status,
        )


class ProjectIndexMappingUpdate(RequestModel):
    """`adminMapProjectIndex`'s body."""

    is_default: StrictBool


CREATE_EXAMPLE: Final[Mapping[str, object]] = {
    "name": "Field notes",
    "description": "Shared notes for the field team",
    "instructions": "",
    "exampleQuestions": [],
    "isDefault": False,
    "initialMemberIds": [],
}
UPDATE_EXAMPLE: Final[Mapping[str, object]] = {
    "description": "Shared notes for the field team, updated"
}
MAPPING_EXAMPLE: Final[Mapping[str, object]] = {"isDefault": True}


def _example(body: Mapping[str, object]) -> dict[str, Any]:
    """The `openapi_extra` of an operation whose request example the matrix test sends."""
    return {"requestBody": {"content": {"application/json": {"example": dict(body)}}}}


class AdminListProjectsQuery(QueryModel):
    status: StatusFilter = "all"
    q: Annotated[str, SingleLineText(1, SEARCH_TEXT_MAX_LENGTH)] | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_LIMIT
    cursor: CursorText | None = None


ProjectIdPath = Annotated[CanonicalUuid, PathParam(alias="projectId")]
UserIdPath = Annotated[CanonicalUuid, PathParam(alias="userId")]
IndexIdPath = Annotated[CanonicalUuid, PathParam(alias="indexId")]


def no_body(request: Request) -> None:
    """`adminAddProjectMember` takes no body: one that isn't empty is `unknown_field` at
    `body`, decided from the headers, so the body is never read."""
    length = request.headers.get("content-length")
    sent = request.headers.get("transfer-encoding") is not None or (
        length is not None and length.strip() not in {"", "0"}
    )
    if sent:
        raise ValidationFailed(errors=[FieldIssue("body", "unknown_field")])


# --- Cursors -------------------------------------------------------------------------------


class AdminProjectsCursor(CursorModel):
    """`{"v":1,"n":"<name>","s":"<status filter>","q":"<q>" or null}`."""

    n: Annotated[str, StringConstraints(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)]
    s: StatusFilter
    q: Annotated[str, StringConstraints(min_length=1, max_length=SEARCH_TEXT_MAX_LENGTH)] | None


class MembersCursor(CursorModel):
    """`{"v":1,"d":"<displayName>","u":"<userId>"}`."""

    d: Annotated[str, StringConstraints(max_length=DISPLAY_NAME_MAX_LENGTH)]
    u: UUID


class MappingsCursor(CursorModel):
    """`{"v":1,"f":0 or 1,"d":"<displayName>","i":"<indexId>"}`; `f` is 1 for the default."""

    f: Literal[0, 1]
    d: Annotated[str, StringConstraints(min_length=1, max_length=INDEX_DISPLAY_NAME_MAX_LENGTH)]
    i: UUID


# --- Response models -----------------------------------------------------------------------

ExampleQuestionList = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=EXAMPLE_QUESTION_MAX_LENGTH)]],
    Field(max_length=EXAMPLE_QUESTIONS_MAX),
]


class AdminProjectSummary(ResponseModel):
    project_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)]
    description: Annotated[str, Field(max_length=DESCRIPTION_MAX_LENGTH)]
    is_default: bool
    status: ProjectStatus
    member_count: Count
    index_count: Count
    default_index: IndexRef | None
    updated_at: UtcDateTime
    row_version: RowVersion


class AdminProjectSummaryPage(ResponseModel):
    items: Annotated[list[AdminProjectSummary], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


class AdminProject(ResponseModel):
    project_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)]
    description: Annotated[str, Field(max_length=DESCRIPTION_MAX_LENGTH)]
    instructions: Annotated[str, Field(max_length=INSTRUCTIONS_MAX_LENGTH)]
    example_questions: ExampleQuestionList
    is_default: bool
    status: ProjectStatus
    member_count: Count
    index_count: Count
    default_index: IndexRef | None
    created_at: UtcDateTime
    created_by: UserRef
    updated_at: UtcDateTime
    updated_by: UserRef
    archived_at: UtcDateTime | None
    archived_by: UserRef | None
    row_version: RowVersion


class ProjectMember(ResponseModel):
    user_id: UUID
    display_name: Annotated[str, Field(max_length=DISPLAY_NAME_MAX_LENGTH)]
    email: Annotated[str, Field(max_length=EMAIL_MAX_LENGTH)]
    grant_source: GrantSource
    granted_at: UtcDateTime
    granted_by: UserRef | None
    last_seen_at: UtcDateTime


class ProjectMemberPage(ResponseModel):
    items: Annotated[list[ProjectMember], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


class ProjectMemberChange(ResponseModel):
    member: ProjectMember
    project_row_version: RowVersion


class ProjectIndexMapping(ResponseModel):
    index_id: UUID
    display_name: Annotated[str, Field(min_length=1, max_length=INDEX_DISPLAY_NAME_MAX_LENGTH)]
    index_status: IndexStatus
    is_default: bool
    mapped_at: UtcDateTime
    mapped_by: UserRef
    updated_at: UtcDateTime


class ProjectIndexMappingPage(ResponseModel):
    items: Annotated[list[ProjectIndexMapping], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


class ProjectIndexMappingChange(ResponseModel):
    mapping: ProjectIndexMapping
    project_row_version: RowVersion


class ProjectPartRemoval(ResponseModel):
    removed: bool
    project_row_version: RowVersion


def admin_project_summary(row: AdminProjectSummaryRow) -> AdminProjectSummary:
    return AdminProjectSummary(
        project_id=row.id,
        name=row.name,
        description=row.description,
        is_default=row.is_default,
        status=row.status,
        member_count=row.member_count,
        index_count=row.index_count,
        default_index=None if row.default_index is None else index_ref(row.default_index),
        updated_at=row.updated_at,
        row_version=encode_row_version(row.row_version),
    )


def admin_project(row: AdminProjectRow) -> AdminProject:
    return AdminProject(
        project_id=row.id,
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        example_questions=list(row.example_questions),
        is_default=row.is_default,
        status=row.status,
        member_count=row.member_count,
        index_count=row.index_count,
        default_index=None if row.default_index is None else index_ref(row.default_index),
        created_at=row.created_at,
        created_by=user_ref(row.created_by),
        updated_at=row.updated_at,
        updated_by=user_ref(row.updated_by),
        archived_at=row.archived_at,
        archived_by=None if row.archived_by is None else user_ref(row.archived_by),
        row_version=encode_row_version(row.row_version),
    )


def project_member(row: ProjectMemberRow) -> ProjectMember:
    return ProjectMember(
        user_id=row.user_id,
        display_name=row.display_name,
        email=row.email,
        grant_source=row.grant_source,
        granted_at=row.granted_at,
        granted_by=None if row.granted_by is None else user_ref(row.granted_by),
        last_seen_at=row.last_seen_at,
    )


def project_index_mapping(row: ProjectIndexMappingRow) -> ProjectIndexMapping:
    return ProjectIndexMapping(
        index_id=row.index_id,
        display_name=row.display_name,
        index_status=row.index_status,
        is_default=row.is_default,
        mapped_at=row.mapped_at,
        mapped_by=user_ref(row.mapped_by),
        updated_at=row.updated_at,
    )


# --- Projects --------------------------------------------------------------------------------


@router.get(
    "/admin/projects",
    operation_id="adminListProjects",
    tags=[TAG],
    response_model=AdminProjectSummaryPage,
)
@access("admin")
async def admin_list_projects(
    query: Annotated[AdminListProjectsQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> AdminProjectSummaryPage:
    after = None
    if query.cursor is not None:
        filters = {"s": query.status, "q": query.q}
        after = decode_cursor(query.cursor, AdminProjectsCursor, filters=filters).n
    rows = await repo.read_admin_projects(
        process.sql,
        status=None if query.status == "all" else query.status,
        q=query.q,
        after_name=after,
        limit_plus_one=query.limit + 1,
    )
    items, next_cursor = keyset_page(
        rows, query.limit, lambda row: AdminProjectsCursor(n=row.name, s=query.status, q=query.q)
    )
    return AdminProjectSummaryPage(
        items=[admin_project_summary(row) for row in items], next_cursor=next_cursor
    )


@router.post(
    "/admin/projects",
    operation_id="adminCreateProject",
    tags=[TAG],
    response_model=AdminProject,
    status_code=201,
    openapi_extra=_example(CREATE_EXAMPLE),
)
@access("admin", scoped_params=["body:/initialMemberIds"], error_codes=["project_name_taken"])
async def admin_create_project(
    body: ProjectCreate,
    request: Request,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> AdminProject:
    project = NewProject(
        id=uuid4(),
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        example_questions=body.example_questions,
        is_default=body.is_default,
        initial_member_ids=body.initial_member_ids,
    )
    result = await repo.create_project(process.sql, project=project, caller=principal.oid)
    if isinstance(result, UnknownMembers):
        raise ValidationFailed(
            errors=[
                FieldIssue(f"body.initialMemberIds.{position}", "not_found")
                for position in result.positions
            ]
        )
    if isinstance(result, Refused):
        raise refusal_problem(result)
    report_audit(request, result.events, admin=True)
    return admin_project(result.project)


@router.get(
    "/admin/projects/{projectId}",
    operation_id="adminGetProject",
    tags=[TAG],
    response_model=AdminProject,
)
@access("admin", scoped_params=["projectId"], error_codes=["project_not_found"])
async def admin_get_project(
    project_id: ProjectIdPath, process: Annotated[ApiProcess, Depends(current_process)]
) -> AdminProject:
    row = await repo.read_admin_project(process.sql, project_id=project_id)
    if row is None:
        raise ProjectNotFound()
    return admin_project(row)


@router.patch(
    "/admin/projects/{projectId}",
    operation_id="adminUpdateProject",
    tags=[TAG],
    response_model=AdminProject,
    openapi_extra=_example(UPDATE_EXAMPLE),
)
@access(
    "admin",
    scoped_params=["projectId"],
    error_codes=["project_not_found", "project_name_taken", "project_archived"],
    if_match=True,
)
async def admin_update_project(
    *,
    project_id: ProjectIdPath,
    body: ProjectUpdate,
    if_match: Annotated[IfMatchHeader, Depends(if_match_header)],
    request: Request,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> AdminProject:
    result = await repo.update_project(
        process.sql,
        project_id=project_id,
        changes=body.changes(),
        version=if_match,
        caller=principal.oid,
    )
    if isinstance(result, Refused):
        raise refusal_problem(result)
    report_audit(request, result.events, admin=True)
    return admin_project(result.project)


# --- Members ---------------------------------------------------------------------------------


@router.get(
    "/admin/projects/{projectId}/members",
    operation_id="adminListProjectMembers",
    tags=[TAG],
    response_model=ProjectMemberPage,
)
@access("admin", scoped_params=["projectId"], error_codes=["project_not_found"])
async def admin_list_project_members(
    project_id: ProjectIdPath,
    query: Annotated[PageQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> ProjectMemberPage:
    after = None
    if query.cursor is not None:
        cursor = decode_cursor(query.cursor, MembersCursor)
        after = MemberPosition(display_name=cursor.d, user_id=cursor.u)
    rows = await repo.read_project_members(
        process.sql, project_id=project_id, after=after, limit_plus_one=query.limit + 1
    )
    if rows is None:
        raise ProjectNotFound()
    items, next_cursor = keyset_page(
        rows, query.limit, lambda row: MembersCursor(d=row.display_name, u=row.user_id)
    )
    return ProjectMemberPage(items=[project_member(row) for row in items], next_cursor=next_cursor)


@router.put(
    "/admin/projects/{projectId}/members/{userId}",
    operation_id="adminAddProjectMember",
    tags=[TAG],
    response_model=ProjectMemberChange,
    status_code=201,
    responses={200: {"model": ProjectMemberChange, "description": "Already a member"}},
)
@access(
    "admin",
    scoped_params=["projectId", "userId"],
    error_codes=["project_not_found", "user_not_found", "project_archived"],
    if_match=True,
)
async def admin_add_project_member(
    *,
    project_id: ProjectIdPath,
    user_id: UserIdPath,
    if_match: Annotated[IfMatchHeader, Depends(if_match_header)],
    _body: Annotated[None, Depends(no_body)],
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> ProjectMemberChange:
    result = await repo.add_member(
        process.sql,
        project_id=project_id,
        user_id=user_id,
        version=if_match,
        caller=principal.oid,
    )
    if isinstance(result, Refused):
        raise refusal_problem(result)
    report_audit(request, result.events, admin=True)
    if not result.created:
        response.status_code = 200
    return ProjectMemberChange(
        member=project_member(result.member),
        project_row_version=encode_row_version(result.project_row_version),
    )


@router.delete(
    "/admin/projects/{projectId}/members/{userId}",
    operation_id="adminRemoveProjectMember",
    tags=[TAG],
    response_model=ProjectPartRemoval,
)
@access(
    "admin",
    scoped_params=["projectId"],
    error_codes=["project_not_found", "project_archived"],
    if_match=True,
)
async def admin_remove_project_member(
    *,
    project_id: ProjectIdPath,
    user_id: UserIdPath,
    if_match: Annotated[IfMatchHeader, Depends(if_match_header)],
    request: Request,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> ProjectPartRemoval:
    result = await repo.remove_member(
        process.sql,
        project_id=project_id,
        user_id=user_id,
        version=if_match,
        caller=principal.oid,
    )
    if isinstance(result, Refused):
        raise refusal_problem(result)
    report_audit(request, result.events, admin=True)
    return ProjectPartRemoval(
        removed=result.removed, project_row_version=encode_row_version(result.project_row_version)
    )


# --- Mappings --------------------------------------------------------------------------------


@router.get(
    "/admin/projects/{projectId}/indexes",
    operation_id="adminListProjectIndexes",
    tags=[TAG],
    response_model=ProjectIndexMappingPage,
)
@access("admin", scoped_params=["projectId"], error_codes=["project_not_found"])
async def admin_list_project_indexes(
    project_id: ProjectIdPath,
    query: Annotated[PageQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> ProjectIndexMappingPage:
    after = None
    if query.cursor is not None:
        cursor = decode_cursor(query.cursor, MappingsCursor)
        after = MappingPosition(is_default=cursor.f == 1, display_name=cursor.d, index_id=cursor.i)
    rows = await repo.read_project_mappings(
        process.sql, project_id=project_id, after=after, limit_plus_one=query.limit + 1
    )
    if rows is None:
        raise ProjectNotFound()
    items, next_cursor = keyset_page(
        rows,
        query.limit,
        lambda row: MappingsCursor(
            f=1 if row.is_default else 0, d=row.display_name, i=row.index_id
        ),
    )
    return ProjectIndexMappingPage(
        items=[project_index_mapping(row) for row in items], next_cursor=next_cursor
    )


@router.put(
    "/admin/projects/{projectId}/indexes/{indexId}",
    operation_id="adminMapProjectIndex",
    tags=[TAG],
    response_model=ProjectIndexMappingChange,
    status_code=201,
    responses={200: {"model": ProjectIndexMappingChange, "description": "Updated or unchanged"}},
    openapi_extra=_example(MAPPING_EXAMPLE),
)
@access(
    "admin",
    scoped_params=["projectId", "indexId"],
    error_codes=[
        "project_not_found",
        "index_not_found",
        "project_archived",
        "index_not_mappable",
        "default_index_required",
    ],
    if_match=True,
)
async def admin_map_project_index(
    *,
    project_id: ProjectIdPath,
    index_id: IndexIdPath,
    body: ProjectIndexMappingUpdate,
    if_match: Annotated[IfMatchHeader, Depends(if_match_header)],
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> ProjectIndexMappingChange:
    result = await repo.map_index(
        process.sql,
        project_id=project_id,
        index_id=index_id,
        is_default=body.is_default,
        version=if_match,
        caller=principal.oid,
    )
    if isinstance(result, Refused):
        raise refusal_problem(result)
    report_audit(request, result.events, admin=True)
    if not result.created:
        response.status_code = 200
    return ProjectIndexMappingChange(
        mapping=project_index_mapping(result.mapping),
        project_row_version=encode_row_version(result.project_row_version),
    )


@router.delete(
    "/admin/projects/{projectId}/indexes/{indexId}",
    operation_id="adminUnmapProjectIndex",
    tags=[TAG],
    response_model=ProjectPartRemoval,
)
@access(
    "admin",
    scoped_params=["projectId"],
    error_codes=["project_not_found", "project_archived", "default_index_in_use"],
    if_match=True,
)
async def admin_unmap_project_index(
    *,
    project_id: ProjectIdPath,
    index_id: IndexIdPath,
    if_match: Annotated[IfMatchHeader, Depends(if_match_header)],
    request: Request,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> ProjectPartRemoval:
    result = await repo.unmap_index(
        process.sql,
        project_id=project_id,
        index_id=index_id,
        version=if_match,
        caller=principal.oid,
    )
    if isinstance(result, Refused):
        raise refusal_problem(result)
    report_audit(request, result.events, admin=True)
    return ProjectPartRemoval(
        removed=result.removed, project_row_version=encode_row_version(result.project_row_version)
    )
