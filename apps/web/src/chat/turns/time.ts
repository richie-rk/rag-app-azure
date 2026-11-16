// Waits that disable a control until a time passes (10-web-client.md sections 5.10, 8.6, 8.14 and
// 8.16): a refused request's `retryAfterSeconds`, a `409 turn_in_progress`, a `429` on a download
// or on feedback. The control re-renders when the time passes, without reading the clock while
// rendering.

import { useEffect, useState } from "react";

/** Whether `until` (epoch milliseconds) is still ahead; false for null. */
export function useWaiting(until: number | null): boolean {
  const [passed, setPassed] = useState<number | null>(null);
  useEffect(() => {
    if (until === null) return;
    const timer = setTimeout(
      () => {
        setPassed(until);
      },
      Math.max(0, until - Date.now()),
    );
    return () => {
      clearTimeout(timer);
    };
  }, [until]);
  return until !== null && passed !== until;
}
