// The chat stream's events and the state machine each turn follows (10-web-client.md section 8.8):
// `awaitingMeta`, `answering`, `cited`, `done` or `failed`. An event a state doesn't allow makes
// the stream unreadable; every turn gets exactly one terminal outcome.

import type { z } from "zod";

import "../../bootstrap/zodSetup";
import {
  zStreamCitationsEvent,
  zStreamDeltaEvent,
  zStreamDoneEvent,
  zStreamErrorEvent,
  zStreamFollowupsEvent,
  zStreamHeartbeatEvent,
  zStreamMetaEvent,
  zStreamStatusEvent,
} from "../../api/generated/zod.gen";

/** The generated schema of each of the eight event types (`01-repository.md` section 7.2). */
export const STREAM_EVENT_SCHEMAS = {
  meta: zStreamMetaEvent,
  status: zStreamStatusEvent,
  delta: zStreamDeltaEvent,
  citations: zStreamCitationsEvent,
  followups: zStreamFollowupsEvent,
  heartbeat: zStreamHeartbeatEvent,
  error: zStreamErrorEvent,
  done: zStreamDoneEvent,
} as const;

export type StreamEventType = keyof typeof STREAM_EVENT_SCHEMAS;

export type StreamEvent = {
  [K in StreamEventType]: z.output<(typeof STREAM_EVENT_SCHEMAS)[K]>;
}[StreamEventType];

export type MetaEvent = z.output<typeof zStreamMetaEvent>;
export type CitationsEvent = z.output<typeof zStreamCitationsEvent>;
export type ErrorEvent = z.output<typeof zStreamErrorEvent>;
export type Citation = CitationsEvent["citations"][number];

export function isStreamEventType(type: string): type is StreamEventType {
  return Object.hasOwn(STREAM_EVENT_SCHEMAS, type);
}

/** Validates a line's object against its type's generated schema; null when it fails. */
export function parseStreamEvent(type: StreamEventType, value: unknown): StreamEvent | null {
  const parsed = STREAM_EVENT_SCHEMAS[type].safeParse(value);
  return parsed.success ? parsed.data : null;
}

export type MachineState = "awaitingMeta" | "answering" | "cited" | "done" | "failed";

export interface Machine {
  readonly state: MachineState;
  readonly statusSeen: boolean;
  readonly deltaSeen: boolean;
  readonly followupsSeen: boolean;
}

export const INITIAL_MACHINE: Machine = {
  state: "awaitingMeta",
  statusSeen: false,
  deltaSeen: false,
  followupsSeen: false,
};

export function isTerminal(state: MachineState): boolean {
  return state === "done" || state === "failed";
}

/** The machine after `event`, or null when the event isn't allowed: the stream is unreadable.
 * `requestSessionId` is the session the request named, which `meta` must repeat. */
export function advance(
  machine: Machine,
  event: StreamEvent,
  requestSessionId: string | null,
): Machine | null {
  if (event.type === "heartbeat" && !isTerminal(machine.state)) return machine;
  if (event.type === "error" && !isTerminal(machine.state)) return { ...machine, state: "failed" };
  switch (machine.state) {
    case "awaitingMeta":
      if (event.type !== "meta") return null;
      if (
        requestSessionId !== null &&
        event.sessionId.toLowerCase() !== requestSessionId.toLowerCase()
      ) {
        return null;
      }
      return { ...machine, state: "answering" };
    case "answering":
      if (event.type === "status" && !machine.statusSeen && !machine.deltaSeen) {
        return { ...machine, statusSeen: true };
      }
      if (event.type === "delta") return { ...machine, deltaSeen: true };
      if (event.type === "citations") return { ...machine, state: "cited" };
      return null;
    case "cited":
      if (event.type === "followups" && !machine.followupsSeen) {
        return { ...machine, followupsSeen: true };
      }
      if (event.type === "done") return { ...machine, state: "done" };
      return null;
    case "done":
    case "failed":
      return null;
  }
}

/** The normalised form of each `allowedLinks` entry (section 9.3): `new URL(s).href`; an entry
 * that doesn't parse is dropped. */
export function normaliseAllowedLinks(entries: readonly string[]): string[] {
  const normalised: string[] = [];
  for (const entry of entries) {
    try {
      normalised.push(new URL(entry).href);
    } catch {
      // Dropped: it can never match a link.
    }
  }
  return normalised;
}
