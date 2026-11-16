// Fixture data for the stub API: the defaults every world starts from, and builders the specs use
// to describe the sessions, turns and admin rows a case needs. Every value passes the generated
// schema the web client parses it with (10-web-client.md section 5.1), so a case fails only on
// the behaviour it tests.

import { randomBytes, randomUUID } from "node:crypto";

import type {
  AdminProject,
  Citation,
  CitationContentType,
  CurrentUser,
  DocumentDetail,
  FeedbackView,
  IndexDetail,
  IndexOptions,
  IngestionRunDetail,
  MemoryView,
  ProjectIndexMapping,
  ProjectMember,
  ProjectSummary,
  UserSummary,
  VersionSummary,
} from "../../../../../apps/web/src/api/generated/types.gen.ts";
import type {
  AdminData,
  CitationFile,
  StoredSession,
  StoredTurn,
  StreamScript,
} from "./protocol.ts";

export function uuid(): string {
  return randomUUID();
}

/** 32 lower-case hexadecimal characters: a correlation ID or a world ID. */
export function hex32(): string {
  return randomBytes(16).toString("hex");
}

/** An ISO 8601 UTC time `offsetSeconds` from now. */
export function isoAt(offsetSeconds = 0): string {
  return new Date(Date.now() + offsetSeconds * 1000).toISOString();
}

/** A SQL `rowversion` as the API writes it: 8 bytes in base64. */
export function rowVersion(serial: number): string {
  const bytes = Buffer.alloc(8);
  bytes.writeBigUInt64BE(BigInt(serial));
  return bytes.toString("base64");
}

export const IDS = {
  user: "6c1f6f0e-2b7a-4c3d-9e5f-0a1b2c3d4e5f",
  admin: "7d2a7a1f-3c8b-4d4e-8f60-1b2c3d4e5f60",
  handbook: "0f3e5c2a-9b1d-4e7f-8a6c-5d4b3a291807",
  fieldGuides: "1a4f6d3b-0c2e-4f80-9b7d-6e5c4b3a2918",
  handbookIndex: "2b5a7e4c-1d3f-4a91-8c8e-7f6d5c4b3a29",
  secondIndex: "3c6b8f5d-2e4a-4ba2-9d9f-807e6d5c4b3a",
  document: "4d7c9a6e-3f5b-4cb3-8eaa-918f7e6d5c4b",
  version: "5e8dab7f-4a6c-4dc4-9fbb-a2a18f7e6d5c",
  run: "6f9ebc8a-5b7d-4ed5-8acc-b3b2a18f7e6d",
} as const;

export const USER_NAME = "Robin Park";
export const USER_EMAIL = "robin.park@contoso.example";

export function defaultUser(): CurrentUser {
  return {
    userId: IDS.user,
    displayName: USER_NAME,
    email: USER_EMAIL,
    isAdmin: false,
    roles: ["User"],
    features: { memoryEnabled: true, feedbackComments: true },
  };
}

/** The user as an administrator: `isAdmin` with both roles. */
export function adminUser(): Partial<CurrentUser> {
  return { isAdmin: true, roles: ["Admin", "User"] };
}

export function defaultProjects(): ProjectSummary[] {
  return [
    {
      projectId: IDS.handbook,
      name: "Employee handbook",
      description: "Policies for leave, travel and expenses.",
      exampleQuestions: ["How many days of leave do I get?", "Who approves travel?"],
      hasDefaultIndex: true,
    },
    {
      projectId: IDS.fieldGuides,
      name: "Field guides",
      description: "",
      exampleQuestions: [],
      hasDefaultIndex: false,
    },
  ];
}

const CONTENT_TYPES = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
} as const satisfies Record<string, CitationContentType>;

export type FileKind = keyof typeof CONTENT_TYPES;

export function citation(
  number: number,
  title: string,
  pageNumber: number,
  kind: FileKind = "pdf",
): Citation {
  return {
    number,
    title,
    pageNumber,
    contentType: CONTENT_TYPES[kind],
    headingPath: [],
    openAs: "pdf_page",
  };
}

export function citationFile(
  fileName: string,
  documentFileName: string,
  documentPdfAvailable = true,
): CitationFile {
  return { fileName, documentFileName, documentPdfAvailable };
}

/** The answer most cases stream: two sentences, each citing one source. */
export const DEFAULT_ANSWER =
  "Employees get 25 days of annual leave each year ⟦1⟧. Unused days carry over until the end of March ⟦2⟧.";

export function defaultCitations(): Citation[] {
  return [citation(1, "Leave policy", 4, "pdf"), citation(2, "Travel guide", 2, "docx")];
}

