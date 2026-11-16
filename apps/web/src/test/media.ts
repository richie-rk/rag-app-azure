// A controllable `window.matchMedia` for jsdom, which has none: the viewport width and the colour
// scheme the tests choose decide which `min-width` and `prefers-color-scheme` queries match.

interface MediaState {
  width: number;
  dark: boolean;
}

const state: MediaState = { width: 1_440, dark: false };
const lists = new Set<FakeMediaQueryList>();

function evaluate(query: string): boolean {
  const minWidth = /\(min-width:\s*(\d+)px\)/.exec(query);
  if (minWidth?.[1] !== undefined) return state.width >= Number(minWidth[1]);
  const maxWidth = /\(max-width:\s*(\d+)px\)/.exec(query);
  if (maxWidth?.[1] !== undefined) return state.width <= Number(maxWidth[1]);
  if (query.includes("prefers-color-scheme: dark")) return state.dark;
  return false;
}

class FakeMediaQueryList extends EventTarget {
  readonly media: string;
  onchange: ((event: MediaQueryListEvent) => void) | null = null;
  private current: boolean;

  constructor(query: string) {
    super();
    this.media = query;
    this.current = evaluate(query);
  }

  get matches(): boolean {
    return evaluate(this.media);
  }

  /** Deprecated listener API that some libraries still call. */
  addListener(listener: (event: MediaQueryListEvent) => void): void {
    this.addEventListener("change", listener as EventListener);
  }

  removeListener(listener: (event: MediaQueryListEvent) => void): void {
    this.removeEventListener("change", listener as EventListener);
  }

  recheck(): void {
    const next = evaluate(this.media);
    if (next === this.current) return;
    this.current = next;
    const event = new Event("change") as MediaQueryListEvent;
    Object.defineProperty(event, "matches", { value: next });
    this.dispatchEvent(event);
  }
}

function notifyAll(): void {
  for (const list of lists) list.recheck();
}

/** Installs the fake on `window`; the setup file calls it before each test. */
export function installMatchMedia(): void {
  state.width = 1_440;
  state.dark = false;
  lists.clear();
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string) => {
      const list = new FakeMediaQueryList(query);
      lists.add(list);
      return list;
    },
  });
}

/** Changes the viewport width the media queries see, and notifies their listeners. */
export function setViewportWidth(width: number): void {
  state.width = width;
  notifyAll();
}

/** Changes the system colour scheme, and notifies the listeners. */
export function setSystemDark(dark: boolean): void {
  state.dark = dark;
  notifyAll();
}
