// The gates every browser test runs under (10-web-client.md section 13.2, T-408): a
// `securitypolicyviolation` collector installed with an init script in every page and frame, a
// console collector, and a request recorder. Any CSP violation, console error, uncaught page error,
// Permissions-Policy warning, or request to an origin other than the site, the stub API and
// Entra ID fails the test when it ends.

import type { BrowserContext, ConsoleMessage, Page, Request } from "@playwright/test";

import { LOGIN_ORIGIN } from "./identity.ts";
import { API_ORIGIN, APP_ORIGIN, CONTROL_ORIGIN } from "./stub-api/protocol.ts";

const REPORT_BINDING = "__docchatReportCspViolation";

const CSP_COLLECTOR = `document.addEventListener("securitypolicyviolation", (event) => {
  const report = window.${REPORT_BINDING};
  if (typeof report === "function") {
    void report(event.violatedDirective + " refused " + (event.blockedURI || "inline") +
      " at " + (event.sourceFile || window.location.href) + ":" + String(event.lineNumber));
  }
}, true);`;

/** The browser's own log line for an HTTP answer a case scripted (a 401, 404, 412, 503...): the
 * page didn't write it, and the client's handling of that answer is what the case asserts. */
const SCRIPTED_STATUS_LOG = /^Failed to load resource: the server responded with a status of \d{3}/;

const ALLOWED_ORIGINS = new Set([APP_ORIGIN, API_ORIGIN, LOGIN_ORIGIN]);

export class Gates {
  readonly cspViolations: string[] = [];
  readonly consoleProblems: string[] = [];
  readonly pageErrors: string[] = [];
  readonly offOrigin: string[] = [];
  readonly loginFrames: string[] = [];
  /** Every request the app's pages made, in order. */
  readonly requests: string[] = [];
  private readonly allowed: RegExp[] = [];
  private readonly helpers = new WeakSet<Page>();
  private readonly context: BrowserContext;

  constructor(context: BrowserContext) {
    this.context = context;
  }

  async install(): Promise<void> {
    await this.context.exposeBinding(REPORT_BINDING, ({ page }, text: string) => {
      if (!this.helpers.has(page)) this.cspViolations.push(text);
    });
    await this.context.addInitScript(CSP_COLLECTOR);
    for (const page of this.context.pages()) this.watch(page);
    this.context.on("page", (page) => {
      this.watch(page);
    });
    this.context.on("request", (request) => {
      this.record(request);
    });
  }

  /** A console line the case provokes on purpose and names. */
  allowConsole(pattern: RegExp): void {
    this.allowed.push(pattern);
  }

  /** A page outside the gates, on the stub's control origin (the clipboard read-back). */
  async helperPage(): Promise<Page> {
    const page = await this.context.newPage();
    this.helpers.add(page);
    await page.goto(`${CONTROL_ORIGIN}/blank`);
    return page;
  }

  failures(): string[] {
    return [
      ...this.cspViolations.map((text) => `CSP violation: ${text}`),
      ...this.consoleProblems.map((text) => `console: ${text}`),
      ...this.pageErrors.map((text) => `page error: ${text}`),
      ...this.offOrigin.map((text) => `request to another origin: ${text}`),
      ...this.loginFrames.map((text) => `frame on Entra ID: ${text}`),
    ];
  }

  private watch(page: Page): void {
    page.on("console", (message) => {
      if (!this.helpers.has(page)) this.onConsole(message);
    });
    page.on("pageerror", (error) => {
      if (!this.helpers.has(page)) this.pageErrors.push(`${error.name}: ${error.message}`);
    });
    // Sign-in uses top-level navigations only: no frame ever loads Entra ID (section 4.1).
    page.on("framenavigated", (frame) => {
      if (frame !== page.mainFrame() && frame.url().startsWith(LOGIN_ORIGIN)) {
        this.loginFrames.push(frame.url());
      }
    });
  }

  private onConsole(message: ConsoleMessage): void {
    const text = message.text();
    const permissionsPolicy = /permissions[- ]policy/i.test(text);
    if (message.type() !== "error" && !permissionsPolicy) return;
    if (!permissionsPolicy && SCRIPTED_STATUS_LOG.test(text)) return;
    if (this.allowed.some((pattern) => pattern.test(text))) return;
    this.consoleProblems.push(`${message.type()}: ${text}`);
  }

  private record(request: Request): void {
    let page: Page | null = null;
    try {
      page = request.frame().page();
    } catch {
      // A request without a frame (a service worker's) belongs to no helper page.
    }
    if (page !== null && this.helpers.has(page)) return;
    const url = request.url();
    // Only network requests count: blob and data URLs are local, and the browser's own PDF viewer
    // loads its chrome:// and extension resources inside the frame.
    if (!url.startsWith("http:") && !url.startsWith("https:")) return;
    this.requests.push(`${request.method()} ${url}`);
    const origin = new URL(url).origin;
    if (!ALLOWED_ORIGINS.has(origin)) this.offOrigin.push(`${request.method()} ${url}`);
  }
}
