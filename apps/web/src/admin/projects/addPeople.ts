// The Add people loop (10-web-client.md section 7.6, ADR 0094): one `adminAddProjectMember` call
// per chosen person, one after another, each with the version the previous answer returned.
//
// - `201` (added) and `200` (already a member) both count as added.
// - `429 too_many_requests` or `429 write_rate_limited`: wait `retryAfterSeconds`, then send the same
//   person with the same `If-Match`; at most 3 waits per person, so a fourth `429` fails that person.
//   A refused write was never applied (`11-api.md` section 1.11), so the wait completes the batch.
// - `412`, `404 project_not_found`, `409 project_archived` and `428` stop the loop: a change by
//   another admin, or a project that can't take members, needs a check before anything else.
// - `404 user_not_found` or any other failure fails that person; the loop goes on.
// - Aborting the signal (leaving the tab) ends the loop before its next request.

import { requestWithStatus } from "../../api/http";
import type { ResponseOf } from "../../api/operations";
import { apiErrorOf, isProblem, retryAtOf, type ApiError } from "../../api/problem";
import { BATCH_RATE_LIMIT_WAITS } from "../../platform/constants";
import type { UserSummary } from "../shared/queries";

export type ProjectMemberChange = ResponseOf<"adminAddProjectMember">;

export interface AddPeopleOptions {
  readonly projectId: string;
  readonly people: readonly UserSummary[];
  /** The cached project's `rowVersion`, sent with the first call. */
  readonly rowVersion: string;
  readonly signal: AbortSignal;
  /** A person was added (or already a member); `sent` is the version that call carried. */
  readonly onAdded: (person: UserSummary, change: ProjectMemberChange, sent: string) => void;
  /** The loop waits until this time (epoch ms) on a `429`, or `null` when it sends again. */
  readonly onWaiting: (until: number | null) => void;
  /** For tests: how the loop waits. */
  readonly sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export interface AddPeopleResult {
  readonly added: readonly UserSummary[];
  readonly failed: readonly UserSummary[];
  /** The answer that stopped the loop, or null when it went through every person. */
  readonly stoppedBy: ApiError | null;
  /** The signal was aborted: the tab was left. */
  readonly aborted: boolean;
}

/** Waits `ms`, or less when the signal aborts. */
export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, Math.max(0, ms));
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done);
  });
}

function isRateLimit(error: ApiError): boolean {
  return isProblem(error, 429, "too_many_requests") || isProblem(error, 429, "write_rate_limited");
}

/** The answers that stop the whole loop rather than fail one person. */
export function stopsAddPeople(error: ApiError): boolean {
  return (
    error.status === 412 ||
    error.status === 428 ||
    isProblem(error, 404, "project_not_found") ||
    isProblem(error, 409, "project_archived")
  );
}

export async function addPeople(options: AddPeopleOptions): Promise<AddPeopleResult> {
  const { projectId, people, signal, onAdded, onWaiting } = options;
  const sleep = options.sleep ?? abortableSleep;
  const added: UserSummary[] = [];
  const failed: UserSummary[] = [];
  const result = (stoppedBy: ApiError | null, aborted = false): AddPeopleResult => ({
    added,
    failed,
    stoppedBy,
    aborted,
  });
  let version = options.rowVersion;
  for (const person of people) {
    let waits = 0;
    for (;;) {
      if (signal.aborted) return result(null, true);
      try {
        const answer = await requestWithStatus("adminAddProjectMember", {
          path: { projectId, userId: person.userId },
          ifMatch: version,
          signal,
        });
        onAdded(person, answer.data, version);
        version = answer.data.projectRowVersion;
        added.push(person);
        break;
      } catch (reason) {
        const error = apiErrorOf(reason);
        if (error === null || error.kind === "aborted") {
          return result(null, true);
        }
        if (isRateLimit(error)) {
          const until = retryAtOf(error);
          if (until !== null && waits < BATCH_RATE_LIMIT_WAITS) {
            waits += 1;
            onWaiting(until);
            await sleep(until - Date.now(), signal);
            onWaiting(null);
            continue;
          }
          failed.push(person);
          break;
        }
        if (stopsAddPeople(error)) return result(error);
        failed.push(person);
        break;
      }
    }
  }
  return result(null);
}