export function defaultFiles(): Record<string, CitationFile> {
  return {
    "1": citationFile("Leave policy, page 4.pdf", "Leave policy.pdf"),
    "2": citationFile("Travel guide, page 2.pdf", "Travel guide.pdf"),
  };
}

export const DEFAULT_FOLLOWUPS = [
  "Can I buy extra leave days?",
  "How do I request carry-over?",
] as const;

export function defaultStream(): StreamScript {
  return {
    mode: "normal",
    answer: DEFAULT_ANSWER,
    citations: defaultCitations(),
    allowedLinks: [],
    followups: [...DEFAULT_FOLLOWUPS],
    files: defaultFiles(),
  };
}

export interface TurnInit {
  readonly question: string;
  readonly answer?: string;
  readonly status?: StoredTurn["status"];
  readonly errorCode?: StoredTurn["errorCode"];
  readonly citations?: readonly Citation[];
  readonly allowedLinks?: readonly string[];
  readonly followups?: readonly string[];
  readonly feedback?: FeedbackView | null;
  readonly answerTruncated?: boolean;
  readonly files?: Readonly<Record<string, CitationFile>>;
  readonly turnId?: string;
}

/** A stored turn; `seq` and the times are set when its session is built. */
export function turn(init: TurnInit): StoredTurn {
  const status = init.status ?? "completed";
  return {
    turnId: init.turnId ?? uuid(),
    seq: 1,
    question: init.question,
    answer: init.answer ?? (status === "completed" ? DEFAULT_ANSWER : ""),
    answerTruncated: init.answerTruncated ?? false,
    status,
    errorCode: init.errorCode ?? null,
    citations: [...(init.citations ?? (status === "completed" ? defaultCitations() : []))],
    allowedLinks: [...(init.allowedLinks ?? [])],
    followups: [...(init.followups ?? [])],
    feedback: init.feedback ?? null,
    createdAt: isoAt(-3600),
    endedAt: status === "streaming" ? null : isoAt(-3590),
    files: init.files ?? defaultFiles(),
  };
}

export interface SessionInit {
  readonly title: string;
  readonly projectId?: string;
  readonly turns: readonly StoredTurn[];
  readonly sessionId?: string;
  /** How long ago the last turn was, in seconds. */
  readonly ageSeconds?: number;
}

export function session(init: SessionInit): StoredSession {
  const age = init.ageSeconds ?? 600;
  const turns = init.turns.map((item, index) => ({
    ...item,
    seq: index + 1,
    createdAt: isoAt(-age - (init.turns.length - index) * 60),
    endedAt:
      item.status === "streaming" ? null : isoAt(-age - (init.turns.length - index) * 60 + 5),
  }));
  return {
    sessionId: init.sessionId ?? uuid(),
    projectId: init.projectId ?? IDS.handbook,
    title: init.title,
    createdAt: isoAt(-age - init.turns.length * 60 - 60),
    lastTurnAt: isoAt(-age),
    turns,
  };
}

export function feedback(rating: "up" | "down", comment: string | null = null): FeedbackView {
  return { rating, category: null, comment, updatedAt: isoAt(-60) };
}

export function defaultMemory(): MemoryView {
  return {
    enabled: true,
    paused: false,
    etag: '"1"',
    updatedAt: isoAt(-86400),
    facts: [
      {
        factId: "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        text: "Works on the finance team",
        category: "team",
        source: { kind: "extracted", sessionId: null },
        createdAt: isoAt(-172800),
        updatedAt: isoAt(-86400),
      },
      {
        factId: "9b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e",
        text: "Prefers short answers with bullet points",
        category: "answer_style",
        source: { kind: "user", sessionId: null },
        createdAt: isoAt(-172800),
        updatedAt: isoAt(-86400),
      },
    ],
    limits: { maxFactChars: 200, maxFacts: 30, maxTokens: 1000, usedFacts: 2, usedTokens: 18 },
  };
}

