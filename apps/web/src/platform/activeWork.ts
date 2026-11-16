// Work that a sign-out or a page unload would cut short: live chat turns before their terminal
// outcome and running uploads (10-web-client.md sections 4.6 and 8.19). The turn store and the
// upload panel register their work here, so the shell can ask before signing out, abort it all
// on a sign-out in this tab or another, and warn on `beforeunload`, without knowing either.

import { useSyncExternalStore } from "react";

export interface ActiveWork {
  /** Stops the work; called on sign-out. */
  abort(): void;
}

const work = new Set<ActiveWork>();
const listeners = new Set<() => void>();
let version = 0;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/** Registers running work; call the returned function once it ends, whatever its outcome. */
export function registerActiveWork(item: ActiveWork): () => void {
  work.add(item);
  changed();
  return () => {
    if (work.delete(item)) changed();
  };
}

export function hasActiveWork(): boolean {
  return work.size > 0;
}

/** Aborts every registered piece of work, for a sign-out in this tab or another. */
export function abortAllWork(): void {
  for (const item of [...work]) item.abort();
}

export function subscribeActiveWork(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Whether any work is running, as React state. */
export function useHasActiveWork(): boolean {
  useSyncExternalStore(subscribeActiveWork, () => version);
  return work.size > 0;
}

/** Asks the browser to confirm leaving the page while work is running (section 8.19). */
export function installUnloadGuard(target: Window = window): () => void {
  const onBeforeUnload = (event: BeforeUnloadEvent) => {
    if (hasActiveWork()) event.preventDefault();
  };
  target.addEventListener("beforeunload", onBeforeUnload);
  return () => {
    target.removeEventListener("beforeunload", onBeforeUnload);
  };
}
