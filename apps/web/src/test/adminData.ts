// Fixtures for the memory, project, index, document and run screens' tests: answers shaped like the API's
// (`11-api.md` section 4), each passing its generated schema, with overrides per test. The method
// catalogue holds the two shipped methods and their parameter schemas (`14-ingestion.md` section
// 9.3).

import type * as T from "../api/generated/types.gen";

/** A canonical UUID (version 4, variant 8) numbered `n`. */
export function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

/** A `rowVersion`: 11 base64 characters and `=`. */
export function rowVersion(n: number): string {
  return `AAAAAAAA${String(n).padStart(3, "0")}=`;
}

export const TIME = "2026-10-21T09:15:02.123Z";
export const LATER = "2026-10-22T10:00:00.000Z";

export const ADMIN_REF = { userId: uuid(900), displayName: "Grace Hopper" } as const;

export function adminProject(overrides: Partial<T.AdminProject> = {}): T.AdminProject {
  return {
    projectId: uuid(1),
    name: "Field notes",
    description: "Shared notes for the field team",
    instructions: "Explain acronyms.",
    exampleQuestions: ["What changed in the policy?"],
    isDefault: false,
    status: "active",
    memberCount: 2,
    indexCount: 1,
    defaultIndex: { indexId: uuid(500), displayName: "Policy library" },
    createdAt: TIME,
    createdBy: ADMIN_REF,
    updatedAt: TIME,
    updatedBy: ADMIN_REF,
    archivedAt: null,
    archivedBy: null,
    rowVersion: rowVersion(1),
    ...overrides,
  };
}

export function projectSummary(
  overrides: Partial<T.AdminProjectSummary> = {},
): T.AdminProjectSummary {
  return {
    projectId: uuid(1),
    name: "Field notes",
    description: "Shared notes for the field team",
    isDefault: false,
    status: "active",
    memberCount: 2,
    indexCount: 1,
    defaultIndex: { indexId: uuid(500), displayName: "Policy library" },
    updatedAt: TIME,
    rowVersion: rowVersion(1),
    ...overrides,
  };
}

export function userSummary(n: number, overrides: Partial<T.UserSummary> = {}): T.UserSummary {
  return {
    userId: uuid(100 + n),
    displayName: `Person ${String(n)}`,
    email: `person${String(n)}@example.test`,
    lastKnownRole: "User",
    firstSeenAt: TIME,
    lastSeenAt: TIME,
    isMember: null,
    ...overrides,
  };
}

export function member(n: number, overrides: Partial<T.ProjectMember> = {}): T.ProjectMember {
  return {
    userId: uuid(100 + n),
    displayName: `Person ${String(n)}`,
    email: `person${String(n)}@example.test`,
    grantSource: "admin",
    grantedAt: TIME,
    grantedBy: ADMIN_REF,
    lastSeenAt: TIME,
    ...overrides,
  };
}

export function mapping(
  n: number,
  overrides: Partial<T.ProjectIndexMapping> = {},
): T.ProjectIndexMapping {
  return {
    indexId: uuid(500 + n),
    displayName: `Index ${String(n)}`,
    indexStatus: "ready",
    isDefault: false,
    mappedAt: TIME,
    mappedBy: ADMIN_REF,
    updatedAt: TIME,
    ...overrides,
  };
}

export function page<I>(items: readonly I[], nextCursor: string | null = null) {
  return { items, nextCursor };
}

export const PAGEWISE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["summarizeTables", "ocrLanguages"],
  properties: {
    summarizeTables: {
      type: "boolean",
      title: "Summarise tables",
      description: "Add a short written summary before each table so it can be found by meaning.",
      default: true,
    },
    ocrLanguages: {
      type: "array",
      title: "Languages in scanned pages",
      description: "Used to read text from scanned pages. Choose up to three.",
      minItems: 1,
      maxItems: 3,
      uniqueItems: true,
      default: ["eng"],
      items: {
        type: "string",
        oneOf: [
          { const: "eng", title: "English" },
          { const: "fra", title: "French" },
          { const: "deu", title: "German" },
          { const: "spa", title: "Spanish" },
          { const: "ita", title: "Italian" },
          { const: "nld", title: "Dutch" },
          { const: "por", title: "Portuguese" },
        ],
      },
    },
  },
} as const;

