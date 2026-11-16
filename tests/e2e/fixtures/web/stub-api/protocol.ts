// What the browser specs and the stub API exchange (10-web-client.md section 13.2). The stub API
// answers the web client on 127.0.0.1:4010; the specs drive it over a separate control port, so
// nothing the client can reach changes the stub's behaviour. Each test owns one world: its fixture
// data, its scripted answers and streams and its recorded requests. The stub identity provider
// mints access tokens that name the world, which is how the stub tells parallel tests apart.

import type {
  AdminProject,
  Citation,
  CurrentUser,
  DocumentDetail,
  FieldError,
  IndexDetail,
  IndexOptions,
  IngestionRunDetail,
  MemoryView,
  PreflightRejection,
  PreflightStatus,
  ProjectIndexMapping,
  ProjectMember,
  ProjectSummary,
  StreamErrorEvent,
  TurnView,
  UserSummary,
} from "../../../../../apps/web/src/api/generated/types.gen.ts";
import type { ProblemCode } from "./problems.ts";

export const APP_ORIGIN = "http://127.0.0.1:4280";
export const API_HOST = "127.0.0.1";
export const API_PORT = 4010;
export const API_ORIGIN = `http://${API_HOST}:${String(API_PORT)}`;
export const CONTROL_PORT = 4011;
export const CONTROL_ORIGIN = `http://${API_HOST}:${String(CONTROL_PORT)}`;

/** An access token the stub identity provider mints: `stub-at.<world>.<serial>`. */
export const ACCESS_TOKEN_PATTERN = /^stub-at\.([0-9a-f]{32})\.(\d+)$/;

/** Every operation of 10-web-client.md section 5.2. */
export const OPERATIONS = [
  "getCurrentUser",
  "listMyProjects",
  "createChatTurn",
  "listSessions",
  "getSession",
  "listSessionTurns",
  "putTurnFeedback",
  "getCitation",
  "getCitationContent",
  "getCitationDocument",
  "getMemory",
  "updateMemorySettings",
  "updateMemoryFact",
  "deleteMemoryFact",
  "clearMemory",
  "adminListProjects",
  "adminCreateProject",
  "adminGetProject",
  "adminUpdateProject",
  "adminListProjectMembers",
  "adminAddProjectMember",
  "adminRemoveProjectMember",
  "adminSearchUsers",
  "adminListProjectIndexes",
  "adminMapProjectIndex",
  "adminUnmapProjectIndex",
  "adminGetIndexOptions",
  "adminListIndexes",
  "adminCreateIndex",
  "adminGetIndex",
  "adminUpdateIndex",
  "adminDeleteIndex",
  "adminStartReingest",
  "adminListDocuments",
  "adminGetDocument",
  "adminGetDocumentPdf",
  "adminDeleteDocument",
  "adminReingestDocument",
  "adminPreflightUploads",
  "adminReserveUpload",
  "adminUploadContent",
  "adminSubmitUploads",
  "adminListIngestionRuns",
  "adminGetIngestionRun",
] as const;

export type OperationId = (typeof OPERATIONS)[number];

/** The file names `getCitation` answers for one citation; cross-origin scripts can't read
 * `Content-Disposition`, so these are what the client names its downloads from. */
export interface CitationFile {
  readonly fileName: string;
  readonly documentFileName: string;
  readonly documentPdfAvailable: boolean;
}

/** A stored turn: the 13 keys of `TurnView`, plus the citation files keyed by number. */
export interface StoredTurn extends TurnView {
  readonly files?: Readonly<Record<string, CitationFile>>;
}

export interface StoredSession {
  readonly sessionId: string;
  readonly projectId: string;
  readonly title: string;
  readonly createdAt: string;
  readonly lastTurnAt: string;
  /** Oldest first. */
  readonly turns: readonly StoredTurn[];
}

/** What `adminPreflightUploads` answers for a file name; a name without an entry is `new`. */
export interface PreflightAnswer {
  readonly status: PreflightStatus;
  readonly reason?: PreflightRejection;
  readonly existingDocumentName?: string;
}

export interface AdminData {
  readonly projects: readonly AdminProject[];
  /** By project ID. */
  readonly members: Readonly<Record<string, readonly ProjectMember[]>>;
  /** By project ID. */
  readonly mappings: Readonly<Record<string, readonly ProjectIndexMapping[]>>;
  readonly users: readonly UserSummary[];
  readonly indexes: readonly IndexDetail[];
  /** By index ID. */
  readonly documents: Readonly<Record<string, readonly DocumentDetail[]>>;
  readonly runs: readonly IngestionRunDetail[];
  readonly indexOptions: IndexOptions;
  /** By file name. */
  readonly preflight: Readonly<Record<string, PreflightAnswer>>;
}

