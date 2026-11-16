// The only console writer (10-web-client.md section 4.1). It writes only in development, and then
// only `{ code, referenceId }` and MSAL errors: never question or answer text, file names, user
// names or tokens. `import.meta.env.DEV` is false in production builds, which drop the calls.

/** MSAL's `LogLevel.Error`; the number keeps MSAL's package out of this module. */
const MSAL_ERROR = 0;

/** MSAL's `loggerCallback`. MSAL is configured at `LogLevel.Error` without PII, and this drops
 * anything else that arrives. */
export function devLog(level: number, message: string, containsPii: boolean): void {
  if (!import.meta.env.DEV || level !== MSAL_ERROR || containsPii) return;
  console.error("[msal]", message);
}

/** Logs a client outcome code and its reference ID in development. */
export function devLogOutcome(code: string, referenceId: string | null): void {
  if (!import.meta.env.DEV) return;
  console.info({ code, referenceId });
}
