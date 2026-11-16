import { describe, expect, test, vi } from "vitest";

import { IDS } from "../../test/handlers";
import {
  bodyOf,
  citationsEvent,
  deltaEvent,
  doneEvent,
  errorEvent,
  followupsEvent,
  heartbeatEvent,
  metaEvent,
  ndjson,
  statusEvent,
} from "../../test/streamEvents";
import {
  INITIAL_MACHINE,
  advance,
  isStreamEventType,
  isTerminal,
  normaliseAllowedLinks,
  parseStreamEvent,
  type Machine,
  type StreamEvent,
} from "./events";
import { readNdjson } from "./ndjson";

function event(value: unknown): StreamEvent {
  const type = (value as { type: string }).type;
  if (!isStreamEventType(type)) throw new Error(type);
  const parsed = parseStreamEvent(type, value);
  if (parsed === null) throw new Error(`invalid ${type}`);
  return parsed;
}

/** Feeds `events` through the state machine and returns the final machine, or null at the first
 * event the machine refuses. */
function run(events: readonly unknown[], requestSessionId: string | null = null): Machine | null {
  let machine: Machine | null = INITIAL_MACHINE;
  for (const value of events) {
    if (machine === null) return null;
    machine = advance(machine, event(value), requestSessionId);
  }
  return machine;
}

/** The reader and the machine together, as the turn store runs them. */
async function readThrough(
  body: ReadableStream<Uint8Array>,
  requestSessionId: string | null = null,
) {
  let machine: Machine = INITIAL_MACHINE;
  const seen: string[] = [];
  const outcome = await readNdjson(
    body,
    {
      lineArrived: () => undefined,
      event: (value) => {
        const next = advance(machine, value, requestSessionId);
        if (next === null) return "unreadable";
        machine = next;
        seen.push(value.type);
        return isTerminal(machine.state) ? "terminal" : "continue";
      },
    },
    () => "interrupted",
  );
  return { outcome, seen, machine };
}

describe("the event state machine", () => {
  test("T-367 a complete turn goes awaitingMeta, answering, cited, done", () => {
    const states: string[] = [];
    let machine: Machine | null = INITIAL_MACHINE;
    for (const value of [
      metaEvent(),
      heartbeatEvent(),
      statusEvent(),
      deltaEvent("a"),
      deltaEvent("b"),
      citationsEvent(),
      heartbeatEvent(),
      followupsEvent(),
      doneEvent(),
    ]) {
      machine = machine === null ? null : advance(machine, event(value), null);
      states.push(machine?.state ?? "refused");
    }
    expect(states).toEqual([
      "answering",
      "answering",
      "answering",
      "answering",
      "answering",
      "cited",
      "cited",
      "cited",
      "done",
    ]);
  });

  test("T-367 an error event fails the turn in every state before its outcome", () => {
    expect(run([errorEvent()])?.state).toBe("failed");
    expect(run([metaEvent(), deltaEvent("a"), errorEvent()])?.state).toBe("failed");
    expect(run([metaEvent(), citationsEvent(), errorEvent()])?.state).toBe("failed");
  });

  test.each([
    ["delta before meta", [deltaEvent("a")]],
    ["citations before meta", [citationsEvent()]],
    ["a second meta", [metaEvent(), metaEvent()]],
    ["a second citations", [metaEvent(), citationsEvent(), citationsEvent()]],
    ["done before citations", [metaEvent(), deltaEvent("a"), doneEvent()]],
    ["status after delta", [metaEvent(), deltaEvent("a"), statusEvent()]],
    ["a second status", [metaEvent(), statusEvent(), statusEvent()]],
    ["followups before citations", [metaEvent(), followupsEvent()]],
    ["a second followups", [metaEvent(), citationsEvent(), followupsEvent(), followupsEvent()]],
    ["delta after citations", [metaEvent(), citationsEvent(), deltaEvent("a")]],
    ["anything after done", [metaEvent(), citationsEvent(), doneEvent(), heartbeatEvent()]],
    ["anything after an error", [metaEvent(), errorEvent(), heartbeatEvent()]],
    ["an error after done", [metaEvent(), citationsEvent(), doneEvent(), errorEvent()]],
  ])("T-367 %s is refused", (_, events) => {
    expect(run(events)).toBeNull();
  });

  test("T-367 meta must repeat the session the request named", () => {
    expect(run([metaEvent()], IDS.session)?.state).toBe("answering");
    expect(run([metaEvent()], IDS.session.toUpperCase())?.state).toBe("answering");
    expect(run([metaEvent()], IDS.projectA)).toBeNull();
    expect(run([metaEvent({ sessionId: IDS.projectB })], null)?.state).toBe("answering");
  });

  test("T-367 a body that ends after delta is incomplete", async () => {
    const result = await readThrough(bodyOf(ndjson(metaEvent(), deltaEvent("partial"))));
    expect(result.outcome).toEqual({ kind: "failed", code: "stream_incomplete" });
    expect(result.machine.state).toBe("answering");
  });

  test("T-367 a body that ends after citations is incomplete", async () => {
    const result = await readThrough(bodyOf(ndjson(metaEvent(), citationsEvent())));
    expect(result.outcome).toEqual({ kind: "failed", code: "stream_incomplete" });
  });

  test.each([
    ["delta before meta", ndjson(deltaEvent("a"))],
    ["a second citations", ndjson(metaEvent(), citationsEvent(), citationsEvent())],
    ["done before citations", ndjson(metaEvent(), deltaEvent("a"), doneEvent())],
  ])("T-367 %s makes the stream unreadable", async (_, bytes) => {
    const result = await readThrough(bodyOf(bytes));
    expect(result.outcome).toEqual({ kind: "failed", code: "stream_unreadable" });
  });

  test("T-367 after done a further line is ignored and the reader is cancelled", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(ndjson(metaEvent(), citationsEvent(), doneEvent(), deltaEvent("late")));
        controller.enqueue(ndjson(deltaEvent("later")));
      },
      cancel,
    });
    const result = await readThrough(body);
    expect(result.outcome).toEqual({ kind: "terminal" });
    expect(result.seen).toEqual(["meta", "citations", "done"]);
    expect(cancel).toHaveBeenCalled();
  });

  test("T-367 an error event ends the stream as terminal", async () => {
    const result = await readThrough(bodyOf(ndjson(metaEvent(), errorEvent(), deltaEvent("x"))));
    expect(result.outcome).toEqual({ kind: "terminal" });
    expect(result.machine.state).toBe("failed");
  });
});

describe("stream events", () => {
  test("T-367 the eight event types are known and others aren't", () => {
    for (const type of [
      "meta",
      "status",
      "delta",
      "citations",
      "followups",
      "heartbeat",
      "error",
      "done",
    ]) {
      expect(isStreamEventType(type)).toBe(true);
    }
    expect(isStreamEventType("future")).toBe(false);
    expect(isStreamEventType("toString")).toBe(false);
  });

  test("T-367 an event failing its generated schema parses to null", () => {
    expect(parseStreamEvent("meta", { type: "meta", sessionId: "x" })).toBeNull();
    expect(parseStreamEvent("done", { ...doneEvent(), usage: {} })).toBeNull();
    expect(parseStreamEvent("delta", deltaEvent("ok"))).toEqual({ type: "delta", text: "ok" });
  });

  test("allowed links are normalised, and an entry that doesn't parse is dropped", () => {
    expect(
      normaliseAllowedLinks([
        "https://Docs.Example:443/a/../b",
        "https://bücher.example/c",
        "not a url",
      ]),
    ).toEqual(["https://docs.example/b", "https://xn--bcher-kva.example/c"]);
  });
});
