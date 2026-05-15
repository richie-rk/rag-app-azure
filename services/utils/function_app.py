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
    """Validate JWT from Authorization header. Returns claims or 401."""
    token = extract_bearer_token(req.headers.get("Authorization"))
    if not token:
        return _json_response({"error": "Missing authorization"}, 401)
    try:
        return validate_jwt(token)
    except Exception:
        return _json_response({"error": "Invalid token"}, 401)


def _get_body(req: func.HttpRequest) -> dict:
    return req.get_json()


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

    factory = get_session_factory()
    with factory() as session:
        result = provision_user(session, _get_body(req))
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
    claims = _require_auth(req)
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

    username = req.params.get("username", "")
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
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.feedback import save_feedback

    result = save_feedback(_get_body(req))
    return _json_response(result)


# Document Endpoint


@app.function_name("get_document")
@app.route(route="documents", methods=["GET"])
def get_document_fn(req: func.HttpRequest) -> func.HttpResponse:
    claims = _require_auth(req)
    if isinstance(claims, func.HttpResponse):
        return claims

    from .endpoints.documents import get_document

    file_name = req.params.get("file_name", "")
    container = req.params.get("container", "")

    if not file_name or not container:
        return _json_response({"error": "file_name and container required"}, 400)

    content, content_type, name = get_document(file_name, container)
    return func.HttpResponse(
        content,
        status_code=200,
        mimetype=content_type,
        headers={"Content-Disposition": f'inline; filename="{name}"'},
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
    username = body.get("username", "")
    project = body.get("project", "")

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
    from .endpoints.auth import create_magic_link

    body = _get_body(req)
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
