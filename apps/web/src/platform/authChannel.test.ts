import { afterEach, describe, expect, test, vi } from "vitest";

import { AUTH_CHANNEL_NAME, isSignedOutMessage, openAuthChannel } from "./authChannel";

class FakeChannel {
  static instances: FakeChannel[] = [];
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  readonly posted: unknown[] = [];
  closed = false;
  constructor(readonly name: string) {
    FakeChannel.instances.push(this);
  }
  postMessage(message: unknown) {
    this.posted.push(message);
  }
  close() {
    this.closed = true;
  }
}

afterEach(() => {
  FakeChannel.instances = [];
});

describe("the sign-out channel", () => {
  test("only { type: signed-out } counts", () => {
    expect(isSignedOutMessage({ type: "signed-out" })).toBe(true);
    expect(isSignedOutMessage({ type: "signed-out", extra: 1 })).toBe(false);
    expect(isSignedOutMessage({ type: "signed-in" })).toBe(false);
    expect(isSignedOutMessage("signed-out")).toBe(false);
    expect(isSignedOutMessage(null)).toBe(false);
    expect(isSignedOutMessage(["signed-out"])).toBe(false);
  });

  test("T-356 the channel docchat-auth posts the sign-out and hears other tabs'", () => {
    vi.stubGlobal("BroadcastChannel", FakeChannel);
    const onSignedOut = vi.fn();
    const channel = openAuthChannel(onSignedOut);
    const fake = FakeChannel.instances[0];
    expect(fake?.name).toBe(AUTH_CHANNEL_NAME);
    channel.postSignedOut();
    expect(fake?.posted).toEqual([{ type: "signed-out" }]);
    fake?.onmessage?.(new MessageEvent("message", { data: { type: "other" } }));
    expect(onSignedOut).not.toHaveBeenCalled();
    fake?.onmessage?.(new MessageEvent("message", { data: { type: "signed-out" } }));
    expect(onSignedOut).toHaveBeenCalledTimes(1);
    channel.close();
    expect(fake?.closed).toBe(true);
    expect(fake?.onmessage).toBeNull();
  });
});
