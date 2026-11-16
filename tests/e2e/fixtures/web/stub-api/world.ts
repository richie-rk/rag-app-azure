// One test's world inside the stub API: its fixture data, the answers and streams it has scripted,
// the requests it received and the header violations it saw. Writes replace whole objects, so the
// readonly fixture types hold throughout.

import type {
  CurrentUser,
  MemoryView,
  ProjectSummary,
} from "../../../../../apps/web/src/api/generated/types.gen.ts";
import { defaultAdmin, defaultMemory, defaultProjects, defaultUser } from "./builders.ts";
import type {
  AdminData,
  RecordedRequest,
  Script,
  StoredSession,
  StreamScript,
  WorldInit,
  WorldSnapshot,
} from "./protocol.ts";

interface PendingScript {
  readonly script: Script;
  remaining: number;
}

export class World {
  readonly id: string;
  user: CurrentUser;
  projects: readonly ProjectSummary[];
  /** Newest first. */
  sessions: readonly StoredSession[];
  memory: MemoryView;
  admin: AdminData;
  sessionsPageSize: number | null;
  readonly requests: RecordedRequest[] = [];
  readonly violations: string[] = [];
  private readonly scripts: PendingScript[] = [];
  private readonly streams: StreamScript[] = [];
  private readonly holds = new Set<() => void>();
  private released = 0;
  private serial = 9000;

  constructor(id: string, init: WorldInit) {
    this.id = id;
    this.user = { ...defaultUser(), ...init.user };
    this.projects = init.projects ?? defaultProjects();
    this.sessions = init.sessions ?? [];
    this.memory = init.memory ?? defaultMemory();
    this.admin = { ...defaultAdmin(this.projects), ...init.admin };
    this.sessionsPageSize = init.sessionsPageSize ?? null;
  }

  /** Replaces the members `init` names; the rest stays as the case left it. */
  update(init: WorldInit): void {
    if (init.user !== undefined) this.user = { ...this.user, ...init.user };
    if (init.projects !== undefined) this.projects = init.projects;
    if (init.sessions !== undefined) this.sessions = init.sessions;
    if (init.memory !== undefined) this.memory = init.memory;
    if (init.admin !== undefined) this.admin = { ...this.admin, ...init.admin };
    if (init.sessionsPageSize !== undefined) this.sessionsPageSize = init.sessionsPageSize;
  }

  addScript(script: Script): void {
    this.scripts.push({ script, remaining: script.times ?? 1 });
  }

  /** The first pending script for this operation whose query condition holds, consumed once. */
  takeScript(operation: string, query: URLSearchParams): Script | null {
    for (const pending of this.scripts) {
      if (pending.remaining <= 0 || pending.script.operation !== operation) continue;
      const wanted = Object.entries(pending.script.query ?? {});
      const matches = wanted.every(([name, value]) =>
        value === true ? query.has(name) : query.get(name) === value,
      );
      if (!matches) continue;
      pending.remaining -= 1;
      return pending.script;
    }
    return null;
  }

  addStream(script: StreamScript): void {
    this.streams.push(script);
  }

  takeStream(): StreamScript | null {
    return this.streams.shift() ?? null;
  }

  /** Resolves when the spec releases held streams; a release before the hold counts too. */
  hold(): { readonly done: Promise<void>; readonly cancel: () => void } {
    if (this.released > 0) {
      this.released -= 1;
      return { done: Promise.resolve(), cancel: () => undefined };
    }
    let resolve: () => void = () => undefined;
    const done = new Promise<void>((settle) => {
      resolve = settle;
    });
    this.holds.add(resolve);
    return {
      done,
      cancel: () => {
        this.holds.delete(resolve);
        resolve();
      },
    };
  }

  release(): void {
    if (this.holds.size === 0) {
      this.released += 1;
      return;
    }
    for (const resolve of this.holds) resolve();
    this.holds.clear();
  }

  nextSerial(): number {
    this.serial += 1;
    return this.serial;
  }

  snapshot(): WorldSnapshot {
    return {
      id: this.id,
      user: this.user,
      projects: this.projects,
      sessions: this.sessions,
      memory: this.memory,
      admin: this.admin,
      violations: this.violations,
    };
  }
}
