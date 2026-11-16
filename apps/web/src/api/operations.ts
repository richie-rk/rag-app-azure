// The hand-written operation table (10-web-client.md section 5.1): one entry per operationId of
// section 5.2, with its method, path template, how its answer
// is read and the generated schema its `2xx` body must pass. The generator is a 0.x release, so
// the client depends only on its types and schemas, never on a generated client runtime. Each
// entry's path is typed against the generated `…Data['url']`, and a unit test compares method and
// path with the document.
//
// Adding an operation: add one entry below, typed with its generated `…Data` type.

import type { z } from "zod";

import "../bootstrap/zodSetup";
import { PDF_DOCUMENT_MAX_BYTES, PDF_PAGE_MAX_BYTES } from "../platform/constants";
import type * as T from "./generated/types.gen";
import * as Z from "./generated/zod.gen";

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

/**
 * How the client reads a `2xx`:
 * - `json`: `application/json`, parsed with `schema`;
 * - `ndjson`: the chat stream, handed to the reader of `src/chat/stream/`;
 * - `file`: a PDF's bytes, at most `maxBytes`;
 * - `upload`: the upload body, sent by `XMLHttpRequest` for its progress events.
 */
export type AnswerKind = "json" | "ndjson" | "file" | "upload";

declare const dataType: unique symbol;

interface OperationFields {
  readonly method: HttpMethod;
  readonly path: string;
  readonly answer: AnswerKind;
  readonly schema: z.ZodType | null;
  /** Whether the operation sends `If-Match` (section 5.6). */
  readonly ifMatch: boolean;
  readonly maxBytes?: number;
}

/** An entry, carrying its generated `…Data` type for the request arguments. */
export type OperationSpec<D> = OperationFields & { readonly [dataType]?: D };

function operation<D extends { url: string }>() {
  return <const S extends OperationFields & { path: D["url"] }>(spec: S): S & OperationSpec<D> =>
    spec;
}

