import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { member, rowVersion, userSummary, uuid } from "../../test/adminData";
import { apiUrl, problemResponse } from "../../test/handlers";
import { configureTestHttp } from "../../test/render";
import { server } from "../../test/server";
import { abortableSleep, addPeople, stopsAddPeople } from "./addPeople";
import { clientError } from "../../api/problem";

const PROJECT = uuid(1);
const PEOPLE = [userSummary(1), userSummary(2), userSummary(3)];

interface Call {
  readonly userId: string;
  readonly ifMatch: string | null;
}

/** Answers each add from `script(call index, call)`; the default adds and moves the version. */
function membersApi(script: (index: number, call: Call) => Response | null = () => null) {
  const calls: Call[] = [];
  server.use(
    http.put(apiUrl("/v1/admin/projects/:projectId/members/:userId"), ({ request, params }) => {
      const call = { userId: String(params["userId"]), ifMatch: request.headers.get("If-Match") };
      calls.push(call);
      const scripted = script(calls.length - 1, call);
      if (scripted !== null) return scripted;
      const n = calls.length + 1;
      return HttpResponse.json(
        { member: member(n, { userId: call.userId }), projectRowVersion: rowVersion(n) },
        { status: 201 },
      );
    }),
  );
  return calls;
}

function run(options: { signal?: AbortSignal; sleep?: (ms: number) => Promise<void> } = {}) {
  const onAdded = vi.fn();
  const onWaiting = vi.fn();
  const promise = addPeople({
    projectId: PROJECT,
    people: PEOPLE,
    rowVersion: rowVersion(1),
    signal: options.signal ?? new AbortController().signal,
    onAdded,
    onWaiting,
    sleep: options.sleep ?? (() => Promise.resolve()),
  });
  return { promise, onAdded, onWaiting };
}

beforeEach(() => {
  configureTestHttp();
});

describe("the Add people loop", () => {
  test("T-398 three people are added one after another, each with the previous projectRowVersion", async () => {
    const calls = membersApi((index) =>
      index === 1
        ? HttpResponse.json(
            { member: member(2), projectRowVersion: rowVersion(2) },
            { status: 200 },
          )
        : null,
    );
    const { promise, onAdded } = run();
    const result = await promise;
    // The second person was already a member: its 200 returns the version unchanged.
    expect(calls.map((call) => call.ifMatch)).toEqual([
      `"${rowVersion(1)}"`,
      `"${rowVersion(2)}"`,
      `"${rowVersion(2)}"`,
    ]);
    expect(calls.map((call) => call.userId)).toEqual(PEOPLE.map((person) => person.userId));
    expect(result).toEqual({ added: PEOPLE, failed: [], stoppedBy: null, aborted: false });
    expect(onAdded.mock.calls.map((call: unknown[]) => call[2])).toEqual([
      rowVersion(1),
      rowVersion(2),
      rowVersion(2),
    ]);
  });

  test("T-398 a 412 on the second person sends no third add and stops the loop", async () => {
    const calls = membersApi((index) =>
      index === 1 ? problemResponse(412, "precondition_failed", { title: "Changed" }) : null,
    );
    const result = await run().promise;
    expect(calls).toHaveLength(2);
    expect(result.added).toEqual([PEOPLE[0]]);
    expect(result.failed).toEqual([]);
    expect(result.stoppedBy?.status).toBe(412);
  });

  test("T-398 a 429 waits retryAfterSeconds and sends the same person with the same version", async () => {
    vi.useFakeTimers();
    try {
      const calls = membersApi((index) =>
        index === 0
          ? problemResponse(429, "write_rate_limited", { retryAfterSeconds: 2, title: "Too many" })
          : null,
      );
      const { promise, onWaiting } = run({
        sleep: (ms) => abortableSleep(ms, new AbortController().signal),
      });
      await vi.advanceTimersByTimeAsync(1_000);
      expect(calls).toHaveLength(1);
      expect(onWaiting).toHaveBeenLastCalledWith(expect.any(Number));
      await vi.advanceTimersByTimeAsync(1_100);
      const result = await promise;
      expect(calls.map((call) => [call.userId, call.ifMatch])).toEqual([
        [PEOPLE[0]?.userId, `"${rowVersion(1)}"`],
        [PEOPLE[0]?.userId, `"${rowVersion(1)}"`],
        [PEOPLE[1]?.userId, `"${rowVersion(3)}"`],
        [PEOPLE[2]?.userId, `"${rowVersion(4)}"`],
      ]);
      expect(onWaiting).toHaveBeenLastCalledWith(null);
      expect(result.added).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });

  test("T-398 a fourth 429 fails that person and the next one is still sent", async () => {
    const calls = membersApi((index, call) =>
      call.userId === PEOPLE[0]?.userId && index < 4
        ? problemResponse(429, "too_many_requests", { retryAfterSeconds: 1 })
        : null,
    );
    const sleep = vi.fn(() => Promise.resolve());
    const result = await run({ sleep }).promise;
    expect(sleep).toHaveBeenCalledTimes(3);
    expect(calls.filter((call) => call.userId === PEOPLE[0]?.userId)).toHaveLength(4);
    expect(result.failed).toEqual([PEOPLE[0]]);
    expect(result.added).toEqual([PEOPLE[1], PEOPLE[2]]);
  });

  test("404 user_not_found or any other failure fails that person; the loop goes on", async () => {
    membersApi((index) =>
      index === 0
        ? problemResponse(404, "user_not_found")
        : index === 1
          ? problemResponse(500, "internal_error")
          : null,
    );
    const result = await run().promise;
    expect(result.failed).toEqual([PEOPLE[0], PEOPLE[1]]);
    expect(result.added).toEqual([PEOPLE[2]]);
    expect(result.stoppedBy).toBeNull();
  });

  test.each([
    [404, "project_not_found"],
    [409, "project_archived"],
    [428, "precondition_required"],
  ])("a %s %s stops the loop", async (status, code) => {
    const calls = membersApi(() => problemResponse(status, code));
    const result = await run().promise;
    expect(calls).toHaveLength(1);
    expect(result.stoppedBy?.code).toBe(code);
    expect(stopsAddPeople(clientError("problem", { status: 409, code: "index_busy" }))).toBe(false);
  });

  test("leaving the tab ends the loop before its next request", async () => {
    const controller = new AbortController();
    const calls = membersApi((index) => {
      if (index === 0) controller.abort();
      return null;
    });
    const result = await run({ signal: controller.signal }).promise;
    expect(result.aborted).toBe(true);
    expect(calls.length).toBeLessThanOrEqual(1);
  });

  test("an aborted wait ends at once", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(abortableSleep(60_000, controller.signal)).resolves.toBeUndefined();
    const later = new AbortController();
    const waiting = abortableSleep(60_000, later.signal);
    later.abort();
    await expect(waiting).resolves.toBeUndefined();
  });
});
