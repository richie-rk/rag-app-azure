// Every browser-storage read and write of the app (10-web-client.md section 2.4). Each access sits
// in try/catch, because storage can be disabled or full; a failure means the default. No user
// content is ever written here.

const SIGNED_OUT_KEY = "docchat.signedOut";
const THEME_KEY = "docchat.theme";
const NAV_OPEN_KEY = "docchat.nav.open";

export type ThemeChoice = "light" | "dark";

function read(storage: () => Storage, key: string): string | null {
  try {
    return storage().getItem(key);
  } catch {
    return null;
  }
}

function write(storage: () => Storage, key: string, value: string): void {
  try {
    storage().setItem(key, value);
  } catch {
    // The preference simply isn't remembered.
  }
}

function remove(storage: () => Storage, key: string): void {
  try {
    storage().removeItem(key);
  } catch {
    // Nothing was stored, or storage is unavailable; either way the flag reads as absent.
  }
}

const session = (): Storage => sessionStorage;
const local = (): Storage => localStorage;

/** Whether this tab signed out explicitly, which stops the automatic sign-in at start-up. */
export function readSignedOut(): boolean {
  return read(session, SIGNED_OUT_KEY) === "1";
}

export function setSignedOut(): void {
  write(session, SIGNED_OUT_KEY, "1");
}

export function clearSignedOut(): void {
  remove(session, SIGNED_OUT_KEY);
}

/** The remembered theme, or null to follow `prefers-color-scheme`. */
export function readTheme(): ThemeChoice | null {
  const value = read(local, THEME_KEY);
  return value === "light" || value === "dark" ? value : null;
}

export function writeTheme(theme: ThemeChoice): void {
  write(local, THEME_KEY, theme);
}

/** The remembered inline state of the navigation drawer, or null for the default. */
export function readNavOpen(): boolean | null {
  const value = read(local, NAV_OPEN_KEY);
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

export function writeNavOpen(open: boolean): void {
  write(local, NAV_OPEN_KEY, open ? "true" : "false");
}
