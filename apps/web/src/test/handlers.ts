// MSW handlers and fixtures shared by the tests: the API origin of the test configuration, the
// signed-in user, reachable projects, and builders for JSON and problem answers.

import { HttpResponse, http } from "msw";

import type { AppConfig } from "../bootstrap/loadConfig";

export const API_BASE = "https://api.docchat.test";

export const TEST_CONFIG: AppConfig = {
  tenantId: "11111111-2222-4333-8444-555555555555",
  clientId: "66666666-7777-4888-9999-aaaaaaaaaaaa",
  apiBaseUrl: API_BASE,
  apiScope: "api://11111111-2222-4333-8444-555555555555/docchat-dev-abc-api/access_as_user",
};

/** Canonical UUIDs that pass the generated `z.uuid()` checks. */
export const IDS = {
  user: "0b5d6c1e-2f3a-4b4c-8d5e-6f708192a3b4",
  admin: "1c6e7d2f-3a4b-4c5d-9e6f-708192a3b4c5",
  projectA: "2d7f8e3a-4b5c-4d6e-af70-8192a3b4c5d6",
  projectB: "3e8a9f4b-5c6d-4e7f-b081-92a3b4c5d6e7",
  session: "4f9bae5c-6d7e-4f80-8192-a3b4c5d6e7f8",
  turn: "5aacbf6d-7e8f-4091-92a3-b4c5d6e7f809",
  clientTurn: "6bbdc07e-8f90-41a2-a3b4-c5d6e7f8091a",
} as const;

export const CORRELATION_ID = "0123456789abcdef0123456789abcdef";

export const CURRENT_USER = {
  userId: IDS.user,
  displayName: "Ada Lovelace",
  email: "ada@example.test",
  roles: ["User"],
  isAdmin: false,
  features: { memoryEnabled: true, feedbackComments: true },
} as const;

export const ADMIN_USER = {
  ...CURRENT_USER,
  userId: IDS.admin,
  displayName: "Grace Hopper",
  email: "grace@example.test",
  roles: ["Admin", "User"],
  isAdmin: true,
} as const;

export const PROJECTS = [
  {
    projectId: IDS.projectA,
    name: "Alpha",
    description: "The alpha project.",
    exampleQuestions: ["What is alpha?"],
    hasDefaultIndex: true,
  },
] as const;

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

export interface ProblemFields {
  readonly title?: string;
  readonly detail?: string;
  readonly retryAfterSeconds?: number;
  readonly errors?: readonly { readonly path: string; readonly reason: string }[];
  readonly correlationId?: string;
}

/** A problem details answer as the API sends it (`11-api.md` section 1.5). */
export function problemBody(status: number, code: string, fields: ProblemFields = {}) {
  return {
    type: `urn:docchat:problem:${code}`,
    title: fields.title ?? "Request refused",
    status,
    detail: fields.detail ?? `The request was refused with ${code}.`,
    code,
    correlationId: fields.correlationId ?? CORRELATION_ID,
    ...(fields.retryAfterSeconds === undefined
      ? {}
      : { retryAfterSeconds: fields.retryAfterSeconds }),
    ...(fields.errors === undefined ? {} : { errors: fields.errors }),
  };
}

export function problemResponse(
  status: number,
  code: string,
  fields: ProblemFields = {},
): Response {
  return HttpResponse.json(problemBody(status, code, fields), {
    status,
    headers: { "Content-Type": "application/problem+json" },
  });
}

/** The handlers every test starts with: the signed-in user, one ready project and no sessions. */
export const defaultHandlers = [
  http.get(apiUrl("/v1/me"), () => HttpResponse.json(CURRENT_USER)),
  http.get(apiUrl("/v1/projects"), () => HttpResponse.json({ items: PROJECTS, nextCursor: null })),
  http.get(apiUrl("/v1/sessions"), () => HttpResponse.json({ items: [], nextCursor: null })),
];
