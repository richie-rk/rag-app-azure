// Which project the chat screen has chosen (10-web-client.md section 8.3). On `/chat` it is the
// `?project=` value when listed, else the project of the newest loaded session when listed, else
// the only project; on `/chat/<sessionId>` it is the session's project. A project that isn't
// listed is never chosen.

import type { ProjectSummary } from "../../api/queryClient";

export interface SelectionInput {
  readonly projects: readonly ProjectSummary[];
  /** The open session, or null on `/chat`. */
  readonly sessionId: string | null;
  /** The open session's project: `getSession`'s, or a live turn's before that answers. */
  readonly sessionProjectId: string | null;
  readonly projectParam: string | null;
  /** The project of the newest loaded session. */
  readonly newestSessionProjectId: string | null;
}

export function chooseProject(input: SelectionInput): ProjectSummary | null {
  const find = (projectId: string | null) =>
    projectId === null
      ? undefined
      : input.projects.find((project) => project.projectId === projectId);
  if (input.sessionId !== null) return find(input.sessionProjectId) ?? null;
  return (
    find(input.projectParam) ??
    find(input.newestSessionProjectId) ??
    (input.projects.length === 1 ? (input.projects[0] ?? null) : null)
  );
}

const collator = new Intl.Collator(undefined, { sensitivity: "base" });

/** The picker's order: by name, at base sensitivity. */
export function sortedProjects(projects: readonly ProjectSummary[]): ProjectSummary[] {
  return [...projects].sort((a, b) => collator.compare(a.name, b.name));
}
