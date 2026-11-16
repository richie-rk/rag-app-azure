// The API's problem catalogue (11-api.md section 2.2): each code's status, one title and one safe
// detail, word for word, so a scripted problem carries exactly what the real API would send and
// the browser specs can assert the client shows it unchanged.

export interface CatalogueEntry {
  readonly status: number;
  readonly title: string;
  readonly detail: string;
}

const entries = {
  authentication_required: [401, "Sign-in required", "Sign in to continue."],
  invalid_token: [
    401,
    "Sign-in not accepted",
    "Your sign-in isn't valid any more. Sign in to continue.",
  ],
  role_not_assigned: [
    403,
    "No access",
    "Your account doesn't have access to DocChat. Ask your administrator to give you access.",
  ],
  admin_required: [403, "Administrator access needed", "This needs administrator access."],
  route_not_found: [404, "Not found", "There's nothing at this address."],
  project_not_found: [
    404,
    "Project not found",
    "This project doesn't exist, or you don't have access to it.",
  ],
  user_not_found: [
    404,
    "Unknown user",
    "There's no user with this ID. People appear here after they first sign in.",
  ],
  index_not_found: [404, "Index not found", "The index wasn't found."],
  document_not_found: [404, "Document not found", "The document wasn't found in this index."],
  version_not_found: [404, "Upload not found", "The upload wasn't found. Restart the upload."],
  run_not_found: [404, "Ingestion run not found", "The ingestion run wasn't found."],
  session_not_found: [
    404,
    "Conversation not found",
    "This conversation isn't available. Start a new chat.",
  ],
  turn_not_found: [404, "Answer not found", "We couldn't find that answer in your conversations."],
  memory_fact_not_found: [
    404,
    "Memory item not found",
    "That memory item doesn't exist. Refresh to see your current memory.",
  ],
  citation_not_found: [404, "Source not found", "This source isn't available."],
  method_not_allowed: [405, "Method not allowed", "This address doesn't support that action."],
  project_name_taken: [409, "Name in use", "Another project already uses this name."],
  project_archived: [
    409,
    "Project archived",
    "This project is archived. Restore it before changing it.",
  ],
  default_index_required: [
    409,
    "Default index needed",
    "A project with indexes needs a default index. Make another index the default instead.",
  ],
  default_index_in_use: [
    409,
    "Default index in use",
    "This is the project's default index. Make another index the default before removing it.",
  ],
  index_not_mappable: [
    409,
    "Index unavailable",
    "This index can't be connected to a project in its current state.",
  ],
  project_index_unavailable: [
    409,
    "Project not ready",
    "This project has nothing to search yet. Ask an administrator to connect an index.",
  ],
  display_name_taken: [
    409,
    "Index name in use",
    "Another index already uses this name. Choose a different name.",
  ],
  index_busy: [
    409,
    "Index busy",
    "This index is busy with another operation. Retry when it finishes.",
  ],
  index_mapped: [
    409,
    "Index used by projects",
    "Remove this index from every project before deleting it.",
  ],
  index_not_accepting_uploads: [
    409,
    "Uploads not accepted",
    "This index can't take new documents right now.",
  ],
  search_index_capacity: [
    409,
    "No room for another index",
    "The search service has no room for another index. Delete an unused index or ask an operator to add capacity.",
  ],
  no_current_version: [
    409,
    "No ingested version",
    "This document has no ingested version to reprocess. Upload it instead.",
  ],
  content_unchanged: [
    409,
    "File unchanged",
    "This file is already in the index with the same content.",
  ],
  duplicate_content: [
    409,
    "Duplicate file",
    "The same file is already in this index under another name.",
  ],
  update_not_confirmed: [
    409,
    "Update not confirmed",
    "This document already exists. Confirm the update to replace it.",
  ],
  version_not_uploading: [
    409,
    "Upload not open",
    "This upload isn't waiting for a file. Restart the upload.",
  ],
  turn_in_progress: [
    409,
    "Answer still being written",
    "An answer is still being written in this conversation. Wait for it to finish, then ask your question.",
  ],
  turn_already_ended: [
    409,
    "Request already finished",
    "That request already finished without a complete answer. Resend the question to retry.",
  ],
  client_turn_id_conflict: [
    409,
    "Request identifier reused",
    "This request reused an identifier from a different question. Reload the page and retry.",
  ],
  session_turn_limit: [
    409,
    "Conversation limit reached",
    "This conversation has reached its length limit. Start a new chat to continue.",
  ],
  feedback_not_allowed: [409, "Feedback not available", "Feedback can only be given on an answer."],
  document_pdf_unavailable: [
    409,
    "Full document unavailable",
    "This document is too large to download as one file. Its cited pages still open.",
  ],
  document_changed: [
    410,
    "Document changed",
    "This document has changed since the answer was written. Ask the question now for an answer from the current version.",
  ],
  content_length_required: [411, "File size missing", "The upload must state its size."],
  precondition_failed: [
    412,
    "Changed by someone else",
    "Someone else changed this item. Reload it to see the latest version, then retry.",
  ],
  request_too_large: [413, "Request too large", "The request is too large."],
  file_too_large: [413, "File too large", "The file is larger than the allowed size."],
  unsupported_media_type: [
    415,
    "Unsupported format",
    "This request was sent in a format this operation doesn't accept.",
  ],
  validation_failed: [422, "Invalid input", "Some of the information sent isn't valid."],
  method_not_found: [422, "Unknown method", "The selected ingestion method isn't available."],
  method_inactive: [422, "Method not available", "The selected ingestion method isn't available."],
  invalid_method_parameters: [
    422,
    "Invalid method settings",
    "Some method settings aren't valid. Check the highlighted fields.",
  ],
  unsupported_embedding_model: [
    422,
    "Embedding model not available",
    "The selected option isn't available.",
  ],
  unsupported_dimensions: [422, "Dimensions not available", "The selected option isn't available."],
  unsupported_analyzer: [
    422,
    "Language analyzer not available",
    "The selected option isn't available.",
  ],
  empty_update: [422, "Nothing to change", "Change at least one setting."],
  // The one detail with a variable; the stub's batch limit is the setting's default of 100.
  batch_too_large: [422, "Too many files", "Send at most 100 files at a time."],
  invalid_file_name: [
    422,
    "Invalid file name",
    "Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names can be uploaded.",
  ],
  unsupported_type: [
    422,
    "File type not accepted",
    "Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names can be uploaded.",
  ],
  empty_file: [
    422,
    "Empty file",
    "Only non-empty PDF, Word (.docx) and PowerPoint (.pptx) files with valid names can be uploaded.",
  ],
  project_not_mapped: [
    422,
    "Project doesn't use this index",
    "Choose a project that uses this index.",
  ],
  size_mismatch: [
    422,
    "File size changed",
    "The file's size changed during upload. Retry the upload.",
  ],
  file_signature_mismatch: [
    422,
    "Content doesn't match file type",
    "The file's content doesn't match its type.",
  ],
  hash_mismatch: [
    422,
    "File changed after checking",
    "The file changed after it was checked. Select the file and upload it.",
  ],
  precondition_required: [
    428,
    "Version needed",
    "This change needs the version of the item you're editing. Reload it and retry.",
  ],
  rate_limited: [
    429,
    "Request limit reached",
    "You're sending requests too quickly. Wait a moment, then retry.",
  ],
  too_many_streams: [
    429,
    "Too many answers in progress",
    "You already have answers being written. Wait for one to finish, then retry.",
  ],
  too_many_downloads: [
    429,
    "Too many sources loading",
    "Too many sources are loading at once. Wait for one to finish, then retry.",
  ],
  token_quota_exceeded: [
    429,
    "Usage limit reached",
    "You've reached your usage limit for now. You can ask more after the time shown.",
  ],
  too_many_requests: [
    429,
    "Too many requests",
    "You're sending requests too quickly. Wait a moment, then retry.",
  ],
  write_rate_limited: [
    429,
    "Too many changes",
    "You're making changes too quickly. Wait a moment, then retry.",
  ],
  upload_busy: [
    429,
    "Too many uploads",
    "Too many uploads are running. Retrying in a few seconds.",
  ],
  internal_error: [
    500,
    "Unexpected error",
    "Something went wrong on our side. Retry, and quote the reference if it keeps happening.",
  ],
  citation_content_missing: [
    500,
    "Source page unavailable",
    "This source couldn't be loaded. Retry later.",
  ],
  document_pdf_missing: [
    500,
    "Document PDF unavailable",
    "The document's PDF couldn't be loaded. Retry later.",
  ],
  authentication_unavailable: [
    503,
    "Sign-in check unavailable",
    "We can't check your sign-in right now. Retry in a minute.",
  ],
  dependency_unavailable: [
    503,
    "Service unavailable",
    "A service DocChat depends on isn't responding. Retry in a few seconds.",
  ],
  storage_unavailable: [
    503,
    "File storage unavailable",
    "The file couldn't be stored. Retry the upload.",
  ],
  malware_scanning_not_ready: [
    503,
    "Uploads paused",
    "Uploads are paused until malware scanning confirms it's working. Retry later.",
  ],
} as const satisfies Readonly<Record<string, readonly [number, string, string]>>;

export type ProblemCode = keyof typeof entries;

export function catalogueEntry(code: ProblemCode): CatalogueEntry {
  const [status, title, detail] = entries[code];
  return { status, title, detail };
}

export function isProblemCode(value: string): value is ProblemCode {
  return Object.hasOwn(entries, value);
}
