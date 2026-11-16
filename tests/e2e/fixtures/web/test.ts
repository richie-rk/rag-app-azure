// The browser suites' Playwright fixtures (10-web-client.md section 13.2): each test gets its own
// stub-API world, the stub identity provider routed into its browser context, and the gates of
// T-408, all installed before its first page opens. Specs import `test` and `expect` from here.

import { test as base, expect, type Locator, type Page } from "@playwright/test";

import { en } from "../../../../apps/web/src/copy/en.ts";
import { expectAccessible } from "./accessibility.ts";
import { Gates } from "./gates.ts";
import { StubIdentityProvider, type IdentityOptions } from "./identity.ts";
import { StubApi } from "./stubApi.ts";
import type { WorldInit } from "./stub-api/protocol.ts";

export { en, expect, expectAccessible };

/** Fills the `{name}` values of a text from `en`, as the client's `fillText` does. */
export function fill(template: string, values: Readonly<Record<string, string | number>>): string {
  return template.replace(/\{(\w+)\}/g, (match, name: string) => {
    const value = values[name];
    return value === undefined ? match : String(value);
  });
}

/** The app as a signed-in person sees it, with the locators the specs share. */
export class App {
  readonly page: Page;

  constructor(page: Page) {
    this.page = page;
  }

  /** Opens a path; without a cached account the page signs in through the stub first. */
  async open(path = "/chat"): Promise<void> {
    await this.page.goto(path);
  }

  /** Opens a path and waits for the app shell, which renders once `getCurrentUser` succeeds. */
  async openShell(path = "/chat"): Promise<void> {
    await this.open(path);
    await this.waitForShell();
  }

  async waitForShell(): Promise<void> {
    await expect(this.page.getByRole("banner")).toBeVisible();
    await expect(this.page.locator("main#main")).toBeVisible();
  }

  /** The `h1` of the screen on show. */
  heading(): Locator {
    return this.page.getByRole("heading", { level: 1 });
  }

  composer(): Locator {
    return this.page.getByRole("textbox", { name: en["chat.composer.label"] });
  }

  sendButton(): Locator {
    return this.page.getByRole("button", { name: en["chat.composer.send"], exact: true });
  }

  stopButton(): Locator {
    return this.page.getByRole("button", { name: en["chat.composer.stop"], exact: true });
  }

  /** The conversation's `article` for a question, labelled by the question. */
  turn(question: string): Locator {
    return this.page
      .getByRole("region", { name: en["chat.conversation.label"] })
      .getByRole("article", { name: question, exact: true });
  }

  /** Types a question and sends it with Enter. */
  async ask(question: string): Promise<void> {
    await this.composer().fill(question);
    await this.composer().press("Enter");
  }

  /** The turn's Sources list. */
  sources(turn: Locator): Locator {
    return turn
      .getByRole("list")
      .filter({ has: this.page.getByRole("button") })
      .last();
  }

  sessionPanel(): Locator {
    return this.page.getByRole("complementary", { name: en["chat.sessions.label"] });
  }

  statusRegion(): Locator {
    return this.page.getByRole("status").and(this.page.locator("[aria-atomic='true']"));
  }

  async signOut(): Promise<void> {
    await this.page.getByRole("button", { name: /^Account: / }).click();
    await this.page.getByRole("menuitem", { name: en["shell.account.signOut"] }).click();
  }
}

interface Options {
  /** The world's fixture data. */
  readonly world: WorldInit;
  /** The stub identity provider's behaviour. */
  readonly identity: IdentityOptions;
}

interface Fixtures {
  readonly stub: StubApi;
  readonly idp: StubIdentityProvider;
  readonly gates: Gates;
  readonly app: App;
}

export const test = base.extend<Options & Fixtures>({
  world: [{}, { option: true }],
  identity: [{}, { option: true }],
  stub: async ({ world }, use) => {
    const stub = await StubApi.create(world);
    await use(stub);
    const refused = await stub.violations();
    await stub.dispose();
    expect(refused, "requests the stub API refused").toEqual([]);
  },
  idp: async ({ context, stub, identity }, use) => {
    const idp = new StubIdentityProvider(stub.worldId, identity);
    await idp.install(context);
    await use(idp);
    expect(idp.unexpected, "Entra ID requests no stub answers").toEqual([]);
    // Renewal never uses a hidden iframe or a silent authorize request (section 4.1, T-355).
    const silent = idp.authorizeRequests.filter((url) => url.searchParams.get("prompt") === "none");
    expect(silent.map(String), "authorize requests with prompt=none").toEqual([]);
  },
  gates: async ({ context }, use) => {
    const gates = new Gates(context);
    await gates.install();
    await use(gates);
    expect(gates.failures(), "gate failures").toEqual([]);
  },
  page: async ({ context, gates, idp }, use) => {
    // Depending on the gates and the stub identity provider puts both in place before the page
    // opens.
    expect([gates, idp]).toHaveLength(2);
    const page = await context.newPage();
    await use(page);
    // Every tab of the test closes before its world goes: a tab left open could still refetch.
    for (const open of context.pages()) await open.close();
  },
  app: async ({ page }, use) => {
    await use(new App(page));
  },
});
