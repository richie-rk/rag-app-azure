import { describe, expect, test, vi } from "vitest";

import { MAX_STREAM_LINE_BYTES } from "../../platform/constants";
import { streamFixtures } from "../../test/files";
import type { StreamEvent } from "./events";
import { handleLine, readNdjson, type LineVerdict, type StreamSink } from "./ndjson";

const encoder = new TextEncoder();

/** The shared stream fixtures of 84-testing.md section 1.5, read by the API's stream tests too. */
const fixtures = streamFixtures;

function streamOf(chunks: readonly Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

/** Records every event and never ends the turn, so the whole body is read. */
function recorder() {
  const events: StreamEvent[] = [];
  let lines = 0;
  const sink: StreamSink = {
    lineArrived: () => {
      lines += 1;
    },
    event: (event): LineVerdict => {
      events.push(event);
      return "continue";
    },
  };
  return { sink, events, lines: () => lines };
}

const interrupted = () => "interrupted" as const;

async function eventsOf(chunks: readonly Uint8Array[]): Promise<StreamEvent[]> {
  const { sink, events } = recorder();
  await readNdjson(streamOf(chunks), sink, interrupted);
  return events;
}

/** What each fixture holds, read without the reader: one JSON object per line. */
function expectedEvents(bytes: Uint8Array): unknown[] {
  return new TextDecoder()
    .decode(bytes)
    .split("\n")
    .filter((line) => line !== "")
    .map((line) => JSON.parse(line) as unknown);
}

describe("the NDJSON reader", () => {
  test("T-366 the shared stream fixtures hold multi-byte characters and a 70 KB line", () => {
    const all = fixtures();
    expect(all.length).toBeGreaterThanOrEqual(5);
    const text = all.map(([, bytes]) => new TextDecoder().decode(bytes)).join("");
    expect(text).toMatch(/[\u0080-߿]/u);
    expect(text).toMatch(/[ࠀ-￿]/u);
    expect(text).toMatch(/[\u{10000}-\u{10ffff}]/u);
    const longest = Math.max(
      ...all.flatMap(([, bytes]) =>
        new TextDecoder()
          .decode(bytes)
          .split("\n")
          .map((line) => encoder.encode(line).length),
      ),
    );
    expect(longest).toBeGreaterThanOrEqual(70_000);
  });

  test("T-366 every fixture parses to its events read whole", async () => {
    for (const [name, bytes] of fixtures()) {
      expect(await eventsOf([bytes]), name).toEqual(expectedEvents(bytes));
    }
  });

  test("T-366 every fixture parses to the same events split into two chunks at every byte offset", async () => {
    for (const [name, bytes] of fixtures()) {
      // Compared as JSON text with the whole read, which the test above checks against the
      // fixture, because a deep comparison per offset of the 70 KB line is slow.
      const expected = JSON.stringify(await eventsOf([bytes]));
      for (let offset = 0; offset <= bytes.length; offset += 1) {
        const events = await eventsOf([bytes.subarray(0, offset), bytes.subarray(offset)]);
        if (JSON.stringify(events) !== expected) {
          expect.fail(`${name} split at byte ${String(offset)} parses differently`);
        }
      }
    }
  }, 120_000);

  test("T-366 every fixture parses to the same events sent one byte per chunk", async () => {
    for (const [name, bytes] of fixtures()) {
      const chunks = Array.from(bytes, (_, index) => bytes.subarray(index, index + 1));
      expect(await eventsOf(chunks), name).toEqual(expectedEvents(bytes));
    }
  }, 60_000);

  test("T-366 \\r\\n line endings parse", async () => {
    for (const [name, bytes] of fixtures()) {
      const crlf = encoder.encode(new TextDecoder().decode(bytes).replaceAll("\n", "\r\n"));
      expect(await eventsOf([crlf]), name).toEqual(expectedEvents(bytes));
    }
  });

  test("T-366 empty lines and unknown event types are skipped", async () => {
    const [, bytes] = fixtures()[0] ?? ["", new Uint8Array()];
    const lines = new TextDecoder().decode(bytes).split("\n");
    const padded = lines
      .flatMap((line) => [line, "", '{"type":"later_addition","value":1}', "\r"])
      .join("\n");
    expect(await eventsOf([encoder.encode(padded)])).toEqual(expectedEvents(bytes));
  });

  test("T-367 a final line without its newline is unreadable", async () => {
    const { sink } = recorder();
    const outcome = await readNdjson(
      streamOf([encoder.encode('{"type":"heartbeat"}')]),
      sink,
      interrupted,
    );
    expect(outcome).toEqual({ kind: "failed", code: "stream_unreadable" });
  });

  test("T-367 a body that ends without a terminal event is incomplete", async () => {
    const { sink } = recorder();
    const outcome = await readNdjson(
      streamOf([encoder.encode('{"type":"heartbeat"}\n')]),
      sink,
      interrupted,
    );
    expect(outcome).toEqual({ kind: "failed", code: "stream_incomplete" });
  });

  test("T-367 a 1,048,577-byte line is unreadable before it is complete", async () => {
    const { sink, lines } = recorder();
    const outcome = await readNdjson(
      streamOf([new Uint8Array(MAX_STREAM_LINE_BYTES).fill(0x20), new Uint8Array([0x20])]),
      sink,
      interrupted,
    );
    expect(outcome).toEqual({ kind: "failed", code: "stream_unreadable" });
    expect(lines()).toBe(0);
  });

  test("T-367 a line of 1,048,577 bytes is unreadable even when it arrives whole", () => {
    const { sink } = recorder();
    expect(handleLine(new Uint8Array(MAX_STREAM_LINE_BYTES + 1).fill(0x20), sink)).toBe(
      "unreadable",
    );
    expect(handleLine(new Uint8Array(MAX_STREAM_LINE_BYTES).fill(0x20), sink)).toBe("unreadable");
  });

  test("T-367 invalid UTF-8, invalid JSON, a non-object and a missing type are unreadable", () => {
    const { sink } = recorder();
    expect(handleLine(new Uint8Array([0x7b, 0xff, 0x7d]), sink)).toBe("unreadable");
    expect(handleLine(encoder.encode("{not json"), sink)).toBe("unreadable");
    expect(handleLine(encoder.encode("[1,2]"), sink)).toBe("unreadable");
    expect(handleLine(encoder.encode("null"), sink)).toBe("unreadable");
    expect(handleLine(encoder.encode('"text"'), sink)).toBe("unreadable");
    expect(handleLine(encoder.encode('{"kind":"delta"}'), sink)).toBe("unreadable");
    expect(handleLine(encoder.encode('{"type":7}'), sink)).toBe("unreadable");
  });

  test("T-367 a known event failing its schema is unreadable", () => {
    const { sink, events } = recorder();
    expect(handleLine(encoder.encode('{"type":"delta","text":""}'), sink)).toBe("unreadable");
    expect(handleLine(encoder.encode('{"type":"status","status":"something_else"}'), sink)).toBe(
      "unreadable",
    );
    expect(events).toEqual([]);
  });

  test("T-367 a terminal verdict cancels the reader and ignores what follows", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('{"type":"heartbeat"}\n{"type":"heartbeat"}\n'));
      },
      cancel,
    });
    const events: StreamEvent[] = [];
    const outcome = await readNdjson(
      body,
      {
        lineArrived: () => undefined,
        event: (event) => {
          events.push(event);
          return "terminal";
        },
      },
      interrupted,
    );
    expect(outcome).toEqual({ kind: "terminal" });
    expect(events).toHaveLength(1);
    expect(cancel).toHaveBeenCalled();
  });

  test("T-367 an unreadable verdict cancels the reader", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('{"type":"heartbeat"}\n'));
      },
      cancel,
    });
    const outcome = await readNdjson(
      body,
      { lineArrived: () => undefined, event: () => "unreadable" },
      interrupted,
    );
    expect(outcome).toEqual({ kind: "failed", code: "stream_unreadable" });
    expect(cancel).toHaveBeenCalled();
  });

  test.each([
    ["stopped", { kind: "stopped" }],
    ["aborted", { kind: "aborted" }],
    ["stalled", { kind: "failed", code: "stream_stalled" }],
    ["interrupted", { kind: "failed", code: "stream_interrupted" }],
  ] as const)(
    "T-367 a rejected read classified %s ends the reader",
    async (rejection, expected) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.error(new Error("cut"));
        },
      });
      const { sink } = recorder();
      expect(await readNdjson(body, sink, () => rejection)).toEqual(expected);
    },
  );

  test("T-366 each complete non-empty line restarts the stall timer", async () => {
    const { sink, lines } = recorder();
    await readNdjson(
      streamOf([encoder.encode('{"type":"heartbeat"}\n\n\r\n{"type":"heartbeat"}\n')]),
      sink,
      interrupted,
    );
    expect(lines()).toBe(2);
  });
});
