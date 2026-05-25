"""Azure Functions v2 entry point for utility endpoints.

Uses decorator-based model (no function.json files).
All endpoints validate the JWT themselves; auth_level is ANONYMOUS
so each function can read the Authorization header directly.
"""

import json
import logging

import azure.functions as func

from services.shared.auth import extract_bearer_token, validate_jwt
from services.shared.database import get_session_factory
from services.shared.models import IngestionAudit, User, UserProjectAccess

logger = logging.getLogger(__name__)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# Helpers


def _json_response(data, status_code=200):
    return func.HttpResponse(
        json.dumps(data, default=str),
        status_code=status_code,
        mimetype="application/json",
    )


def _require_auth(req: func.HttpRequest) -> dict | func.HttpResponse:
    """Validate JWT and require a role. Returns claims, 401, or 403."""
    token = extract_bearer_token(req.headers.get("Authorization"))
    if not token:
        return _json_response({"error": "Missing authorization"}, 401)
    try:
        claims = validate_jwt(token)
    except Exception:
        return _json_response({"error": "Invalid token"}, 401)
    if not claims.get("role"):
        # AAD-authenticated but in neither configured group. The frontend
        # shows a "contact your administrator" page on this code. See ADR-0003.
        return _json_response(
            {"error": "Not authorized", "code": "no_group"}, 403
        )
    return claims


def _require_non_guest(req: func.HttpRequest) -> dict | func.HttpResponse:
    """Like _require_auth, but also rejects guest (view-only) users."""
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims
    if claims.get("role") == "guest":
        return _json_response(
            {"error": "Read-only access for guest users"}, 403
        )
    return claims


def _require_admin(req: func.HttpRequest) -> dict | func.HttpResponse:
    """Like _require_auth, but also requires the 'admin' role."""
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims
    if claims.get("role") != "admin":
        return _json_response({"error": "Admin role required"}, 403)
    return claims


def _get_body(req: func.HttpRequest) -> dict:
    return req.get_json()


def _user_can_access_project(session, email: str, project_id: int) -> bool:
    """True if the user with this email has an access row for the project."""
    user = session.query(User).filter(User.email == email).first()
    if not user:
        return False
    access = (
        session.query(UserProjectAccess)
        .filter(
            UserProjectAccess.user_id == user.id,
            UserProjectAccess.project_id == project_id,
        )
        .first()
    )
    return access is not None


def _user_can_access_file(session, email: str, file_name: str) -> bool:
    """True if file_name was ingested into a project the user can access.

    With a single shared blob container, ingestion_audit is the only record
    of which project a file belongs to, so it is the authority for blob reads.
    """
    user = session.query(User).filter(User.email == email).first()
    if not user:
        return False
    accessible_ids = [
        row.project_id
        for row in session.query(UserProjectAccess.project_id)
        .filter(UserProjectAccess.user_id == user.id)
        .all()
    ]
    if not accessible_ids:
        return False
    match = (
        session.query(IngestionAudit)
        .filter(
            IngestionAudit.source_file == file_name,
            IngestionAudit.project_id.in_(accessible_ids),
        )
        .first()
    )
    return match is not None


# Project Endpoints


@app.function_name("get_projects")
@app.route(route="projects", methods=["GET"])
def get_projects_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.projects import get_projects

    user_id = req.params.get("user_id")
    factory = get_session_factory()
    with factory() as session:
        result = get_projects(session, int(user_id) if user_id else None)
    return _json_response(result)


@app.function_name("create_project")
@app.route(route="projects", methods=["POST"])
def create_project_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.projects import create_project

    factory = get_session_factory()
    with factory() as session:
        result = create_project(session, _get_body(req))
    status = result.pop("status", 201)
    return _json_response(result, status)


@app.function_name("update_project")
@app.route(route="projects/{project_id}", methods=["PUT"])
def update_project_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.projects import update_project

    project_id = int(req.route_params["project_id"])
    factory = get_session_factory()
    with factory() as session:
        result = update_project(session, project_id, _get_body(req))
    status = result.pop("status", 200)
    return _json_response(result, status)


# User Endpoints


@app.function_name("provision_user")
@app.route(route="users/provision", methods=["POST"])
def provision_user_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.users import provision_user

    # display_name is the only body field still consulted, and only as a
    # fallback when the token does not carry a `name` claim. Identity comes
    # from the validated token, never the body.
    try:
        body = _get_body(req) or {}
    except Exception:
        body = {}
    display_name = body.get("display_name") or claims.get("display_name", "")

    factory = get_session_factory()
    with factory() as session:
        result = provision_user(session, claims, display_name=display_name)
    return _json_response(result)


