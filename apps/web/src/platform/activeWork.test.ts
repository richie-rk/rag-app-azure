import { act, renderHook } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import {
  abortAllWork,
  hasActiveWork,
  installUnloadGuard,
  registerActiveWork,
  subscribeActiveWork,
  useHasActiveWork,
} from "./activeWork";

describe("active work", () => {
  test("work registers until it ends, and a sign-out aborts it all", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeActiveWork(listener);
    const first = vi.fn();
    const second = vi.fn();
    const endFirst = registerActiveWork({ abort: first });
    const endSecond = registerActiveWork({ abort: second });
    expect(hasActiveWork()).toBe(true);
    abortAllWork();
    expect(first).toHaveBeenCalled();
    expect(second).toHaveBeenCalled();
    endFirst();
    endFirst();
    endSecond();
    expect(hasActiveWork()).toBe(false);
    expect(listener).toHaveBeenCalledTimes(4);
    unsubscribe();
  });

  test("whether work is running is React state", () => {
    const { result } = renderHook(() => useHasActiveWork());
    expect(result.current).toBe(false);
    let end: () => void = () => undefined;
    act(() => {
      end = registerActiveWork({ abort: () => undefined });
    });
    expect(result.current).toBe(true);
    act(() => {
      end();
    });
    expect(result.current).toBe(false);
  });

  test("leaving the page asks for confirmation only while work runs", () => {
    const remove = installUnloadGuard(window);
    const idle = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(idle);
    expect(idle.defaultPrevented).toBe(false);
    const end = registerActiveWork({ abort: () => undefined });
    const busy = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(busy);
    expect(busy.defaultPrevented).toBe(true);
    end();
    remove();
    const removed = new Event("beforeunload", { cancelable: true });
    registerActiveWork({ abort: () => undefined })();
    window.dispatchEvent(removed);
    expect(removed.defaultPrevented).toBe(false);
  });
});