export const operations = {
  getCurrentUser: operation<T.GetCurrentUserData>()({
    method: "GET",
    path: "/v1/me",
    answer: "json",
    schema: Z.zGetCurrentUserResponse,
    ifMatch: false,
  }),
  listMyProjects: operation<T.ListMyProjectsData>()({
    method: "GET",
    path: "/v1/projects",
    answer: "json",
    schema: Z.zListMyProjectsResponse,
    ifMatch: false,
  }),
  createChatTurn: operation<T.CreateChatTurnData>()({
    method: "POST",
    path: "/v1/chat/turns",
    answer: "ndjson",
    schema: null,
    ifMatch: false,
  }),
  listSessions: operation<T.ListSessionsData>()({
    method: "GET",
    path: "/v1/sessions",
    answer: "json",
    schema: Z.zListSessionsResponse,
    ifMatch: false,
  }),
  getSession: operation<T.GetSessionData>()({
    method: "GET",
    path: "/v1/sessions/{sessionId}",
    answer: "json",
    schema: Z.zGetSessionResponse,
    ifMatch: false,
  }),
  listSessionTurns: operation<T.ListSessionTurnsData>()({
    method: "GET",
    path: "/v1/sessions/{sessionId}/turns",
    answer: "json",
    schema: Z.zListSessionTurnsResponse,
    ifMatch: false,
  }),
  putTurnFeedback: operation<T.PutTurnFeedbackData>()({
    method: "PUT",
    path: "/v1/sessions/{sessionId}/turns/{turnId}/feedback",
    answer: "json",
    schema: Z.zPutTurnFeedbackResponse,
    ifMatch: false,
  }),
  getCitation: operation<T.GetCitationData>()({
    method: "GET",
    path: "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}",
    answer: "json",
    schema: Z.zGetCitationResponse,
    ifMatch: false,
  }),
  getCitationContent: operation<T.GetCitationContentData>()({
    method: "GET",
    path: "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/content",
    answer: "file",
    schema: null,
    ifMatch: false,
    maxBytes: PDF_PAGE_MAX_BYTES,
  }),
  getCitationDocument: operation<T.GetCitationDocumentData>()({
    method: "GET",
    path: "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/document",
    answer: "file",
    schema: null,
    ifMatch: false,
    maxBytes: PDF_DOCUMENT_MAX_BYTES,
  }),
  getMemory: operation<T.GetMemoryData>()({
    method: "GET",
    path: "/v1/memory",
    answer: "json",
    schema: Z.zGetMemoryResponse,
    ifMatch: false,
  }),
  updateMemorySettings: operation<T.UpdateMemorySettingsData>()({
    method: "PATCH",
    path: "/v1/memory",
    answer: "json",
    schema: Z.zUpdateMemorySettingsResponse,
    ifMatch: true,
  }),
  updateMemoryFact: operation<T.UpdateMemoryFactData>()({
    method: "PUT",
    path: "/v1/memory/facts/{factId}",
    answer: "json",
    schema: Z.zUpdateMemoryFactResponse,
    ifMatch: true,
  }),
  deleteMemoryFact: operation<T.DeleteMemoryFactData>()({
    method: "DELETE",
    path: "/v1/memory/facts/{factId}",
    answer: "json",
    schema: Z.zDeleteMemoryFactResponse,
    ifMatch: true,
  }),
  clearMemory: operation<T.ClearMemoryData>()({
    method: "DELETE",
    path: "/v1/memory/facts",
    answer: "json",
    schema: Z.zClearMemoryResponse,
    ifMatch: true,
  }),
  adminListProjects: operation<T.AdminListProjectsData>()({
    method: "GET",
    path: "/v1/admin/projects",
    answer: "json",
    schema: Z.zAdminListProjectsResponse,
    ifMatch: false,
  }),
  adminCreateProject: operation<T.AdminCreateProjectData>()({
    method: "POST",
    path: "/v1/admin/projects",
    answer: "json",
    schema: Z.zAdminCreateProjectResponse,
    ifMatch: false,
  }),
  adminGetProject: operation<T.AdminGetProjectData>()({
    method: "GET",
    path: "/v1/admin/projects/{projectId}",
    answer: "json",
    schema: Z.zAdminGetProjectResponse,
    ifMatch: false,
  }),
  adminUpdateProject: operation<T.AdminUpdateProjectData>()({
    method: "PATCH",
    path: "/v1/admin/projects/{projectId}",
    answer: "json",
    schema: Z.zAdminUpdateProjectResponse,
    ifMatch: true,
  }),
  adminListProjectMembers: operation<T.AdminListProjectMembersData>()({
    method: "GET",
    path: "/v1/admin/projects/{projectId}/members",
    answer: "json",
    schema: Z.zAdminListProjectMembersResponse,
    ifMatch: false,
  }),
  adminAddProjectMember: operation<T.AdminAddProjectMemberData>()({
    method: "PUT",
    path: "/v1/admin/projects/{projectId}/members/{userId}",
    answer: "json",
    schema: Z.zAdminAddProjectMemberResponse,
    ifMatch: true,
  }),
  adminRemoveProjectMember: operation<T.AdminRemoveProjectMemberData>()({
    method: "DELETE",
    path: "/v1/admin/projects/{projectId}/members/{userId}",
    answer: "json",
    schema: Z.zAdminRemoveProjectMemberResponse,
    ifMatch: true,
  }),
  adminSearchUsers: operation<T.AdminSearchUsersData>()({
    method: "GET",
    path: "/v1/admin/users",
    answer: "json",
    schema: Z.zAdminSearchUsersResponse,
    ifMatch: false,
  }),
  adminListProjectIndexes: operation<T.AdminListProjectIndexesData>()({
    method: "GET",
    path: "/v1/admin/projects/{projectId}/indexes",
    answer: "json",
    schema: Z.zAdminListProjectIndexesResponse,
    ifMatch: false,
  }),
  adminMapProjectIndex: operation<T.AdminMapProjectIndexData>()({
    method: "PUT",
    path: "/v1/admin/projects/{projectId}/indexes/{indexId}",
    answer: "json",
    schema: Z.zAdminMapProjectIndexResponse,
    ifMatch: true,
  }),
  adminUnmapProjectIndex: operation<T.AdminUnmapProjectIndexData>()({
    method: "DELETE",
    path: "/v1/admin/projects/{projectId}/indexes/{indexId}",
    answer: "json",
    schema: Z.zAdminUnmapProjectIndexResponse,
    ifMatch: true,
  }),
  adminGetIndexOptions: operation<T.AdminGetIndexOptionsData>()({
    method: "GET",
    path: "/v1/admin/index-options",
    answer: "json",
    schema: Z.zAdminGetIndexOptionsResponse,
    ifMatch: false,
  }),
  adminListIndexes: operation<T.AdminListIndexesData>()({
    method: "GET",
    path: "/v1/admin/indexes",
    answer: "json",
    schema: Z.zAdminListIndexesResponse,
    ifMatch: false,
  }),
  adminCreateIndex: operation<T.AdminCreateIndexData>()({
    method: "POST",
    path: "/v1/admin/indexes",
    answer: "json",
    schema: Z.zAdminCreateIndexResponse,
    ifMatch: false,
  }),
  adminGetIndex: operation<T.AdminGetIndexData>()({
    method: "GET",
    path: "/v1/admin/indexes/{indexId}",
    answer: "json",
    schema: Z.zAdminGetIndexResponse,
    ifMatch: false,
  }),
  adminUpdateIndex: operation<T.AdminUpdateIndexData>()({
    method: "PATCH",
    path: "/v1/admin/indexes/{indexId}",
    answer: "json",
    schema: Z.zAdminUpdateIndexResponse,
    ifMatch: true,
  }),
  adminDeleteIndex: operation<T.AdminDeleteIndexData>()({
    method: "DELETE",
    path: "/v1/admin/indexes/{indexId}",
    answer: "json",
    schema: Z.zAdminDeleteIndexResponse,
    ifMatch: true,
  }),
  adminStartReingest: operation<T.AdminStartReingestData>()({
    method: "POST",
    path: "/v1/admin/indexes/{indexId}/reingest",
    answer: "json",
    schema: Z.zAdminStartReingestResponse,
    ifMatch: true,
  }),
  adminListDocuments: operation<T.AdminListDocumentsData>()({
    method: "GET",
    path: "/v1/admin/indexes/{indexId}/documents",
    answer: "json",
    schema: Z.zAdminListDocumentsResponse,
    ifMatch: false,
  }),
  adminGetDocument: operation<T.AdminGetDocumentData>()({
    method: "GET",
    path: "/v1/admin/indexes/{indexId}/documents/{documentId}",
    answer: "json",
    schema: Z.zAdminGetDocumentResponse,
    ifMatch: false,
  }),
  adminGetDocumentPdf: operation<T.AdminGetDocumentPdfData>()({
    method: "GET",
    path: "/v1/admin/indexes/{indexId}/documents/{documentId}/pdf",
    answer: "file",
    schema: null,
    ifMatch: false,
    maxBytes: PDF_DOCUMENT_MAX_BYTES,
  }),
  adminDeleteDocument: operation<T.AdminDeleteDocumentData>()({
    method: "DELETE",
    path: "/v1/admin/indexes/{indexId}/documents/{documentId}",
    answer: "json",
    schema: Z.zAdminDeleteDocumentResponse,
    ifMatch: true,
  }),
  adminReingestDocument: operation<T.AdminReingestDocumentData>()({
    method: "POST",
    path: "/v1/admin/indexes/{indexId}/documents/{documentId}/reingest",
    answer: "json",
    schema: Z.zAdminReingestDocumentResponse,
    ifMatch: true,
  }),
  adminPreflightUploads: operation<T.AdminPreflightUploadsData>()({
    method: "POST",
    path: "/v1/admin/indexes/{indexId}/uploads/preflight",
    answer: "json",
    schema: Z.zAdminPreflightUploadsResponse,
    ifMatch: false,
  }),
  adminReserveUpload: operation<T.AdminReserveUploadData>()({
    method: "POST",
    path: "/v1/admin/indexes/{indexId}/uploads",
    answer: "json",
    schema: Z.zAdminReserveUploadResponse,
    ifMatch: false,
  }),
  adminUploadContent: operation<T.AdminUploadContentData>()({
    method: "PUT",
    path: "/v1/admin/indexes/{indexId}/uploads/{versionId}/content",
    answer: "upload",
    schema: Z.zAdminUploadContentResponse,
    ifMatch: false,
  }),
  adminSubmitUploads: operation<T.AdminSubmitUploadsData>()({
    method: "POST",
    path: "/v1/admin/indexes/{indexId}/uploads/submit",
    answer: "json",
    schema: Z.zAdminSubmitUploadsResponse,
    ifMatch: false,
  }),
  adminListIngestionRuns: operation<T.AdminListIngestionRunsData>()({
    method: "GET",
    path: "/v1/admin/ingestion-runs",
    answer: "json",
    schema: Z.zAdminListIngestionRunsResponse,
    ifMatch: false,
  }),
  adminGetIngestionRun: operation<T.AdminGetIngestionRunData>()({
    method: "GET",
    path: "/v1/admin/ingestion-runs/{runId}",
    answer: "json",
    schema: Z.zAdminGetIngestionRunResponse,
    ifMatch: false,
  }),
} as const;