export const RECURSIVE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["targetTokens", "overlapPercent", "tableMaxTokens"],
  properties: {
    targetTokens: {
      type: "integer",
      title: "Passage size (tokens)",
      minimum: 128,
      maximum: 2048,
      default: 512,
    },
    overlapPercent: {
      type: "integer",
      title: "Overlap between passages (%)",
      description:
        "Share of each passage repeated at the start of the next one in the same section.",
      minimum: 0,
      maximum: 50,
      multipleOf: 5,
      default: 25,
    },
    tableMaxTokens: {
      type: "integer",
      title: "Largest table kept whole (tokens)",
      description: "Longer tables are split between rows, repeating the header rows.",
      minimum: 256,
      maximum: 6000,
      default: 1600,
    },
  },
} as const;

const CONTENT_TYPES: T.MethodOption["supportedContentTypes"] = [
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
];

export function methodOption(overrides: Partial<T.MethodOption> = {}): T.MethodOption {
  return {
    key: "recursive",
    version: 1,
    displayName: "By headings and paragraphs",
    description:
      "Splits by headings, then paragraphs and sentences, into passages of a target size. Uses Azure AI Document Intelligence.",
    parameterSchema: RECURSIVE_SCHEMA,
    defaultParameters: { targetTokens: 512, overlapPercent: 25, tableMaxTokens: 1600 },
    supportedContentTypes: CONTENT_TYPES,
    ...overrides,
  };
}

export const PAGEWISE_METHOD = methodOption({
  key: "pagewise",
  displayName: "Page by page",
  description:
    "One passage per page, with tables summarised and kept as structured tables. Uses the built-in parser.",
  parameterSchema: PAGEWISE_SCHEMA,
  defaultParameters: { summarizeTables: true, ocrLanguages: ["eng"] },
});

const ANALYZERS: [string, string][] = [
  ["standard.lucene", "Any language"],
  ["en.microsoft", "English"],
  ["en.lucene", "English, Lucene"],
  ["fr.microsoft", "French"],
  ["fr.lucene", "French, Lucene"],
  ["de.microsoft", "German"],
  ["de.lucene", "German, Lucene"],
  ["es.microsoft", "Spanish"],
  ["es.lucene", "Spanish, Lucene"],
  ["it.microsoft", "Italian"],
  ["it.lucene", "Italian, Lucene"],
  ["nl.microsoft", "Dutch"],
  ["nl.lucene", "Dutch, Lucene"],
  ["pt-Pt.microsoft", "Portuguese, Portugal"],
  ["pt-Br.microsoft", "Portuguese, Brazil"],
  ["ja.microsoft", "Japanese"],
  ["ko.microsoft", "Korean"],
  ["zh-Hans.microsoft", "Chinese, Simplified"],
  ["ar.microsoft", "Arabic"],
  ["hi.microsoft", "Hindi"],
];

export function indexOptions(overrides: Partial<T.IndexOptions> = {}): T.IndexOptions {
  return {
    methods: [PAGEWISE_METHOD, methodOption()],
    embeddingModels: [
      {
        model: "text-embedding-3-large",
        modelVersion: "1",
        dimensions: [256, 1024, 3072],
        defaultDimensions: 1024,
      },
      {
        model: "text-embedding-3-small",
        modelVersion: "1",
        dimensions: [256],
        defaultDimensions: 256,
      },
    ],
    languageAnalyzers: ANALYZERS.map(([value, label]) => ({ value, label })) as never,
    defaults: {
      methodKey: "recursive",
      methodVersion: 1,
      embeddingModel: "text-embedding-3-large",
      embeddingDimensions: 1024,
      languageAnalyzer: "standard.lucene",
    },
    uploadLimits: {
      maxFileBytes: 52_428_800,
      maxFilesPerBatch: 100,
      allowedExtensions: [".pdf", ".docx", ".pptx"],
    },
    ...overrides,
  };
}

export function indexSummary(n: number, overrides: Partial<T.IndexSummary> = {}): T.IndexSummary {
  return {
    indexId: uuid(500 + n),
    displayName: `Index ${String(n)}`,
    status: "ready",
    reingestRequired: false,
    method: { key: "recursive", version: 1, displayName: "By headings and paragraphs" },
    embedding: { model: "text-embedding-3-large", dimensions: 1024 },
    languageAnalyzer: "standard.lucene",
    liveDocumentCount: 12,
    updatedAt: TIME,
    rowVersion: rowVersion(50 + n),
    ...overrides,
  };
}

