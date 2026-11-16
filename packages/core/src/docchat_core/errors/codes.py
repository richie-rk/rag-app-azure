"""`CODES`: the error-code catalogue (`11-api.md` section 2; ADR 0012).

One entry per code of `11-api.md` sections 2.2 (problem codes), 2.3 (stream-only codes) and 2.4
(the stored-only turn code). Each code has one title and one safe `detail`, the same on every
occurrence, so a client branches on the code and never on text, and no text can carry input
values, names, paths or upstream messages. The catalogue is open: an area that adds a code adds its
entry here in the same change.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

PROBLEM_TYPE_PREFIX: Final = "urn:docchat:problem:"
CODE_PATTERN: Final = r"^[a-z][a-z0-9_]*$"
CODE_MAX_LENGTH: Final = 64
TITLE_MAX_LENGTH: Final = 40
DETAIL_MAX_LENGTH: Final = 300
RETRY_AFTER_MAX: Final = 2_678_400
"""The longest wait a problem states: a 31-day month, until the monthly token quota resets."""

ProblemMember = Literal["retryAfterSeconds", "errors", "sessionId", "turnId"]
"""The extension members a problem body may carry beyond the standard ones (`11-api.md` 1.5)."""

_CODE_RE: Final = re.compile(CODE_PATTERN)
_STATUS_MIN: Final = 400
_STATUS_MAX: Final = 599


class CodeArea(StrEnum):
    """The area whose code it is; that area writes the code's title and text."""

    IDENTITY_ACCESS = "identity_access"
    INGESTION = "ingestion"
    CHAT = "chat"
    CONVERSATIONS = "conversations"


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeEntry:
    """One code of the catalogue.

    `status` is `None` exactly for a code that exists only as a stream `error` event or as a stored
    turn code; the problem handler never builds a response from such an entry. `extensions` are
    the members a problem with this code always carries. `retry_after` is the inclusive range of
    `retryAfterSeconds`, with equal ends where the catalogue fixes the wait.
    """

    code: str
    status: int | None
    title: str
    detail: str
    retryable: bool
    area: CodeArea
    extensions: frozenset[ProblemMember] = frozenset()
    retry_after: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not _CODE_RE.fullmatch(self.code) or len(self.code) > CODE_MAX_LENGTH:
            raise ValueError(f"invalid code {self.code!r}")
        if self.status is not None and not _STATUS_MIN <= self.status <= _STATUS_MAX:
            raise ValueError(f"{self.code}: a problem status is 400 to 599")
        if not 1 <= len(self.title) <= TITLE_MAX_LENGTH:
            raise ValueError(f"{self.code}: a title has 1 to {TITLE_MAX_LENGTH} characters")
        if not 1 <= len(self.detail) <= DETAIL_MAX_LENGTH:
            raise ValueError(f"{self.code}: a detail has 1 to {DETAIL_MAX_LENGTH} characters")
        if self.status is None and self.extensions:
            raise ValueError(f"{self.code}: a code without a status is never a problem")
        if self.retry_after is not None:
            low, high = self.retry_after
            if not 1 <= low <= high <= RETRY_AFTER_MAX:
                raise ValueError(f"{self.code}: retry_after must lie within 1 to {RETRY_AFTER_MAX}")
        if self.status is not None and (
            ("retryAfterSeconds" in self.extensions) != (self.retry_after is not None)
        ):
            raise ValueError(f"{self.code}: retryAfterSeconds and retry_after go together")

    @property
    def problem_type(self) -> str:
        """The problem's `type` member."""
        return f"{PROBLEM_TYPE_PREFIX}{self.code}"

    @property
    def fixed_retry_after(self) -> int | None:
        """The wait the catalogue fixes for this code, if it fixes one."""
        if self.retry_after is None or self.retry_after[0] != self.retry_after[1]:
            return None
        return self.retry_after[0]

    @property
    def detail_fields(self) -> frozenset[str]:
        """The placeholders of `detail`; only `batch_too_large` has one."""
        return frozenset(
            name for _, name, _, _ in string.Formatter().parse(self.detail) if name is not None
        )

    def render_detail(self, values: Mapping[str, int] | None = None) -> str:
        """The `detail` with its placeholders filled; the values must name exactly them."""
        given = dict(values or {})
        if set(given) != self.detail_fields:
            raise ValueError(f"{self.code}: detail values must name exactly its placeholders")
        return self.detail.format_map(given) if given else self.detail