/** A world's fixture data; every member left out takes the stub's default. */
export interface WorldInit {
  readonly user?: Partial<CurrentUser>;
  readonly projects?: readonly ProjectSummary[];
  /** Newest first, as `listSessions` orders them. */
  readonly sessions?: readonly StoredSession[];
  readonly memory?: MemoryView;
  readonly admin?: Partial<AdminData>;
  /** The page size `listSessions` uses, whatever `limit` asks; small values force `nextCursor`. */
  readonly sessionsPageSize?: number;
}

/** One scripted answer in place of the operation's default behaviour. */
export type ScriptedAnswer =
  /** A problem from the API's catalogue, with the optional members a code carries. */
  | {
      readonly kind: "problem";
      readonly code: ProblemCode;
      readonly retryAfterSeconds?: number;
      readonly errors?: readonly FieldError[];
      readonly sessionId?: string;
      readonly turnId?: string;
      /** A fixed reference ID, so a case can assert the client shows it. */
      readonly correlationId?: string;
    }
  /** A status with no body: platform authentication, or App Service while the site is stopped. */
  | { readonly kind: "bare"; readonly status: number }
  /** A file operation's `200 application/pdf` whose body ends before its `Content-Length`. */
  | { readonly kind: "truncated" }
  /** The default behaviour, after a delay. */
  | { readonly kind: "delay"; readonly ms: number };

export interface Script {
  readonly operation: OperationId;
  /** Only requests with these query values; `true` matches any value that is present. */
  readonly query?: Readonly<Record<string, string | true>>;
  /** How many matching requests it answers (default 1). */
  readonly times?: number;
  readonly answer: ScriptedAnswer;
}

/**
 * The scripted NDJSON streams of section 13.2. `normal` sends meta, deltas, citations,
 * followups and done; the others change one thing:
 * - `byteSplit`: the same events, written one byte at a time;
 * - `slow`: a pause and a heartbeat between events;
 * - `broadened`: a `search_broadened` status after meta;
 * - `error`: an `error` event after the deltas (or after citations, with `errorAfterCitations`);
 * - `missingTerminal`: the body ends after citations, with no terminal event;
 * - `stalled`: nothing after the first delta, until the client gives up;
 * - `oversized`: a line longer than 1,048,576 bytes after meta;
 * - `invalid`: a known event that fails its schema after meta;
 * - `unknownType`: an event of an unknown type among the normal ones.
 */
export type StreamMode =
  | "normal"
  | "byteSplit"
  | "slow"
  | "broadened"
  | "error"
  | "missingTerminal"
  | "stalled"
  | "oversized"
  | "invalid"
  | "unknownType";

export interface StreamScript {
  readonly mode?: StreamMode;
  /** The answer; its deltas split it at sentence ends. */
  readonly answer?: string;
  readonly citations?: readonly Citation[];
  readonly allowedLinks?: readonly string[];
  readonly followups?: readonly string[];
  readonly files?: Readonly<Record<string, CitationFile>>;
  readonly truncated?: boolean;
  readonly error?: {
    readonly code: StreamErrorEvent["code"];
    readonly retryable: boolean;
    readonly retryAfterSeconds?: number | null;
  };
  readonly errorAfterCitations?: boolean;
  /** Holds the stream at this point, sending heartbeats, until the spec releases it;
   * `afterStatus` holds a `broadened` stream between its status and its first delta. */
  readonly hold?: "afterMeta" | "afterStatus" | "beforeCitations";
}

export interface RecordedRequest {
  readonly method: string;
  readonly path: string;
  readonly query: Readonly<Record<string, string>>;
  readonly operation: OperationId | null;
  readonly headers: Readonly<Record<string, string>>;
  /** The parsed JSON body, or null. */
  readonly body: unknown;
  readonly bodyBytes: number;
  readonly contentType: string | null;
  /** Epoch milliseconds at arrival. */
  readonly at: number;
}

/** A world as the specs read it back. */
export interface WorldSnapshot {
  readonly id: string;
  readonly user: CurrentUser;
  readonly projects: readonly ProjectSummary[];
  readonly sessions: readonly StoredSession[];
  readonly memory: MemoryView;
  readonly admin: AdminData;
  /** Requests the stub refused because a header was missing or invalid. */
  readonly violations: readonly string[];
}
