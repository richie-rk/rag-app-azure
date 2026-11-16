// Every operation of 10-web-client.md section 5.2 against a world's fixture data, with the
// behaviour the client depends on from 11-api.md section 3: paging with `nextCursor`, `If-Match`
// on the 14 versioned writes (`428` without it, `412` on a stale one), the `projectRowVersion` of
// membership and mapping changes, and the admin role on every `/v1/admin` operation.

import type { IncomingMessage, ServerResponse } from "node:http";

import type {
  AdminProject,
  AdminProjectSummary,
  ChatTurnRequest,
  CitationDetail,
  FactCategory,
  FeedbackView,
  IndexDetail,
  MemoryView,
  ProjectCreate,
  ProjectIndexMapping,
  ProjectSummary,
  ProjectUpdate,
  SessionDetail,
  TurnView,
  UploadPreflightRequest,
  UploadReservationRequest,
  UploadSubmitRequest,
  VersionSummary,
} from "../../../../../apps/web/src/api/generated/types.gen.ts";
import { isoAt, mapping, member, rowVersion, uuid, versionSummary } from "./builders.ts";
import { buildPdf } from "./pdf.ts";
import type { OperationId, StoredSession, StoredTurn } from "./protocol.ts";
import { sendJson, sendPdf, sendProblem, sendTruncatedPdf } from "./reply.ts";
import { handleChatTurn } from "./streams.ts";
import type { World } from "./world.ts";

export interface OperationContext {
  readonly world: World;
  readonly request: IncomingMessage;
  readonly response: ServerResponse;
  readonly params: Readonly<Record<string, string>>;
  readonly query: URLSearchParams;
  readonly body: unknown;
  readonly rawBody: Buffer;
  /** Set when a scripted `truncated` answer replaces a file operation's body. */
  readonly truncate: boolean;
}

type Handler = (context: OperationContext) => void | Promise<void>;

interface Route {
  readonly operation: OperationId;
  readonly method: string;
  readonly pattern: RegExp;
  readonly names: readonly string[];
  readonly handler: Handler;
}

/** The operations that send `If-Match` (11-api.md section 1.9). */
export const IF_MATCH_OPERATIONS: ReadonlySet<OperationId> = new Set([
  "adminUpdateProject",
  "adminAddProjectMember",
  "adminRemoveProjectMember",
  "adminMapProjectIndex",
  "adminUnmapProjectIndex",
  "adminUpdateIndex",
  "adminStartReingest",
  "adminDeleteIndex",
  "adminDeleteDocument",
  "adminReingestDocument",
  "updateMemorySettings",
  "updateMemoryFact",
  "deleteMemoryFact",
  "clearMemory",
]);

export const FILE_OPERATIONS: ReadonlySet<OperationId> = new Set([
  "getCitationContent",
  "getCitationDocument",
  "adminGetDocumentPdf",
]);

function encodeCursor(offset: number): string {
  return Buffer.from(String(offset), "utf8").toString("base64url");
}

function decodeCursor(cursor: string | null): number {
  if (cursor === null) return 0;
  const value = Number.parseInt(Buffer.from(cursor, "base64url").toString("utf8"), 10);
  return Number.isNaN(value) ? 0 : value;
}

function page<T>(
  items: readonly T[],
  query: URLSearchParams,
  fallback: number,
  fixed: number | null = null,
): { items: T[]; nextCursor: string | null } {
  const limit = fixed ?? Number.parseInt(query.get("limit") ?? String(fallback), 10);
  const offset = decodeCursor(query.get("cursor"));
  const slice = items.slice(offset, offset + limit);
  return {
    items: slice,
    nextCursor: offset + limit < items.length ? encodeCursor(offset + limit) : null,
  };
}

/** The `If-Match` value as the client sends it: quoted, holding the version. */
function ifMatchOf(context: OperationContext): string | null {
  const header = context.request.headers["if-match"];
  if (typeof header !== "string") return null;
  return header.startsWith('"') && header.endsWith('"') && header.length >= 2
    ? header.slice(1, -1)
    : null;
}

/** Answers `428` or `412` and returns false when the request's version isn't `current`. */
function versionHolds(context: OperationContext, current: string): boolean {
  const sent = ifMatchOf(context);
  const { request, response } = context;
  if (sent === null) {
    sendProblem(request, response, "precondition_required");
    return false;
  }
  // A memory ETag carries its own quotes, which the client sends verbatim.
  const bare = current.startsWith('"') && current.endsWith('"') ? current.slice(1, -1) : current;
  if (sent !== bare) {
    sendProblem(request, response, "precondition_failed");
    return false;
  }
  return true;
}

// --- chat, sessions, feedback and citations -------------------------------------------------

function view(turn: StoredTurn): TurnView {
  const { files, ...rest } = turn;
  return rest;
}

