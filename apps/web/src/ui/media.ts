// Viewport breakpoints as React state (10-web-client.md section 8.1): 768 px and 1,280 px.

import { useSyncExternalStore } from "react";

import { BREAKPOINT_MEDIUM_PX, BREAKPOINT_WIDE_PX } from "../platform/constants";

export const MEDIUM_QUERY = `(min-width: ${String(BREAKPOINT_MEDIUM_PX)}px)`;
export const WIDE_QUERY = `(min-width: ${String(BREAKPOINT_WIDE_PX)}px)`;

function mediaList(query: string): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
}

/** Whether `query` matches now, re-rendering when that changes. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (listener) => {
      const list = mediaList(query);
      list?.addEventListener("change", listener);
      return () => list?.removeEventListener("change", listener);
    },
    () => mediaList(query)?.matches ?? false,
  );
}

/** Which of the three layouts of section 8.1 applies. */
export type Breakpoint = "narrow" | "medium" | "wide";

export function useBreakpoint(): Breakpoint {
  const medium = useMediaQuery(MEDIUM_QUERY);
  const wide = useMediaQuery(WIDE_QUERY);
  if (wide) return "wide";
  return medium ? "medium" : "narrow";
}