const ANALYZERS = [
  ["standard.lucene", "Any language"],
  ["en.microsoft", "English (Microsoft)"],
  ["en.lucene", "English (Lucene)"],
  ["fr.microsoft", "French (Microsoft)"],
  ["fr.lucene", "French (Lucene)"],
  ["de.microsoft", "German (Microsoft)"],
  ["de.lucene", "German (Lucene)"],
  ["es.microsoft", "Spanish (Microsoft)"],
  ["es.lucene", "Spanish (Lucene)"],
  ["it.microsoft", "Italian (Microsoft)"],
  ["it.lucene", "Italian (Lucene)"],
  ["nl.microsoft", "Dutch (Microsoft)"],
  ["nl.lucene", "Dutch (Lucene)"],
  ["pt-Pt.microsoft", "Portuguese, Portugal (Microsoft)"],
  ["pt-Br.microsoft", "Portuguese, Brazil (Microsoft)"],
  ["ja.microsoft", "Japanese (Microsoft)"],
  ["ko.microsoft", "Korean (Microsoft)"],
  ["zh-Hans.microsoft", "Chinese, Simplified (Microsoft)"],
  ["ar.microsoft", "Arabic (Microsoft)"],
  ["hi.microsoft", "Hindi (Microsoft)"],
] as const;

export function defaultIndexOptions(): IndexOptions {
  const languageAnalyzers = ANALYZERS.map(([value, label]) => ({ value, label }));
  return {
    defaults: {
      methodKey: "recursive",
      methodVersion: 1,
      embeddingModel: "text-embedding-3-large",
      embeddingDimensions: 1024,
      languageAnalyzer: "standard.lucene",
    },
    embeddingModels: [
      {
        model: "text-embedding-3-large",
        modelVersion: "1",
        dimensions: [256, 1024, 3072],
        defaultDimensions: 1024,
      },
    ],
    languageAnalyzers: languageAnalyzers as unknown as IndexOptions["languageAnalyzers"],
    methods: [
      {
        key: "recursive",
        version: 1,
        displayName: "Recursive",
        description: "Splits each document into passages of a target size.",
        supportedContentTypes: [CONTENT_TYPES.pdf, CONTENT_TYPES.docx, CONTENT_TYPES.pptx],
        parameterSchema: {
          type: "object",
          additionalProperties: false,
          required: ["targetTokens"],
          properties: {
            targetTokens: {
              type: "integer",
              title: "Target passage size",
              description: "Tokens per passage.",
              minimum: 200,
              maximum: 2000,
              multipleOf: 50,
              default: 800,
            },
          },
        },
        defaultParameters: { targetTokens: 800 },
      },
    ],
    uploadLimits: {
      allowedExtensions: [".pdf", ".docx", ".pptx"],
      maxFileBytes: 52428800,
      maxFilesPerBatch: 100,
    },
  };
}

function userRef(userId: string, displayName: string) {
  return { userId, displayName };
}

export function adminProject(
  summary: ProjectSummary,
  serial: number,
  overrides: Partial<AdminProject> = {},
): AdminProject {
  return {
    projectId: summary.projectId,
    name: summary.name,
    description: summary.description,
    exampleQuestions: [...summary.exampleQuestions],
    instructions: "",
    isDefault: false,
    status: "active",
    memberCount: 1,
    indexCount: summary.hasDefaultIndex ? 1 : 0,
    defaultIndex: summary.hasDefaultIndex
      ? { indexId: IDS.handbookIndex, displayName: "Handbook index" }
      : null,
    rowVersion: rowVersion(serial),
    createdAt: isoAt(-864000),
    createdBy: userRef(IDS.admin, "Alex Admin"),
    updatedAt: isoAt(-86400),
    updatedBy: userRef(IDS.admin, "Alex Admin"),
    archivedAt: null,
    archivedBy: null,
    ...overrides,
  };
}

export function member(userId: string, displayName: string, email: string): ProjectMember {
  return {
    userId,
    displayName,
    email,
    grantSource: "admin",
    grantedAt: isoAt(-86400),
    grantedBy: userRef(IDS.admin, "Alex Admin"),
    lastSeenAt: isoAt(-3600),
  };
}

export function userSummary(userId: string, displayName: string, email: string): UserSummary {
  return {
    userId,
    displayName,
    email,
    firstSeenAt: isoAt(-864000),
    lastSeenAt: isoAt(-3600),
    lastKnownRole: "User",
    isMember: null,
  };
}

export function mapping(
  indexId: string,
  displayName: string,
  isDefault: boolean,
): ProjectIndexMapping {
  return {
    indexId,
    displayName,
    indexStatus: "ready",
    isDefault,
    mappedAt: isoAt(-86400),
    mappedBy: userRef(IDS.admin, "Alex Admin"),
    updatedAt: isoAt(-86400),
  };
}