function findSession(world: World, sessionId: string | undefined): StoredSession | null {
  return world.sessions.find((item) => item.sessionId === sessionId) ?? null;
}

function sessionDetail(session: StoredSession, turnsLimit: number): SessionDetail {
  const turns = session.turns.slice(-turnsLimit);
  const earlier = session.turns.length > turns.length;
  return {
    sessionId: session.sessionId,
    projectId: session.projectId,
    title: session.title,
    createdAt: session.createdAt,
    lastTurnAt: session.lastTurnAt,
    turnCount: session.turns.length,
    turns: turns.map(view),
    earlierTurnsCursor: earlier ? encodeCursor(session.turns.length - turns.length) : null,
  };
}

function citationOf(context: OperationContext) {
  const session = findSession(context.world, context.params["sessionId"]);
  const turn = session?.turns.find((item) => item.turnId === context.params["turnId"]);
  const number = Number.parseInt(context.params["citationNumber"] ?? "", 10);
  const found = turn?.citations.find((item) => item.number === number);
  if (turn === undefined || found === undefined) {
    sendProblem(context.request, context.response, "citation_not_found");
    return null;
  }
  const file = turn.files?.[String(number)] ?? {
    fileName: `${found.title}, page ${String(found.pageNumber)}.pdf`,
    documentFileName: `${found.title}.pdf`,
    documentPdfAvailable: true,
  };
  const detail: CitationDetail = { ...found, ...file };
  return detail;
}

function sendFile(context: OperationContext, pages: readonly string[], fileName: string): void {
  const bytes = buildPdf(pages);
  if (context.truncate) sendTruncatedPdf(context.request, context.response, bytes);
  else sendPdf(context.request, context.response, bytes, fileName);
}

// --- memory ------------------------------------------------------------------------------------

