// The wait after a refused write (10-web-client.md sections 5.7 and 5.10): a `429` shows the
// problem's text with the retry time, and the control that sent the request stays disabled until
// then. The memory screen and the admin screens share this hook, so it lives outside both.

import { useCallback, useEffect, useState } from "react";

import { retryAtOf, type ApiError } from "../api/problem";

export interface RetryWaits {
  /** Whether the control named `key` is waiting for its retry time. */
  readonly isWaiting: (key: string) => boolean;
  /** Starts the wait for `key` when `error` is a `429` that carries `retryAfterSeconds`. */
  readonly waitFor: (key: string, error: ApiError | null) => void;
}

/** The controls waiting for a retry time, keyed by the screen's own names; each re-enables when
 * its time passes. */
export function useRetryWaits(): RetryWaits {
  const [until, setUntil] = useState<ReadonlyMap<string, number>>(new Map());

  useEffect(() => {
    if (until.size === 0) return;
    const next = Math.min(...until.values());
    const timer = setTimeout(
      () => {
        const now = Date.now();
        setUntil((current) => new Map([...current].filter(([, at]) => at > now)));
      },
      Math.max(0, next - Date.now()),
    );
    return () => {
      clearTimeout(timer);
    };
  }, [until]);

  const isWaiting = useCallback(
    (key: string) => {
      const at = until.get(key);
      return at !== undefined && at > Date.now();
    },
    [until],
  );

  const waitFor = useCallback((key: string, error: ApiError | null) => {
    if (error?.status !== 429) return;
    const at = retryAtOf(error);
    if (at === null) return;
    setUntil((current) => new Map(current).set(key, at));
  }, []);

  return { isWaiting, waitFor };
}