export function indexDetail(overrides: Partial<T.IndexDetail> = {}): T.IndexDetail {
  return {
    indexId: uuid(500),
    displayName: "Policy library",
    description: "Shared policy and procedure documents",
    status: "ready",
    reingestRequired: false,
    method: {
      key: "recursive",
      version: 1,
      displayName: "By headings and paragraphs",
      parameters: { targetTokens: 512, overlapPercent: 25, tableMaxTokens: 1600 },
    },
    embedding: { model: "text-embedding-3-large", dimensions: 1024 },
    languageAnalyzer: "standard.lucene",
    liveDocumentCount: 12,
    updatedAt: TIME,
    rowVersion: rowVersion(50),
    activePhysicalIndex: null,
    buildingPhysicalIndex: null,
    documentCounts: { live: 12, ingested: 10, inProgress: 1, failed: 1 },
    rebuildProgress: null,
    mappedProjects: [],
    openRun: null,
    createdAt: TIME,
    createdBy: ADMIN_REF.userId,
    updatedBy: ADMIN_REF.userId,
    ...overrides,
  };
}

export function fact(n: number, overrides: Partial<T.FactView> = {}): T.FactView {
  return {
    factId: uuid(700 + n),
    text: `Fact number ${String(n)}`,
    category: "topic",
    source: { kind: "extracted", sessionId: uuid(800) },
    createdAt: TIME,
    updatedAt: TIME,
    ...overrides,
  };
}

/** A memory ETag: Cosmos DB's `_etag`, quotes included. */
export function etag(n: number): string {
  return `"etag-${String(n)}"`;
}

export function memoryView(overrides: Partial<T.MemoryView> = {}): T.MemoryView {
  const facts = overrides.facts ?? [fact(1), fact(2, { category: "role" })];
  return {
    enabled: true,
    paused: false,
    facts,
    limits: {
      maxFacts: 50,
      maxFactChars: 200,
      maxTokens: 1000,
      usedFacts: facts.length,
      usedTokens: 120,
    },
    etag: etag(1),
    updatedAt: TIME,
    ...overrides,
  };
}

/** A version's SHA-256: 64 lower-case hexadecimal characters, numbered `n`. */
export function sha(n: number): string {
  return String(n).padStart(64, "0");
}

export function versionSummary(
  n: number,
  overrides: Partial<T.VersionSummary> = {},
): T.VersionSummary {
  return {
    versionId: uuid(300 + n),
    versionNumber: n,
    state: "ingested",
    sizeBytes: 482_113,
    sha256: sha(n),
    contentType: "application/pdf",
    scanStatus: "clean",
    failureCode: null,
    failureMessage: null,
    uploadedBy: ADMIN_REF.userId,
    uploadedByName: ADMIN_REF.displayName,
    uploadedViaProject: { projectId: uuid(1), name: "Field notes" },
    reservedAt: TIME,
    committedAt: TIME,
    ingestedAt: TIME,
    documentPdfStatus: "built",
    ...overrides,
  };
}

export function documentSummary(
  n: number,
  overrides: Partial<T.DocumentSummary> = {},
): T.DocumentSummary {
  const version = versionSummary(n);
  return {
    documentId: uuid(200 + n),
    name: `document-${String(n)}.pdf`,
    contentType: "application/pdf",
    displayState: "ingested",
    currentVersion: version,
    latestVersion: version,
    updatedAt: TIME,
    rowVersion: rowVersion(200 + n),
    ...overrides,
  };
}

export function documentDetail(
  n: number,
  overrides: Partial<T.DocumentDetail> = {},
): T.DocumentDetail {
  const summary = documentSummary(n);
  return {
    ...summary,
    versions: summary.currentVersion === null ? [] : [summary.currentVersion],
    recentItems: [],
    ...overrides,
  };
}

export function runSummary(n: number, overrides: Partial<T.RunSummary> = {}): T.RunSummary {
  return {
    runId: uuid(400 + n),
    kind: "upload",
    state: "completed",
    index: { indexId: uuid(500), displayName: "Policy library" },
    priority: "interactive",
    requestedBy: ADMIN_REF.userId,
    requestedByName: ADMIN_REF.displayName,
    createdAt: TIME,
    startedAt: TIME,
    completedAt: LATER,
    counts: { total: 3, queued: 0, running: 0, succeeded: 3, skipped: 0, failed: 0, cancelled: 0 },
    ...overrides,
  };
}

export function runItem(n: number, overrides: Partial<T.RunItem> = {}): T.RunItem {
  return {
    itemId: uuid(600 + n),
    operation: "ingest",
    document: { documentId: uuid(200 + n), name: `document-${String(n)}.pdf` },
    versionNumber: 1,
    target: { generation: 1, state: "active" },
    state: "succeeded",
    stage: "flip",
    attempts: 1,
    outcomeCode: null,
    outcomeMessage: null,
    warnings: [],
    usage: {
      embeddingTokens: null,
      summaryInputTokens: null,
      summaryOutputTokens: null,
      docIntelPages: null,
    },
    createdAt: TIME,
    startedAt: TIME,
    completedAt: LATER,
    ...overrides,
  };
}
