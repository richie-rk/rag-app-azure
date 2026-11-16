// What the run screens show for a run (10-web-client.md sections 7.10 to 7.12): its progress, who
// started it, an item's problem, and the announcement when a watched run finishes.

import { useEffect, useRef } from "react";

import { announceStatus } from "../../a11y/announce";
import { displayNames, en } from "../../copy/en";
import { fillText } from "../../ui/format";
import type { RunItem, RunSummary } from "../shared/queries";

type RunState = RunSummary["state"];

/** The states in which a run still changes by itself; the run screens poll while one is shown. */
export function isOpenRun(state: RunState): boolean {
  return state === "queued" || state === "running";
}

/** Done counts the items that reached an end: succeeded, skipped, failed and cancelled. */
export function doneCount(counts: RunSummary["counts"]): number {
  return counts.succeeded + counts.skipped + counts.failed + counts.cancelled;
}

/** `runs.progress`, and `runs.progressFailed` when any item failed (null otherwise). */
export function progressTexts(counts: RunSummary["counts"]): {
  readonly progress: string;
  readonly failed: string | null;
} {
  return {
    progress: fillText(en["runs.progress"], { done: doneCount(counts), total: counts.total }),
    failed:
      counts.failed > 0 ? fillText(en["runs.progressFailed"], { failed: counts.failed }) : null,
  };
}

/** Started by: the requester's name, or `run.system` when the run names none. */
export function startedByText(run: Pick<RunSummary, "requestedByName">): string {
  return run.requestedByName ?? en["run.system"];
}

/** `run.problem` from an item's or a version's safe message and code; whichever is present when
 * one is missing, and nothing when both are. */
export function problemText(message: string | null, code: string | null): string {
  if (message !== null && code !== null) return fillText(en["run.problem"], { message, code });
  return message ?? code ?? "";
}

/** The Document column: the document's name, or the operation's display name for an item of an
 * index operation. */
export function itemName(item: RunItem): string {
  return item.document?.name ?? displayNames.itemOperation[item.operation];
}

/** Section 7.11 rule 5: a watched run that moves from `queued` or `running` to a final state is
 * announced with `runs.announce.finished`. Runs seen for the first time announce nothing. */
export function useRunsFinishedAnnouncement(runs: readonly RunSummary[] | undefined): void {
  const seen = useRef(new Map<string, RunState>());
  useEffect(() => {
    if (runs === undefined) return;
    for (const run of runs) {
      const before = seen.current.get(run.runId);
      if (before !== undefined && isOpenRun(before) && !isOpenRun(run.state)) {
        announceStatus(
          fillText(en["runs.announce.finished"], { state: displayNames.runState[run.state] }),
        );
      }
      seen.current.set(run.runId, run.state);
    }
  }, [runs]);
}