_IA: Final = CodeArea.IDENTITY_ACCESS
_ING: Final = CodeArea.INGESTION
_CHAT: Final = CodeArea.CHAT
_CONV: Final = CodeArea.CONVERSATIONS

_ERRORS: Final[frozenset[ProblemMember]] = frozenset({"errors"})
_RETRY: Final[frozenset[ProblemMember]] = frozenset({"retryAfterSeconds"})
_SESSION_TURN: Final[frozenset[ProblemMember]] = frozenset({"sessionId", "turnId"})

# Section 2.2: the problem codes, in the catalogue's order.
_PROBLEM_CODES: Final = (
    CodeEntry(
        code="authentication_required",
        status=401,
        title="Sign-in required",
        detail="Sign in to continue.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="invalid_token",
        status=401,
        title="Sign-in not accepted",
        detail="Your sign-in isn't valid any more. Sign in to continue.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="role_not_assigned",
        status=403,
        title="No access",
        detail=(
            "Your account doesn't have access to DocChat. "
            "Ask your administrator to give you access."
        ),
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="admin_required",
        status=403,
        title="Administrator access needed",
        detail="This needs administrator access.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="route_not_found",
        status=404,
        title="Not found",
        detail="There's nothing at this address.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="project_not_found",
        status=404,
        title="Project not found",
        detail="This project doesn't exist, or you don't have access to it.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="user_not_found",
        status=404,
        title="Unknown user",
        detail="There's no user with this ID. People appear here after they first sign in.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="index_not_found",
        status=404,
        title="Index not found",
        detail="The index wasn't found.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="document_not_found",
        status=404,
        title="Document not found",
        detail="The document wasn't found in this index.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="version_not_found",
        status=404,
        title="Upload not found",
        detail="The upload wasn't found. Restart the upload.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="run_not_found",
        status=404,
        title="Ingestion run not found",
        detail="The ingestion run wasn't found.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="session_not_found",
        status=404,
        title="Conversation not found",
        detail="This conversation isn't available. Start a new chat.",
        retryable=False,
        area=_CONV,
    ),
    CodeEntry(
        code="turn_not_found",
        status=404,
        title="Answer not found",
        detail="We couldn't find that answer in your conversations.",
        retryable=False,
        area=_CONV,
    ),
    CodeEntry(
        code="memory_fact_not_found",
        status=404,
        title="Memory item not found",
        detail="That memory item doesn't exist. Refresh to see your current memory.",
        retryable=False,
        area=_CONV,
    ),
    CodeEntry(
        code="citation_not_found",
        status=404,
        title="Source not found",
        detail="This source isn't available.",
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="method_not_allowed",
        status=405,
        title="Method not allowed",
        detail="This address doesn't support that action.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="project_name_taken",
        status=409,
        title="Name in use",
        detail="Another project already uses this name.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="project_archived",
        status=409,
        title="Project archived",
        detail="This project is archived. Restore it before changing it.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="default_index_required",
        status=409,
        title="Default index needed",
        detail=(
            "A project with indexes needs a default index. Make another index the default instead."
        ),
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="default_index_in_use",
        status=409,
        title="Default index in use",
        detail=(
            "This is the project's default index. "
            "Make another index the default before removing it."
        ),
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="index_not_mappable",
        status=409,
        title="Index unavailable",
        detail="This index can't be connected to a project in its current state.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="project_index_unavailable",
        status=409,
        title="Project not ready",
        detail="This project has nothing to search yet. Ask an administrator to connect an index.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="display_name_taken",
        status=409,
        title="Index name in use",
        detail="Another index already uses this name. Choose a different name.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="index_busy",
        status=409,
        title="Index busy",
        detail="This index is busy with another operation. Retry when it finishes.",
        retryable=True,
        area=_ING,
    ),
    CodeEntry(
        code="index_mapped",
        status=409,
        title="Index used by projects",
        detail="Remove this index from every project before deleting it.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="index_not_accepting_uploads",
        status=409,
        title="Uploads not accepted",
        detail="This index can't take new documents right now.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="search_index_capacity",
        status=409,
        title="No room for another index",
        detail=(
            "The search service has no room for another index. "
            "Delete an unused index or ask an operator to add capacity."
        ),
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="no_current_version",
        status=409,
        title="No ingested version",
        detail="This document has no ingested version to reprocess. Upload it instead.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="content_unchanged",
        status=409,
        title="File unchanged",
        detail="This file is already in the index with the same content.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="duplicate_content",
        status=409,
        title="Duplicate file",
        detail="The same file is already in this index under another name.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="update_not_confirmed",
        status=409,
        title="Update not confirmed",
        detail="This document already exists. Confirm the update to replace it.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="version_not_uploading",
        status=409,
        title="Upload not open",
        detail="This upload isn't waiting for a file. Restart the upload.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="turn_in_progress",
        status=409,
        title="Answer still being written",
        detail=(
            "An answer is still being written in this conversation. "
            "Wait for it to finish, then ask your question."
        ),
        retryable=True,
        area=_CONV,
        extensions=_RETRY | _SESSION_TURN,
        retry_after=(1, RETRY_AFTER_MAX),
    ),
    CodeEntry(
        code="turn_already_ended",
        status=409,
        title="Request already finished",
        detail=(
            "That request already finished without a complete answer. Resend the question to retry."
        ),
        retryable=True,
        area=_CONV,
        extensions=_SESSION_TURN,
    ),
    CodeEntry(
        code="client_turn_id_conflict",
        status=409,
        title="Request identifier reused",
        detail=(
            "This request reused an identifier from a different question. "
            "Reload the page and retry."
        ),
        retryable=False,
        area=_CONV,
        extensions=_SESSION_TURN,
    ),
    CodeEntry(
        code="session_turn_limit",
        status=409,
        title="Conversation limit reached",
        detail="This conversation has reached its length limit. Start a new chat to continue.",
        retryable=False,
        area=_CONV,
        extensions=frozenset({"sessionId"}),
    ),
    CodeEntry(
        code="feedback_not_allowed",
        status=409,
        title="Feedback not available",
        detail="Feedback can only be given on an answer.",
        retryable=False,
        area=_CONV,
    ),
    CodeEntry(
        code="document_pdf_unavailable",
        status=409,
        title="Full document unavailable",
        detail="This document is too large to download as one file. Its cited pages still open.",
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="document_changed",
        status=410,
        title="Document changed",
        detail=(
            "This document has changed since the answer was written. "
            "Ask the question now for an answer from the current version."
        ),
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="content_length_required",
        status=411,
        title="File size missing",
        detail="The upload must state its size.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="precondition_failed",
        status=412,
        title="Changed by someone else",
        detail="Someone else changed this item. Reload it to see the latest version, then retry.",
        retryable=True,
        area=_IA,
    ),
    CodeEntry(
        code="request_too_large",
        status=413,
        title="Request too large",
        detail="The request is too large.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="file_too_large",
        status=413,
        title="File too large",
        detail="The file is larger than the allowed size.",
        retryable=False,
        area=_ING,
    ),
    CodeEntry(
        code="unsupported_media_type",
        status=415,
        title="Unsupported format",
        detail="This request was sent in a format this operation doesn't accept.",
        retryable=False,
        area=_IA,
    ),
    CodeEntry(
        code="validation_failed",
        status=422,
        title="Invalid input",
        detail="Some of the information sent isn't valid.",
        retryable=False,
        area=_IA,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="method_not_found",
        status=422,
        title="Unknown method",
        detail="The selected ingestion method isn't available.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="method_inactive",
        status=422,
        title="Method not available",
        detail="The selected ingestion method isn't available.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="invalid_method_parameters",
        status=422,
        title="Invalid method settings",
        detail="Some method settings aren't valid. Check the highlighted fields.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="unsupported_embedding_model",
        status=422,
        title="Embedding model not available",
        detail="The selected option isn't available.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="unsupported_dimensions",
        status=422,
        title="Dimensions not available",
        detail="The selected option isn't available.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="unsupported_analyzer",
        status=422,
        title="Language analyzer not available",
        detail="The selected option isn't available.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="empty_update",
        status=422,
        title="Nothing to change",
        detail="Change at least one setting.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="batch_too_large",
        status=422,
        title="Too many files",
        # The only text with a variable: DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH fills it.
        detail="Send at most {maxFilesPerBatch} files at a time.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="invalid_file_name",
        status=422,
        title="Invalid file name",
        detail=(
            "Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names "
            "can be uploaded."
        ),
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="unsupported_type",
        status=422,
        title="File type not accepted",
        detail=(
            "Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names "
            "can be uploaded."
        ),
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="empty_file",
        status=422,
        title="Empty file",
        detail=(
            "Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names "
            "can be uploaded."
        ),
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="project_not_mapped",
        status=422,
        title="Project doesn't use this index",
        detail="Choose a project that uses this index.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="size_mismatch",
        status=422,
        title="File size changed",
        detail="The file's size changed during upload. Retry the upload.",
        retryable=True,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="file_signature_mismatch",
        status=422,
        title="Content doesn't match file type",
        detail="The file's content doesn't match its type.",
        retryable=False,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="hash_mismatch",
        status=422,
        title="File changed after checking",
        detail="The file changed after it was checked. Select the file and upload it.",
        retryable=True,
        area=_ING,
        extensions=_ERRORS,
    ),
    CodeEntry(
        code="precondition_required",
        status=428,
        title="Version needed",
        detail="This change needs the version of the item you're editing. Reload it and retry.",
        retryable=True,
        area=_IA,
    ),
    CodeEntry(
        code="rate_limited",
        status=429,
        title="Request limit reached",
        detail="You're sending requests too quickly. Wait a moment, then retry.",
        retryable=True,
        area=_CHAT,
        extensions=_RETRY,
        retry_after=(1, 120),
    ),
    CodeEntry(
        code="too_many_streams",
        status=429,
        title="Too many answers in progress",
        detail="You already have answers being written. Wait for one to finish, then retry.",
        retryable=True,
        area=_CHAT,
        extensions=_RETRY,
        retry_after=(10, 10),
    ),
    CodeEntry(
        code="too_many_downloads",
        status=429,
        title="Too many sources loading",
        detail="Too many sources are loading at once. Wait for one to finish, then retry.",
        retryable=True,
        area=_CHAT,
        extensions=_RETRY,
        retry_after=(10, 10),
    ),
    CodeEntry(
        code="token_quota_exceeded",
        status=429,
        title="Usage limit reached",
        detail="You've reached your usage limit for now. You can ask more after the time shown.",
        retryable=True,
        area=_CHAT,
        extensions=_RETRY,
        retry_after=(1, RETRY_AFTER_MAX),
    ),
    CodeEntry(
        code="too_many_requests",
        status=429,
        title="Too many requests",
        detail="You're sending requests too quickly. Wait a moment, then retry.",
        retryable=True,
        area=_IA,
        extensions=_RETRY,
        retry_after=(1, 1),
    ),
    CodeEntry(
        code="write_rate_limited",
        status=429,
        title="Too many changes",
        detail="You're making changes too quickly. Wait a moment, then retry.",
        retryable=True,
        area=_IA,
        extensions=_RETRY,
        retry_after=(1, 120),
    ),
    CodeEntry(
        code="upload_busy",
        status=429,
        title="Too many uploads",
        detail="Too many uploads are running. Retrying in a few seconds.",
        retryable=True,
        area=_ING,
        extensions=_RETRY,
        retry_after=(5, 5),
    ),
    CodeEntry(
        code="internal_error",
        status=500,
        title="Unexpected error",
        detail=(
            "Something went wrong on our side. "
            "Retry, and quote the reference if it keeps happening."
        ),
        retryable=True,
        area=_IA,
    ),
    CodeEntry(
        code="citation_content_missing",
        status=500,
        title="Source page unavailable",
        detail="This source couldn't be loaded. Retry later.",
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="document_pdf_missing",
        status=500,
        title="Document PDF unavailable",
        detail="The document's PDF couldn't be loaded. Retry later.",
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="authentication_unavailable",
        status=503,
        title="Sign-in check unavailable",
        detail="We can't check your sign-in right now. Retry in a minute.",
        retryable=True,
        area=_IA,
        extensions=_RETRY,
        retry_after=(30, 30),
    ),
    CodeEntry(
        code="dependency_unavailable",
        status=503,
        title="Service unavailable",
        detail="A service DocChat depends on isn't responding. Retry in a few seconds.",
        retryable=True,
        area=_IA,
        extensions=_RETRY,
        retry_after=(5, 5),
    ),
    CodeEntry(
        code="storage_unavailable",
        status=503,
        title="File storage unavailable",
        detail="The file couldn't be stored. Retry the upload.",
        retryable=True,
        area=_ING,
    ),
    CodeEntry(
        code="malware_scanning_not_ready",
        status=503,
        title="Uploads paused",
        detail="Uploads are paused until malware scanning confirms it's working. Retry later.",
        retryable=True,
        area=_ING,
        extensions=_RETRY,
        retry_after=(3600, 3600),
    ),
)

