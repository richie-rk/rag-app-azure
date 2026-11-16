// The theme (10-web-client.md sections 6.3 and 11.5): Fluent's web light and dark themes,
// unmodified. It follows `docchat.theme`, else `prefers-color-scheme`; toggling writes the key.

import { webDarkTheme, webLightTheme, type Theme } from "@fluentui/react-components";
import { useSyncExternalStore } from "react";

import { readTheme, writeTheme, type ThemeChoice } from "../platform/storage";

const DARK_QUERY = "(prefers-color-scheme: dark)";
const listeners = new Set<() => void>();
/** This page's choice, which applies even when storage refuses to remember it. */
let chosen: ThemeChoice | null = null;

function systemTheme(): ThemeChoice {
  return typeof window.matchMedia === "function" && window.matchMedia(DARK_QUERY).matches
    ? "dark"
    : "light";
}

/** The theme now: the remembered choice, else the system's. */
export function currentTheme(): ThemeChoice {
  return chosen ?? readTheme() ?? systemTheme();
}

function notify(): void {
  for (const listener of listeners) listener();
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  // Without a remembered choice the theme follows the system as it changes.
  const media = typeof window.matchMedia === "function" ? window.matchMedia(DARK_QUERY) : null;
  const onChange = () => {
    if (chosen === null && readTheme() === null) listener();
  };
  media?.addEventListener("change", onChange);
  return () => {
    listeners.delete(listener);
    media?.removeEventListener("change", onChange);
  };
}

/** Remembers the choice and applies it to every subscriber. */
export function setTheme(theme: ThemeChoice): void {
  chosen = theme;
  writeTheme(theme);
  notify();
}

/** For tests: forgets this page's choice. */
export function resetThemeForTests(): void {
  chosen = null;
}

export function useThemeChoice(): ThemeChoice {
  return useSyncExternalStore(subscribeTheme, currentTheme);
}

export function fluentTheme(choice: ThemeChoice): Theme {
  return choice === "dark" ? webDarkTheme : webLightTheme;
}