function nextMemory(world: World, change: Partial<MemoryView>): MemoryView {
  const facts = change.facts ?? world.memory.facts;
  const tokens = facts.reduce((sum, fact) => sum + Math.ceil(fact.text.length / 4), 0);
  const serial = Number.parseInt(world.memory.etag.replace(/"/g, ""), 10) || 1;
  world.memory = {
    ...world.memory,
    ...change,
    facts,
    etag: `"${String(serial + 1)}"`,
    updatedAt: isoAt(),
    limits: { ...world.memory.limits, usedFacts: facts.length, usedTokens: tokens },
  };
  return world.memory;
}

// --- admin ---------------------------------------------------------------------------------------

function adminSummary(project: AdminProject): AdminProjectSummary {
  return {
    projectId: project.projectId,
    name: project.name,
    description: project.description,
    isDefault: project.isDefault,
    status: project.status,
    memberCount: project.memberCount,
    indexCount: project.indexCount,
    defaultIndex: project.defaultIndex,
    rowVersion: project.rowVersion,
    updatedAt: project.updatedAt,
  };
}

function findProject(context: OperationContext): AdminProject | null {
  const project = context.world.admin.projects.find(
    (item) => item.projectId === context.params["projectId"],
  );
  if (project === undefined) {
    sendProblem(context.request, context.response, "project_not_found");
    return null;
  }
  return project;
}

function saveProject(world: World, project: AdminProject): AdminProject {
  world.admin = {
    ...world.admin,
    projects: world.admin.projects.map((item) =>
      item.projectId === project.projectId ? project : item,
    ),
  };
  // The chat side reads the same project.
  world.projects = world.projects.map((item) =>
    item.projectId === project.projectId
      ? {
          ...item,
          name: project.name,
          description: project.description,
          exampleQuestions: project.exampleQuestions,
          hasDefaultIndex: project.defaultIndex !== null,
        }
      : item,
  );
  return project;
}

/** The project with a new `rowVersion`, as every membership and mapping change leaves it. */
function bumpProject(world: World, project: AdminProject, change: Partial<AdminProject> = {}) {
  return saveProject(world, {
    ...project,
    ...change,
    rowVersion: rowVersion(world.nextSerial()),
    updatedAt: isoAt(),
  });
}

function findIndex(context: OperationContext): IndexDetail | null {
  const index = context.world.admin.indexes.find(
    (item) => item.indexId === context.params["indexId"],
  );
  if (index === undefined) {
    sendProblem(context.request, context.response, "index_not_found");
    return null;
  }
  return index;
}

function saveIndex(world: World, index: IndexDetail): IndexDetail {
  world.admin = {
    ...world.admin,
    indexes: world.admin.indexes.map((item) => (item.indexId === index.indexId ? index : item)),
  };
  return index;
}

function indexSummary(index: IndexDetail) {
  return {
    indexId: index.indexId,
    displayName: index.displayName,
    method: {
      key: index.method.key,
      version: index.method.version,
      displayName: index.method.displayName,
    },
    embedding: index.embedding,
    languageAnalyzer: index.languageAnalyzer,
    liveDocumentCount: index.liveDocumentCount,
    reingestRequired: index.reingestRequired,
    rowVersion: index.rowVersion,
    status: index.status,
    updatedAt: index.updatedAt,
  };
}

function documentsOf(world: World, indexId: string | undefined) {
  return world.admin.documents[indexId ?? ""] ?? [];
}

function findDocument(context: OperationContext) {
  const document = documentsOf(context.world, context.params["indexId"]).find(
    (item) => item.documentId === context.params["documentId"],
  );
  if (document === undefined) {
    sendProblem(context.request, context.response, "document_not_found");
    return null;
  }
  return document;
}

const uploadedVersions = new Map<string, { name: string; sizeBytes: number; sha256: string }>();

// --- the table -----------------------------------------------------------------------------------

const handlers: Record<OperationId, Handler> = {
  getCurrentUser: ({ world, request, response }) => {
    sendJson(request, response, 200, world.user);
  },
  listMyProjects: ({ world, request, response, query }) => {
    const sorted: ProjectSummary[] = [...world.projects].sort((a, b) =>
      a.name.localeCompare(b.name),
    );
    sendJson(request, response, 200, page(sorted, query, 100));
  },
  createChatTurn: (context) =>
    handleChatTurn(
      context.world,
      context.request,
      context.response,
      context.body as ChatTurnRequest,
    ),
  listSessions: ({ world, request, response, query }) => {
    const summaries = world.sessions.map((item) => ({
      sessionId: item.sessionId,
      projectId: item.projectId,
      title: item.title,
      createdAt: item.createdAt,
      lastTurnAt: item.lastTurnAt,
      turnCount: Math.max(item.turns.length, 1),
    }));
    sendJson(request, response, 200, page(summaries, query, 30, world.sessionsPageSize));
  },
  getSession: (context) => {
    const session = findSession(context.world, context.params["sessionId"]);
    if (session === null) {
      sendProblem(context.request, context.response, "session_not_found");
      return;
    }
    const limit = Number.parseInt(context.query.get("turnsLimit") ?? "30", 10);
    sendJson(context.request, context.response, 200, sessionDetail(session, limit));
  },
  listSessionTurns: (context) => {
    const session = findSession(context.world, context.params["sessionId"]);
    if (session === null) {
      sendProblem(context.request, context.response, "session_not_found");
      return;
    }
    // The cursor holds how many turns are older than the page before it.
    const before = decodeCursor(context.query.get("cursor")) || session.turns.length;
    const limit = Number.parseInt(context.query.get("limit") ?? "30", 10);
    const start = Math.max(0, before - limit);
    sendJson(context.request, context.response, 200, {
      items: session.turns.slice(start, before).map(view),
      nextCursor: start > 0 ? encodeCursor(start) : null,
    });
  },
  putTurnFeedback: (context) => {
    const { world, request, response, params } = context;
    const session = findSession(world, params["sessionId"]);
    const turn = session?.turns.find((item) => item.turnId === params["turnId"]);
    if (session === null || turn === undefined) {
      sendProblem(request, response, "turn_not_found");
      return;
    }
    if (!(turn.status === "completed" || (turn.status === "cancelled" && turn.answer !== ""))) {
      sendProblem(request, response, "feedback_not_allowed");
      return;
    }
    const body = context.body as {
      rating: "up" | "down";
      category?: string | null;
      comment?: string | null;
    };
    const comment = typeof body.comment === "string" ? body.comment.trim() : "";
    const stored: FeedbackView = {
      rating: body.rating,
      category: (body.category ?? null) as FeedbackView["category"],
      comment: comment === "" ? null : comment,
      updatedAt: isoAt(),
    };
    world.sessions = world.sessions.map((item) =>
      item.sessionId !== session.sessionId
        ? item
        : {
            ...item,
            turns: item.turns.map((current) =>
              current.turnId === turn.turnId ? { ...current, feedback: stored } : current,
            ),
          },
    );
    sendJson(request, response, 200, stored);
  },
  getCitation: (context) => {
    const detail = citationOf(context);
    if (detail !== null) sendJson(context.request, context.response, 200, detail);
  },
  getCitationContent: (context) => {
    const detail = citationOf(context);
    if (detail === null) return;
    sendFile(context, [`${detail.title}, page ${String(detail.pageNumber)}`], detail.fileName);
  },
  getCitationDocument: (context) => {
    const detail = citationOf(context);
    if (detail === null) return;
    if (!detail.documentPdfAvailable) {
      sendProblem(context.request, context.response, "document_pdf_unavailable");
      return;
    }
    sendFile(
      context,
      [1, 2, 3].map((n) => `${detail.title}, page ${String(n)}`),
      detail.documentFileName,
    );
  },
  getMemory: ({ world, request, response }) => {
    sendJson(request, response, 200, world.memory);
  },
  updateMemorySettings: (context) => {
    if (!versionHolds(context, context.world.memory.etag)) return;
    const { paused } = context.body as { paused: boolean };
    sendJson(context.request, context.response, 200, nextMemory(context.world, { paused }));
  },
  updateMemoryFact: (context) => {
    const { world, request, response, params } = context;
    if (!versionHolds(context, world.memory.etag)) return;
    const fact = world.memory.facts.find((item) => item.factId === params["factId"]);
    if (fact === undefined) {
      sendProblem(request, response, "memory_fact_not_found");
      return;
    }
    const body = context.body as { text: string; category: FactCategory };
    const facts = world.memory.facts.map((item) =>
      item.factId === fact.factId
        ? { ...item, text: body.text, category: body.category, updatedAt: isoAt() }
        : item,
    );
    sendJson(request, response, 200, nextMemory(world, { facts }));
  },
  deleteMemoryFact: (context) => {
    const { world, request, response, params } = context;
    if (!versionHolds(context, world.memory.etag)) return;
    if (!world.memory.facts.some((item) => item.factId === params["factId"])) {
      sendProblem(request, response, "memory_fact_not_found");
      return;
    }
    const facts = world.memory.facts.filter((item) => item.factId !== params["factId"]);
    sendJson(request, response, 200, nextMemory(world, { facts }));
  },
  clearMemory: (context) => {
    if (!versionHolds(context, context.world.memory.etag)) return;
    sendJson(context.request, context.response, 200, nextMemory(context.world, { facts: [] }));
  },
  adminListProjects: ({ world, request, response, query }) => {
    const status = query.get("status") ?? "active";
    const q = (query.get("q") ?? "").toLowerCase();
    const items = world.admin.projects
      .filter((item) => status === "all" || item.status === status)
      .filter((item) => item.name.toLowerCase().includes(q))
      .sort((a, b) => a.name.localeCompare(b.name))
      .map(adminSummary);
    sendJson(request, response, 200, page(items, query, 50));
  },
  adminCreateProject: (context) => {
    const { world, request, response } = context;
    const body = context.body as ProjectCreate;
    if (world.admin.projects.some((item) => item.name === body.name)) {
      sendProblem(request, response, "project_name_taken", {
        errors: [{ path: "body.name", reason: "not_allowed" }],
      });
      return;
    }
    const memberIds = body.initialMemberIds ?? [];
    const unknown = memberIds.findIndex((id) => !world.admin.users.some((u) => u.userId === id));
    if (unknown >= 0) {
      sendProblem(request, response, "validation_failed", {
        errors: [{ path: `body.initialMemberIds.${String(unknown)}`, reason: "not_found" }],
      });
      return;
    }
    const projectId = uuid();
    const by = { userId: world.user.userId, displayName: world.user.displayName };
    const project: AdminProject = {
      projectId,
      name: body.name,
      description: body.description ?? "",
      exampleQuestions: [...(body.exampleQuestions ?? [])],
      instructions: body.instructions ?? "",
      isDefault: body.isDefault ?? false,
      status: "active",
      memberCount: memberIds.length,
      indexCount: 0,
      defaultIndex: null,
      rowVersion: rowVersion(world.nextSerial()),
      createdAt: isoAt(),
      createdBy: by,
      updatedAt: isoAt(),
      updatedBy: by,
      archivedAt: null,
      archivedBy: null,
    };
    world.admin = {
      ...world.admin,
      projects: [...world.admin.projects, project],
      members: {
        ...world.admin.members,
        [projectId]: memberIds.map((id) => {
          const user = world.admin.users.find((u) => u.userId === id);
          return member(id, user?.displayName ?? "", user?.email ?? "");
        }),
      },
      mappings: { ...world.admin.mappings, [projectId]: [] },
    };
    world.projects = [
      ...world.projects,
      {
        projectId,
        name: project.name,
        description: project.description,
        exampleQuestions: project.exampleQuestions,
        hasDefaultIndex: false,
      },
    ];
    sendJson(request, response, 201, project);
  },
  adminGetProject: (context) => {
    const project = findProject(context);
    if (project !== null) sendJson(context.request, context.response, 200, project);
  },
  adminUpdateProject: (context) => {
    const project = findProject(context);
    if (project === null || !versionHolds(context, project.rowVersion)) return;
    const change = context.body as ProjectUpdate;
    const archived = change.status === "archived" && project.status !== "archived";
    const updated = bumpProject(context.world, project, {
      ...change,
      ...(archived
        ? {
            archivedAt: isoAt(),
            archivedBy: {
              userId: context.world.user.userId,
              displayName: context.world.user.displayName,
            },
          }
        : {}),
      ...(change.status === "active" ? { archivedAt: null, archivedBy: null } : {}),
    });
    sendJson(context.request, context.response, 200, updated);
  },
  adminListProjectMembers: (context) => {
    const project = findProject(context);
    if (project === null) return;
    const items = [...(context.world.admin.members[project.projectId] ?? [])].sort((a, b) =>
      a.displayName.localeCompare(b.displayName),
    );
    sendJson(context.request, context.response, 200, page(items, context.query, 100));
  },
  adminAddProjectMember: (context) => {
    const { world, request, response, params } = context;
    const project = findProject(context);
    if (project === null || !versionHolds(context, project.rowVersion)) return;
    const user = world.admin.users.find((item) => item.userId === params["userId"]);
    if (user === undefined) {
      sendProblem(request, response, "user_not_found");
      return;
    }
    const current = world.admin.members[project.projectId] ?? [];
    const existing = current.find((item) => item.userId === user.userId);
    if (existing !== undefined) {
      sendJson(request, response, 200, { member: existing, projectRowVersion: project.rowVersion });
      return;
    }
    const added = member(user.userId, user.displayName, user.email);
    world.admin = {
      ...world.admin,
      members: { ...world.admin.members, [project.projectId]: [...current, added] },
    };
    const updated = bumpProject(world, project, { memberCount: current.length + 1 });
    sendJson(request, response, 201, { member: added, projectRowVersion: updated.rowVersion });
  },
  adminRemoveProjectMember: (context) => {
    const { world, request, response, params } = context;
    const project = findProject(context);
    if (project === null || !versionHolds(context, project.rowVersion)) return;
    const current = world.admin.members[project.projectId] ?? [];
    if (!current.some((item) => item.userId === params["userId"])) {
      sendJson(request, response, 200, { removed: false, projectRowVersion: project.rowVersion });
      return;
    }
    const remaining = current.filter((item) => item.userId !== params["userId"]);
    world.admin = {
      ...world.admin,
      members: { ...world.admin.members, [project.projectId]: remaining },
    };
    const updated = bumpProject(world, project, { memberCount: remaining.length });
    sendJson(request, response, 200, { removed: true, projectRowVersion: updated.rowVersion });
  },
  adminSearchUsers: ({ world, request, response, query }) => {
    const q = (query.get("q") ?? "").toLowerCase();
    const projectId = query.get("projectId");
    const members = projectId === null ? [] : (world.admin.members[projectId] ?? []);
    const items = world.admin.users
      .filter((item) => `${item.displayName} ${item.email}`.toLowerCase().includes(q))
      .sort((a, b) => a.displayName.localeCompare(b.displayName))
      .map((item) => ({
        ...item,
        isMember: projectId === null ? null : members.some((m) => m.userId === item.userId),
      }));
    sendJson(request, response, 200, page(items, query, 20));
  },
  adminListProjectIndexes: (context) => {
    const project = findProject(context);
    if (project === null) return;
    const items = [...(context.world.admin.mappings[project.projectId] ?? [])].sort(
      (a, b) =>
        Number(b.isDefault) - Number(a.isDefault) || a.displayName.localeCompare(b.displayName),
    );
    sendJson(context.request, context.response, 200, page(items, context.query, 50));
  },
  adminMapProjectIndex: (context) => {
    const { world, request, response, params } = context;
    const project = findProject(context);
    if (project === null || !versionHolds(context, project.rowVersion)) return;
    const index = world.admin.indexes.find((item) => item.indexId === params["indexId"]);
    if (index === undefined) {
      sendProblem(request, response, "index_not_found");
      return;
    }
    const { isDefault } = context.body as { isDefault: boolean };
    const current = world.admin.mappings[project.projectId] ?? [];
    const existed = current.some((item) => item.indexId === index.indexId);
    const next: ProjectIndexMapping[] = [
      ...current
        .filter((item) => item.indexId !== index.indexId)
        .map((item) => (isDefault ? { ...item, isDefault: false } : item)),
      { ...mapping(index.indexId, index.displayName, isDefault || current.length === 0) },
    ];
    world.admin = {
      ...world.admin,
      mappings: { ...world.admin.mappings, [project.projectId]: next },
    };
    const chosen = next.find((item) => item.isDefault);
    const updated = bumpProject(world, project, {
      indexCount: next.length,
      defaultIndex:
        chosen === undefined ? null : { indexId: chosen.indexId, displayName: chosen.displayName },
    });
    const created = next.find((item) => item.indexId === index.indexId);
    sendJson(request, response, existed ? 200 : 201, {
      mapping: created,
      projectRowVersion: updated.rowVersion,
    });
  },
  adminUnmapProjectIndex: (context) => {
    const { world, request, response, params } = context;
    const project = findProject(context);
    if (project === null || !versionHolds(context, project.rowVersion)) return;
    const current = world.admin.mappings[project.projectId] ?? [];
    const target = current.find((item) => item.indexId === params["indexId"]);
    if (target === undefined) {
      sendJson(request, response, 200, { removed: false, projectRowVersion: project.rowVersion });
      return;
    }
    if (target.isDefault && current.length > 1) {
      sendProblem(request, response, "default_index_in_use");
      return;
    }
    const remaining = current.filter((item) => item.indexId !== target.indexId);
    world.admin = {
      ...world.admin,
      mappings: { ...world.admin.mappings, [project.projectId]: remaining },
    };
    const updated = bumpProject(world, project, {
      indexCount: remaining.length,
      ...(target.isDefault ? { defaultIndex: null } : {}),
    });
    sendJson(request, response, 200, { removed: true, projectRowVersion: updated.rowVersion });
  },
  adminGetIndexOptions: ({ world, request, response }) => {
    sendJson(request, response, 200, world.admin.indexOptions);
  },
  adminListIndexes: ({ world, request, response, query }) => {
    const items = [...world.admin.indexes]
      .filter((item) => item.status !== "deleted")
      .sort((a, b) => a.displayName.localeCompare(b.displayName))
      .map(indexSummary);
    sendJson(request, response, 200, page(items, query, 50));
  },
  adminCreateIndex: (context) => {
    const { world, request, response } = context;
    const body = context.body as { displayName: string; description?: string | null };
    if (world.admin.indexes.some((item) => item.displayName === body.displayName)) {
      sendProblem(request, response, "display_name_taken", {
        errors: [{ path: "body.displayName", reason: "not_allowed" }],
      });
      return;
    }
    const template = world.admin.indexes[0];
    if (template === undefined) {
      sendProblem(request, response, "internal_error");
      return;
    }
    const index: IndexDetail = {
      ...template,
      indexId: uuid(),
      displayName: body.displayName,
      description: body.description ?? null,
      status: "provisioning",
      mappedProjects: [],
      liveDocumentCount: 0,
      documentCounts: { live: 0, ingested: 0, inProgress: 0, failed: 0 },
      rowVersion: rowVersion(world.nextSerial()),
      createdAt: isoAt(),
      updatedAt: isoAt(),
    };
    world.admin = { ...world.admin, indexes: [...world.admin.indexes, index] };
    sendJson(request, response, 202, { index, runId: uuid() });
  },
  adminGetIndex: (context) => {
    const index = findIndex(context);
    if (index !== null) sendJson(context.request, context.response, 200, index);
  },
  adminUpdateIndex: (context) => {
    const index = findIndex(context);
    if (index === null || !versionHolds(context, index.rowVersion)) return;
    const change = context.body as Partial<IndexDetail>;
    const updated = saveIndex(context.world, {
      ...index,
      ...(change.displayName === undefined ? {} : { displayName: change.displayName }),
      ...(change.description === undefined ? {} : { description: change.description }),
      rowVersion: rowVersion(context.world.nextSerial()),
      updatedAt: isoAt(),
    });
    sendJson(context.request, context.response, 200, updated);
  },
  adminDeleteIndex: (context) => {
    const index = findIndex(context);
    if (index === null || !versionHolds(context, index.rowVersion)) return;
    const updated = saveIndex(context.world, {
      ...index,
      status: "deleting",
      rowVersion: rowVersion(context.world.nextSerial()),
    });
    sendJson(context.request, context.response, 202, { index: updated, runId: uuid() });
  },
  adminStartReingest: (context) => {
    const index = findIndex(context);
    if (index === null || !versionHolds(context, index.rowVersion)) return;
    const updated = saveIndex(context.world, {
      ...index,
      status: "reingesting",
      reingestRequired: false,
      rowVersion: rowVersion(context.world.nextSerial()),
    });
    sendJson(context.request, context.response, 202, {
      index: updated,
      runId: uuid(),
      mode: "same_index",
      itemCount: index.liveDocumentCount,
    });
  },
  adminListDocuments: (context) => {
    const index = findIndex(context);
    if (index === null) return;
    const items = documentsOf(context.world, index.indexId).map((item) => {
      const { versions, recentItems, ...summary } = item;
      return summary;
    });
    sendJson(context.request, context.response, 200, page(items, context.query, 50));
  },
  adminGetDocument: (context) => {
    if (findIndex(context) === null) return;
    const document = findDocument(context);
    if (document !== null) sendJson(context.request, context.response, 200, document);
  },
  adminGetDocumentPdf: (context) => {
    if (findIndex(context) === null) return;
    const document = findDocument(context);
    if (document === null) return;
    if (document.currentVersion?.documentPdfStatus !== "built") {
      sendProblem(context.request, context.response, "document_pdf_unavailable");
      return;
    }
    const stem = document.name.replace(/\.[^.]+$/, "");
    sendFile(context, [`${stem}, page 1`, `${stem}, page 2`], `${stem}.pdf`);
  },
  adminDeleteDocument: (context) => {
    if (findIndex(context) === null) return;
    const document = findDocument(context);
    if (document === null || !versionHolds(context, document.rowVersion)) return;
    const indexId = context.params["indexId"] ?? "";
    context.world.admin = {
      ...context.world.admin,
      documents: {
        ...context.world.admin.documents,
        [indexId]: documentsOf(context.world, indexId).filter(
          (item) => item.documentId !== document.documentId,
        ),
      },
    };
    sendJson(context.request, context.response, 202, { runId: uuid() });
  },
  adminReingestDocument: (context) => {
    if (findIndex(context) === null) return;
    const document = findDocument(context);
    if (document === null || !versionHolds(context, document.rowVersion)) return;
    const { versions, recentItems, ...summary } = document;
    sendJson(context.request, context.response, 202, {
      document: { ...summary, rowVersion: rowVersion(context.world.nextSerial()) },
      runId: uuid(),
      itemCount: 1,
    });
  },
  adminPreflightUploads: (context) => {
    const index = findIndex(context);
    if (index === null) return;
    const { files } = context.body as UploadPreflightRequest;
    const results = files.map((file) => {
      const answer = context.world.admin.preflight[file.name] ?? { status: "new" as const };
      const existing =
        answer.existingDocumentName === undefined
          ? null
          : { documentId: uuid(), name: answer.existingDocumentName };
      const known = answer.status === "unchanged" || answer.status === "update";
      return {
        name: file.name,
        status: answer.status,
        reason: answer.reason ?? null,
        documentId: known || answer.status === "duplicate" ? uuid() : null,
        existingDocument: answer.status === "duplicate" ? existing : null,
        currentVersionNumber: known ? 1 : null,
      };
    });
    sendJson(context.request, context.response, 200, { results });
  },
  adminReserveUpload: (context) => {
    const index = findIndex(context);
    if (index === null) return;
    const body = context.body as UploadReservationRequest;
    const versionId = uuid();
    uploadedVersions.set(versionId, {
      name: body.name,
      sizeBytes: body.sizeBytes,
      sha256: body.sha256,
    });
    sendJson(context.request, context.response, 201, {
      documentId: uuid(),
      versionId,
      versionNumber: body.confirmUpdate === true ? 2 : 1,
      reused: false,
    });
  },
  adminUploadContent: (context) => {
    const index = findIndex(context);
    if (index === null) return;
    const reserved = uploadedVersions.get(context.params["versionId"] ?? "");
    if (reserved === undefined) {
      sendProblem(context.request, context.response, "version_not_found");
      return;
    }
    if (context.rawBody.byteLength !== reserved.sizeBytes) {
      sendProblem(context.request, context.response, "size_mismatch", {
        errors: [{ path: "header.Content-Length", reason: "out_of_range" }],
      });
      return;
    }
    const version: VersionSummary = versionSummary({
      versionId: context.params["versionId"] ?? uuid(),
      state: "pending",
      sizeBytes: Math.max(1, reserved.sizeBytes),
      sha256: reserved.sha256.toLowerCase(),
      ingestedAt: null,
      documentPdfStatus: null,
    });
    sendJson(context.request, context.response, 200, { version });
  },
  adminSubmitUploads: (context) => {
    const index = findIndex(context);
    if (index === null) return;
    const { versionIds } = context.body as UploadSubmitRequest;
    sendJson(context.request, context.response, 202, {
      runId: uuid(),
      results: versionIds.map((versionId) => ({ versionId, outcome: "queued" })),
    });
  },
  adminListIngestionRuns: ({ world, request, response, query }) => {
    const indexId = query.get("indexId");
    const state = query.get("state");
    const items = world.admin.runs
      .map((item) => item.run)
      .filter((run) => indexId === null || run.index.indexId === indexId)
      .filter((run) => state === null || run.state === state);
    sendJson(request, response, 200, page(items, query, 50));
  },
  adminGetIngestionRun: (context) => {
    const run = context.world.admin.runs.find((item) => item.run.runId === context.params["runId"]);
    if (run === undefined) {
      sendProblem(context.request, context.response, "run_not_found");
      return;
    }
    sendJson(context.request, context.response, 200, run);
  },
};

const TEMPLATES: readonly (readonly [OperationId, string, string])[] = [
  ["getCurrentUser", "GET", "/v1/me"],
  ["listMyProjects", "GET", "/v1/projects"],
  ["createChatTurn", "POST", "/v1/chat/turns"],
  ["listSessions", "GET", "/v1/sessions"],
  ["getSession", "GET", "/v1/sessions/{sessionId}"],
  ["listSessionTurns", "GET", "/v1/sessions/{sessionId}/turns"],
  ["putTurnFeedback", "PUT", "/v1/sessions/{sessionId}/turns/{turnId}/feedback"],
  ["getCitation", "GET", "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}"],
  [
    "getCitationContent",
    "GET",
    "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/content",
  ],
  [
    "getCitationDocument",
    "GET",
    "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/document",
  ],
  ["getMemory", "GET", "/v1/memory"],
  ["updateMemorySettings", "PATCH", "/v1/memory"],
  ["updateMemoryFact", "PUT", "/v1/memory/facts/{factId}"],
  ["deleteMemoryFact", "DELETE", "/v1/memory/facts/{factId}"],
  ["clearMemory", "DELETE", "/v1/memory/facts"],
  ["adminListProjects", "GET", "/v1/admin/projects"],
  ["adminCreateProject", "POST", "/v1/admin/projects"],
  ["adminGetProject", "GET", "/v1/admin/projects/{projectId}"],
  ["adminUpdateProject", "PATCH", "/v1/admin/projects/{projectId}"],
  ["adminListProjectMembers", "GET", "/v1/admin/projects/{projectId}/members"],
  ["adminAddProjectMember", "PUT", "/v1/admin/projects/{projectId}/members/{userId}"],
  ["adminRemoveProjectMember", "DELETE", "/v1/admin/projects/{projectId}/members/{userId}"],
  ["adminSearchUsers", "GET", "/v1/admin/users"],
  ["adminListProjectIndexes", "GET", "/v1/admin/projects/{projectId}/indexes"],
  ["adminMapProjectIndex", "PUT", "/v1/admin/projects/{projectId}/indexes/{indexId}"],
  ["adminUnmapProjectIndex", "DELETE", "/v1/admin/projects/{projectId}/indexes/{indexId}"],
  ["adminGetIndexOptions", "GET", "/v1/admin/index-options"],
  ["adminListIndexes", "GET", "/v1/admin/indexes"],
  ["adminCreateIndex", "POST", "/v1/admin/indexes"],
  ["adminGetIndex", "GET", "/v1/admin/indexes/{indexId}"],
  ["adminUpdateIndex", "PATCH", "/v1/admin/indexes/{indexId}"],
  ["adminDeleteIndex", "DELETE", "/v1/admin/indexes/{indexId}"],
  ["adminStartReingest", "POST", "/v1/admin/indexes/{indexId}/reingest"],
  ["adminListDocuments", "GET", "/v1/admin/indexes/{indexId}/documents"],
  ["adminGetDocument", "GET", "/v1/admin/indexes/{indexId}/documents/{documentId}"],
  ["adminGetDocumentPdf", "GET", "/v1/admin/indexes/{indexId}/documents/{documentId}/pdf"],
  ["adminDeleteDocument", "DELETE", "/v1/admin/indexes/{indexId}/documents/{documentId}"],
  ["adminReingestDocument", "POST", "/v1/admin/indexes/{indexId}/documents/{documentId}/reingest"],
  ["adminPreflightUploads", "POST", "/v1/admin/indexes/{indexId}/uploads/preflight"],
  ["adminReserveUpload", "POST", "/v1/admin/indexes/{indexId}/uploads"],
  ["adminUploadContent", "PUT", "/v1/admin/indexes/{indexId}/uploads/{versionId}/content"],
  ["adminSubmitUploads", "POST", "/v1/admin/indexes/{indexId}/uploads/submit"],
  ["adminListIngestionRuns", "GET", "/v1/admin/ingestion-runs"],
  ["adminGetIngestionRun", "GET", "/v1/admin/ingestion-runs/{runId}"],
];

export const ROUTES: readonly Route[] = TEMPLATES.map(([operation, method, template]) => {
  const names: string[] = [];
  const source = template.replace(/\{(\w+)\}/g, (_, name: string) => {
    names.push(name);
    return "([^/]+)";
  });
  return {
    operation,
    method,
    pattern: new RegExp(`^${source}$`),
    names,
    handler: handlers[operation],
  };
});

export function findRoute(
  method: string,
  path: string,
): { route: Route; params: Record<string, string> } | null {
  for (const route of ROUTES) {
    if (route.method !== method) continue;
    const match = route.pattern.exec(path);
    if (match === null) continue;
    const params = Object.fromEntries(
      route.names.map((name, index) => [name, decodeURIComponent(match[index + 1] ?? "")]),
    );
    return { route, params };
  }
  return null;
}
