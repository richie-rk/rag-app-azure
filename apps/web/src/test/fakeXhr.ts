// A scriptable XMLHttpRequest for the upload body (10-web-client.md section 10.5), the one call
// `http.ts` sends by XMLHttpRequest. Each request is recorded; a test answers it, fails it or
// reports progress whenever it likes, or `respond` answers it as it is sent.

import { vi } from "vitest";

export interface FakeUpload {
  readonly method: string;
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: unknown;
  /** Whether the client aborted it (Stop, a stall, a sign-out). */
  readonly aborted: boolean;
  /** Whether it has been answered, failed or aborted. */
  readonly settled: boolean;
  progress(loaded: number, total: number): void;
  answer(status: number, body: unknown, contentType?: string): void;
  /** No response: the request failed. */
  fail(): void;
}

export function installFakeXhr(respond?: (xhr: FakeUpload) => void): FakeUpload[] {
  const instances: FakeXhr[] = [];
  class FakeXhr implements FakeUpload {
    status = 0;
    responseText = "";
    method = "";
    url = "";
    body: unknown = null;
    aborted = false;
    settled = false;
    readonly headers: Record<string, string> = {};
    private responseHeaders: Record<string, string> = {};
    readonly upload: { onprogress: ((event: { loaded: number; total: number }) => void) | null } = {
      onprogress: null,
    };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    constructor() {
      instances.push(this);
    }
    open(method: string, url: string) {
      this.method = method;
      this.url = url;
    }
    setRequestHeader(name: string, value: string) {
      this.headers[name] = value;
    }
    getResponseHeader(name: string) {
      return this.responseHeaders[name] ?? null;
    }
    send(body: unknown) {
      this.body = body;
      respond?.(this);
    }
    abort() {
      if (this.settled) return;
      this.aborted = true;
      this.settled = true;
      this.onabort?.();
    }
    progress(loaded: number, total: number) {
      this.upload.onprogress?.({ loaded, total });
    }
    answer(status: number, body: unknown, contentType = "application/json") {
      if (this.settled) return;
      this.settled = true;
      this.status = status;
      this.responseText = typeof body === "string" ? body : JSON.stringify(body);
      this.responseHeaders = { "Content-Type": contentType };
      this.onload?.();
    }
    fail() {
      if (this.settled) return;
      this.settled = true;
      this.onerror?.();
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  return instances;
}
