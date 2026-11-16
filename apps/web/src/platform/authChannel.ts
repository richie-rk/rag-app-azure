// The sign-out signal between tabs (10-web-client.md section 4.6). Session storage is per tab, so
// a tab that missed the sign-out would keep working tokens; every tab of the app listens here.

export const AUTH_CHANNEL_NAME = "docchat-auth";

export interface AuthChannel {
  /** Tells every other tab that this one signed out. */
  postSignedOut(): void;
  close(): void;
}

/** Only `{ "type": "signed-out" }` counts; a message of any other shape is ignored. */
export function isSignedOutMessage(data: unknown): boolean {
  if (typeof data !== "object" || data === null || Array.isArray(data)) return false;
  const keys = Object.keys(data);
  return keys.length === 1 && (data as { type?: unknown }).type === "signed-out";
}

export function openAuthChannel(onSignedOut: () => void): AuthChannel {
  const channel = new BroadcastChannel(AUTH_CHANNEL_NAME);
  channel.onmessage = (event: MessageEvent<unknown>) => {
    if (isSignedOutMessage(event.data)) onSignedOut();
  };
  return {
    postSignedOut() {
      channel.postMessage({ type: "signed-out" });
    },
    close() {
      channel.onmessage = null;
      channel.close();
    },
  };
}