export type Operations = typeof operations;
export type OperationId = keyof Operations;

type AnswerOf<A extends AnswerKind> = {
  [K in OperationId]: Operations[K]["answer"] extends A ? K : never;
}[OperationId];

/** Operations whose `2xx` is a JSON body. */
export type JsonOperationId = AnswerOf<"json">;
/** The three file operations, whose `2xx` is a PDF. */
export type FileOperationId = AnswerOf<"file">;

type DataOf<K extends OperationId> = Operations[K] extends OperationSpec<infer D> ? D : never;

type Present<D, F extends PropertyKey> = F extends keyof D ? Exclude<D[F], undefined> : never;

/** `path`, `query` and `body` as the operation's generated `…Data` declares them: required,
 * optional or absent. */
type ArgField<D, F extends "path" | "query" | "body"> = [Present<D, F>] extends [never]
  ? Readonly<Partial<Record<F, never>>>
  : F extends keyof D
    ? undefined extends D[F]
      ? Readonly<Partial<Record<F, Present<D, F>>>>
      : Readonly<Record<F, Present<D, F>>>
    : Readonly<Partial<Record<F, never>>>;

/** The arguments of a call: its path parameters, query, body, `If-Match` version (the resource's
 * `rowVersion` or `etag`, section 5.6) and the `AbortSignal` of the component, stream or
 * download. */
export type RequestArgs<K extends OperationId> = ArgField<DataOf<K>, "path"> &
  ArgField<DataOf<K>, "query"> &
  ArgField<DataOf<K>, "body"> &
  (Operations[K]["ifMatch"] extends true
    ? { readonly ifMatch: string }
    : { readonly ifMatch?: never }) & {
    readonly signal?: AbortSignal;
  };

/** The parsed `2xx` body of an operation with a response schema. */
export type ResponseOf<K extends OperationId> =
  Operations[K]["schema"] extends z.ZodType<infer Output> ? Output : never;