@app.function_name("list_users")
@app.route(route="users", methods=["GET"])
def list_users_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.users import list_users

    factory = get_session_factory()
    with factory() as session:
        result = list_users(session)
    return _json_response(result)


# Session Endpoints


@app.function_name("save_session")
@app.route(route="sessions", methods=["POST"])
def save_session_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_non_guest(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.sessions import save_session

    result = save_session(_get_body(req))
    return _json_response(result)


@app.function_name("get_sessions")
@app.route(route="sessions", methods=["GET"])
def get_sessions_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.sessions import get_sessions

    # Identity is the token subject, never a client-supplied query param.
    username = claims["sub"]
    session_id = req.params.get("session_id")
    result = get_sessions(username, session_id)
    return _json_response(result)


@app.function_name("delete_session")
@app.route(route="sessions/{session_id}", methods=["DELETE"])
def delete_session_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.sessions import delete_session

    result = delete_session(req.route_params["session_id"])
    return _json_response(result)


# Feedback Endpoint


@app.function_name("save_feedback")
@app.route(route="feedback", methods=["POST"])
def save_feedback_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_non_guest(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.feedback import save_feedback

    # Pass the token subject so feedback can only touch the caller's own turn.
    result = save_feedback(_get_body(req), claims["sub"])
    status_code = 403 if "error" in result else 200
    return _json_response(result, status_code)


# Document Endpoint


@app.function_name("get_document")
@app.route(route="documents", methods=["GET"])
def get_document_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.documents import get_document

    file_name = req.params.get("file_name", "")
    if not file_name:
        return _json_response({"error": "file_name required"}, 400)

    factory = get_session_factory()
    with factory() as session:
        if not _user_can_access_file(session, claims["sub"], file_name):
            # Uniform 404 whether the file is missing or simply off-limits, so
            # the endpoint never confirms which filenames exist.
            return _json_response({"error": "Document not found"}, 404)

    content, content_type, disposition = get_document(file_name)
    return func.HttpResponse(
        content,
        status_code=200,
        mimetype=content_type,
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
        },
    )


# Audit Endpoint


@app.function_name("get_audit_info")
@app.route(route="audit/{project_id}", methods=["GET"])
def get_audit_info_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.audit import get_audit_info

    project_id = int(req.route_params["project_id"])
    factory = get_session_factory()
    with factory() as session:
        if not _user_can_access_project(session, claims["sub"], project_id):
            return _json_response({"error": "No access to this project"}, 403)
        result = get_audit_info(session, project_id)
    return _json_response(result)


# Prompt Library Endpoints


@app.function_name("prompt_library")
@app.route(route="prompts", methods=["POST"])
def prompt_library_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.prompts import create_prompt, get_all_prompts, get_prompt, update_prompt

    body = _get_body(req)
    request_type = body.get("request_type", "")
    # The prompt library is per-user; its owner is the token subject, never a
    # body field, so one user cannot reach another user's library.
    username = claims["sub"]
    project = body.get("project", "")

    # Guests are view-only: they may read the library but not write to it.
    if request_type in ("create", "update") and claims.get("role") == "guest":
        return _json_response({"error": "Read-only access for guest users"}, 403)

    if request_type == "create":
        result = create_prompt(username, project, body["prompt_name"], body["prompt_content"])
    elif request_type == "update":
        result = update_prompt(username, project, body["prompt_name"], body["prompt_content"])
    elif request_type == "get":
        result = get_prompt(username, project, body["prompt_name"])
    elif request_type == "get_all":
        result = get_all_prompts(username)
    else:
        return _json_response({"error": f"Unknown request_type: {request_type}"}, 400)

    status = result.pop("status", 200) if isinstance(result, dict) else 200
    return _json_response(result, status)


# Magic Link Auth Endpoints


@app.function_name("create_magic_link")
@app.route(route="auth/magic-link", methods=["POST"])
def create_magic_link_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_admin(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.auth import create_magic_link

    try:
        body = _get_body(req) or {}
    except Exception:
        body = {}
    email = body.get("email", "")
    if not email:
        return _json_response({"error": "email required"}, 400)

    factory = get_session_factory()
    with factory() as session:
        result = create_magic_link(session, email)
    return _json_response(result)


@app.function_name("verify_magic_link")
@app.route(route="auth/verify", methods=["GET"])
def verify_magic_link_fn(req: func.HttpRequest) -> func.HttpResponse:
    from .endpoints.auth import verify_magic_link

    token = req.params.get("token", "")
    if not token:
        return _json_response({"error": "token required"}, 400)

    factory = get_session_factory()
    with factory() as session:
        result = verify_magic_link(session, token)
    status = result.pop("status", 200) if "status" in result else 200
    return _json_response(result, status)