# Section 2.3: the 13 codes that reach a client only as a stream `error` event after `200`;
# `internal_error` is the problem code above, used as a stream code with the same text.
_STREAM_ONLY_CODES: Final = (
    CodeEntry(
        code="conversation_unavailable",
        status=None,
        title="Conversation unavailable",
        detail="Your conversation couldn't be loaded. Retry in a moment.",
        retryable=True,
        area=_CHAT,
    ),
    CodeEntry(
        code="retrieval_unavailable",
        status=None,
        title="Document search unavailable",
        detail="The document search isn't responding. Retry in a moment.",
        retryable=True,
        area=_CHAT,
    ),
    CodeEntry(
        code="source_screening_unavailable",
        status=None,
        title="Source safety check unavailable",
        detail="The safety check for sources isn't available right now. Retry in a moment.",
        retryable=True,
        area=_CHAT,
    ),
    CodeEntry(
        code="prompt_too_large",
        status=None,
        title="Conversation too long",
        detail="This conversation is too long to continue. Start a new chat.",
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="question_filtered",
        status=None,
        title="Question blocked",
        detail=(
            "This question was blocked by the content safety filter. Rephrase it, then send it."
        ),
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="answer_filtered",
        status=None,
        title="Answer blocked",
        detail=(
            "The answer was stopped by the content safety filter. Try asking in a different way."
        ),
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="model_busy",
        status=None,
        title="Answer service busy",
        detail="The answer service is busy. Retry shortly.",
        retryable=True,
        area=_CHAT,
        retry_after=(1, 60),
    ),
    CodeEntry(
        code="model_unavailable",
        status=None,
        title="Answer service unavailable",
        detail="The answer service isn't responding. Retry in a moment.",
        retryable=True,
        area=_CHAT,
    ),
    CodeEntry(
        code="model_request_rejected",
        status=None,
        title="Request not processed",
        detail="The answer service couldn't process this request. Try a shorter question.",
        retryable=False,
        area=_CHAT,
    ),
    CodeEntry(
        code="answer_incomplete",
        status=None,
        title="Answer incomplete",
        detail="The answer couldn't be completed. Retry or narrow the question.",
        retryable=True,
        area=_CHAT,
    ),
    CodeEntry(
        code="turn_timeout",
        status=None,
        title="Answer took too long",
        detail="The answer took too long. Retry the question.",
        retryable=True,
        area=_CHAT,
    ),
    CodeEntry(
        code="turn_save_failed",
        status=None,
        title="Answer not saved",
        detail=(
            "The answer was shown but couldn't be saved to your history. "
            "Resend the question if you need it saved."
        ),
        retryable=True,
        area=_CHAT,
    ),
    CodeEntry(
        code="service_restarting",
        status=None,
        title="Service restarting",
        detail="The service is restarting. Resend your question in a moment.",
        retryable=True,
        area=_CHAT,
        retry_after=(5, 5),
    ),
)

# Section 2.4: stored as a turn's `error.code` and shown as `TurnView.errorCode`; never a problem
# and never a stream event.
_STORED_ONLY_CODES: Final = (
    CodeEntry(
        code="turn_abandoned",
        status=None,
        title="Answer interrupted",
        detail="This answer was interrupted before it finished.",
        retryable=True,
        area=_CONV,
    ),
)


def _catalogue(entries: Sequence[CodeEntry]) -> Mapping[str, CodeEntry]:
    by_code: dict[str, CodeEntry] = {}
    for entry in entries:
        if entry.code in by_code:
            raise ValueError(f"code {entry.code!r} is registered twice")
        by_code[entry.code] = entry
    return MappingProxyType(by_code)


CODES: Final[Mapping[str, CodeEntry]] = _catalogue(
    (*_PROBLEM_CODES, *_STREAM_ONLY_CODES, *_STORED_ONLY_CODES)
)
"""Every code of `11-api.md` sections 2.2 to 2.4, keyed by code; read-only."""

STREAM_ONLY_CODES: Final = frozenset(entry.code for entry in _STREAM_ONLY_CODES)
"""The 13 codes that exist only as a stream `error` event."""
