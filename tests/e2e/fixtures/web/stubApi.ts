// The specs' side of the stub API's control port: create a world, script its answers and streams,
// release a held stream, and read back what the client sent and what the stub stored.

import {
  CONTROL_ORIGIN,
  type OperationId,
  type RecordedRequest,
  type Script,
  type ScriptedAnswer,
  type StreamScript,
  type WorldInit,
  type WorldSnapshot,
} from "./stub-api/protocol.ts";

async function control<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${CONTROL_ORIGIN}${path}`, {
    method,
    ...(body === undefined
      ? {}
      : { body: JSON.stringify(body), headers: { "Content-Type": "application/json" } }),
  });
  if (!response.ok) {
    throw new Error(`stub control ${method} ${path} answered ${String(response.status)}`);
  }
  const text = await response.text();
  return (text === "" ? undefined : JSON.parse(text)) as T;
}

export class StubApi {
  readonly worldId: string;
  readonly startedAt: number;

  private constructor(worldId: string, startedAt: number) {
    this.worldId = worldId;
    this.startedAt = startedAt;
  }

  static async create(init: WorldInit): Promise<StubApi> {
    const startedAt = Date.now();
    const { id } = await control<{ id: string }>("POST", "/worlds", init);
    return new StubApi(id, startedAt);
  }

  private path(suffix = ""): string {
    return `/worlds/${this.worldId}${suffix}`;
  }

  /** Answers the operation's next matching request(s) as scripted. */
  async script(script: Script): Promise<void> {
    await control("POST", this.path("/scripts"), script);
  }

  /** Shorthand: the operation's next request answers this problem. */
  async problem(
    operation: OperationId,
    answer: Omit<Extract<ScriptedAnswer, { kind: "problem" }>, "kind">,
    options: Pick<Script, "query" | "times"> = {},
  ): Promise<void> {
    await this.script({ operation, answer: { kind: "problem", ...answer }, ...options });
  }

  /** The next `createChatTurn` streams this script. */
  async stream(script: StreamScript): Promise<void> {
    await control("POST", this.path("/streams"), script);
  }

  /** Lets a held stream go on. */
  async release(): Promise<void> {
    await control("POST", this.path("/release"));
  }

  async update(init: WorldInit): Promise<WorldSnapshot> {
    return control<WorldSnapshot>("PATCH", this.path(), init);
  }

  async snapshot(): Promise<WorldSnapshot> {
    return control<WorldSnapshot>("GET", this.path());
  }

  async requests(operation?: OperationId): Promise<RecordedRequest[]> {
    const all = await control<RecordedRequest[]>("GET", this.path("/requests"));
    return operation === undefined ? all : all.filter((item) => item.operation === operation);
  }

  /** Header violations of this world, plus token-less requests since the test began. */
  async violations(): Promise<string[]> {
    const snapshot = await this.snapshot();
    const orphans = await control<{ message: string }[]>(
      "GET",
      `/orphans?since=${String(this.startedAt)}`,
    );
    return [...snapshot.violations, ...orphans.map((item) => item.message)];
  }

  async dispose(): Promise<void> {
    await control("DELETE", this.path());
  }
}
