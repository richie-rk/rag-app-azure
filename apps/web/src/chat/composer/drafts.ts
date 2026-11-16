// Composer drafts (10-web-client.md section 8.5): kept in memory per conversation, keyed by
// `sessionId` or `new:<projectId>`, for the page's lifetime and never in browser storage. A `422`
// from `createChatTurn` puts the question back here with its reason, even when the user has
// moved to another conversation meanwhile.

import { useCallback, useSyncExternalStore } from "react";

const drafts = new Map<string, string>();
const reasons = new Map<string, string>();
const listeners = new Set<() => void>();

function changed(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The draft key of a conversation: its session, or a new chat in a project. */
export function draftKey(sessionId: string | null, projectId: string | null): string {
  return sessionId ?? `new:${projectId ?? ""}`;
}

export function getDraft(key: string): string {
  return drafts.get(key) ?? "";
}

export function setDraft(key: string, value: string): void {
  if (value === "") drafts.delete(key);
  else drafts.set(key, value);
  changed();
}

/** The text of the reason a `422` gave, shown under the composer until the draft changes. */
export function setDraftReason(key: string, reason: string | null): void {
  if (reason === null) reasons.delete(key);
  else reasons.set(key, reason);
  changed();
}

export interface Draft {
  readonly text: string;
  readonly reason: string | null;
  readonly setText: (value: string) => void;
}

export function useDraft(key: string): Draft {
  const text = useSyncExternalStore(subscribe, () => getDraft(key));
  const reason = useSyncExternalStore(subscribe, () => reasons.get(key) ?? null);
  const setText = useCallback(
    (value: string) => {
      reasons.delete(key);
      setDraft(key, value);
    },
    [key],
  );
  return { text, reason, setText };
}

/** For tests: forgets every draft. */
export function resetDraftsForTests(): void {
  drafts.clear();
  reasons.clear();
  changed();
}