export function indexDetail(
  indexId: string,
  displayName: string,
  serial: number,
  overrides: Partial<IndexDetail> = {},
): IndexDetail {
  return {
    indexId,
    displayName,
    description: null,
    status: "ready",
    reingestRequired: false,
    method: {
      key: "recursive",
      version: 1,
      displayName: "Recursive",
      parameters: { targetTokens: 800 },
    },
    embedding: { model: "text-embedding-3-large", dimensions: 1024 },
    languageAnalyzer: "standard.lucene",
    documentCounts: { live: 1, ingested: 1, inProgress: 0, failed: 0 },
    liveDocumentCount: 1,
    mappedProjects: [{ projectId: IDS.handbook, name: "Employee handbook" }],
    openRun: null,
    rebuildProgress: null,
    activePhysicalIndex: {
      generation: 1,
      state: "active",
      embeddingModel: "text-embedding-3-large",
      embeddingDimensions: 1024,
      languageAnalyzer: "standard.lucene",
      schemaVersion: 1,
      createdAt: isoAt(-864000),
      activatedAt: isoAt(-863000),
    },
    buildingPhysicalIndex: null,
    rowVersion: rowVersion(serial),
    createdAt: isoAt(-864000),
    createdBy: IDS.admin,
    updatedAt: isoAt(-86400),
    updatedBy: IDS.admin,
    ...overrides,
  };
}

export function versionSummary(overrides: Partial<VersionSummary> = {}): VersionSummary {
  return {
    versionId: IDS.version,
    versionNumber: 1,
    state: "ingested",
    contentType: CONTENT_TYPES.pdf,
    sizeBytes: 524288,
    sha256: "a".repeat(64),
    scanStatus: "clean",
    reservedAt: isoAt(-86400),
    committedAt: isoAt(-86390),
    ingestedAt: isoAt(-86000),
    uploadedBy: IDS.admin,
    uploadedByName: "Alex Admin",
    uploadedViaProject: { projectId: IDS.handbook, name: "Employee handbook" },
    documentPdfStatus: "built",
    failureCode: null,
    failureMessage: null,
    ...overrides,
  };
}

export function documentDetail(
  name: string,
  serial: number,
  overrides: Partial<DocumentDetail> = {},
): DocumentDetail {
  const version = versionSummary();
  return {
    documentId: IDS.document,
    name,
    contentType: CONTENT_TYPES.pdf,
    displayState: "ingested",
    currentVersion: version,
    latestVersion: version,
    versions: [version],
    recentItems: [],
    rowVersion: rowVersion(serial),
    updatedAt: isoAt(-86000),
    ...overrides,
  };
}

export function runDetail(): IngestionRunDetail {
  return {
    run: {
      runId: IDS.run,
      kind: "upload",
      state: "completed",
      priority: "interactive",
      index: { indexId: IDS.handbookIndex, displayName: "Handbook index" },
      requestedBy: IDS.admin,
      requestedByName: "Alex Admin",
      counts: {
        total: 1,
        queued: 0,
        running: 0,
        succeeded: 1,
        skipped: 0,
        failed: 0,
        cancelled: 0,
      },
      createdAt: isoAt(-86400),
      startedAt: isoAt(-86390),
      completedAt: isoAt(-86000),
    },
    items: [],
    nextItemsCursor: null,
  };
}

export function defaultAdmin(projects: readonly ProjectSummary[]): AdminData {
  const adminProjects = projects.map((summary, index) => adminProject(summary, 2001 + index));
  return {
    projects: adminProjects,
    members: Object.fromEntries(
      adminProjects.map((project) => [
        project.projectId,
        [member(IDS.user, USER_NAME, USER_EMAIL)],
      ]),
    ),
    mappings: Object.fromEntries(
      projects.map((project) => [
        project.projectId,
        project.hasDefaultIndex ? [mapping(IDS.handbookIndex, "Handbook index", true)] : [],
      ]),
    ),
    users: [
      userSummary(IDS.user, USER_NAME, USER_EMAIL),
      userSummary("8c3d4e5f-6a7b-4c8d-8e9f-a0b1c2d3e4f5", "Sam Lee", "sam.lee@contoso.example"),
      userSummary("9d4e5f6a-7b8c-4d9e-9fa0-b1c2d3e4f5a6", "Kim Ito", "kim.ito@contoso.example"),
      userSummary("ae5f6a7b-8c9d-4eaf-a0b1-c2d3e4f5a6b7", "Lee Moss", "lee.moss@contoso.example"),
    ],
    indexes: [
      indexDetail(IDS.handbookIndex, "Handbook index", 3001),
      indexDetail(IDS.secondIndex, "Archive index", 3002, { mappedProjects: [] }),
    ],
    documents: { [IDS.handbookIndex]: [documentDetail("Leave policy.pdf", 4001)] },
    runs: [runDetail()],
    indexOptions: defaultIndexOptions(),
    preflight: {},
  };
}
