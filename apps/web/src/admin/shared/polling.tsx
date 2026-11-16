// Polling on the admin screens (10-web-client.md section 7.11):
//
// 1. a polled query's `refetchInterval` is the interval while its condition holds and Auto-refresh
//    is on, otherwise `false`; polling pauses while the tab is hidden;
// 2. the Auto-refresh switch is on by default and not remembered (WCAG 2.2.2);
// 3. a failed poll keeps the last data and shows `admin.poll.stale` with the last success's time;
// 4. 3 failures in a row stop polling, and `admin.poll.stopped` offers Refresh, which refetches once
//    and restarts polling when that succeeds.
//
// Use: `const polling = usePolling(POLL_LIST_MS)`; give the query
// `refetchInterval: (query) => polling.interval(<condition on query.state.data>)` and
// `refetchIntervalInBackground: false`; call `usePollWatch(polling, result)`; render
// `<AutoRefreshSwitch polling={polling} />` while the condition holds and
// `<PollStatusBar polling={polling} result={result} />`.

import {
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  Switch,
} from "@fluentui/react-components";
import type { UseQueryResult } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { en } from "../../copy/en";
import { POLL_MAX_FAILURES } from "../../platform/constants";
import { fillText, formatTime } from "../../ui/format";

export interface Polling {
  readonly autoRefresh: boolean;
  readonly setAutoRefresh: (on: boolean) => void;
  /** Consecutive failed polls; a success resets it. */
  readonly failures: number;
  /** 3 failures in a row: polling has stopped until Refresh succeeds. */
  readonly stopped: boolean;
  /** The `refetchInterval` while `condition` holds. */
  readonly interval: (condition: boolean) => number | false;
  readonly recordFailure: () => void;
  readonly recordSuccess: () => void;
}

export function usePolling(intervalMs: number): Polling {
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [failures, setFailures] = useState(0);
  const stopped = failures >= POLL_MAX_FAILURES;
  const live = autoRefresh && !stopped;
  const interval = useCallback(
    (condition: boolean) => (live && condition ? intervalMs : false),
    [live, intervalMs],
  );
  const recordFailure = useCallback(() => {
    setFailures((count) => count + 1);
  }, []);
  const recordSuccess = useCallback(() => {
    setFailures(0);
  }, []);
  return { autoRefresh, setAutoRefresh, failures, stopped, interval, recordFailure, recordSuccess };
}

/** The query fields polling watches. */
export type PolledResult = Pick<
  UseQueryResult,
  "data" | "dataUpdatedAt" | "errorUpdateCount" | "isError" | "refetch"
>;

/** Counts the polled query's consecutive failures: each failed fetch after data exists is a
 * failed poll; each success resets the count. */
export function usePollWatch(polling: Polling, result: PolledResult): void {
  const { recordFailure, recordSuccess } = polling;
  const seenErrors = useRef(result.errorUpdateCount);
  useEffect(() => {
    if (result.errorUpdateCount === seenErrors.current) return;
    seenErrors.current = result.errorUpdateCount;
    if (result.data !== undefined) recordFailure();
  }, [result.errorUpdateCount, result.data, recordFailure]);
  useEffect(() => {
    if (result.dataUpdatedAt > 0) recordSuccess();
  }, [result.dataUpdatedAt, recordSuccess]);
}

/** The Auto-refresh switch (`admin.autoRefresh`), shown while the screen polls. */
export function AutoRefreshSwitch({ polling }: { readonly polling: Polling }) {
  return (
    <Switch
      label={en["admin.autoRefresh"]}
      checked={polling.autoRefresh}
      onChange={(_, data) => {
        polling.setAutoRefresh(data.checked);
      }}
    />
  );
}

/** A failed poll: the stale bar with the last success's time, or, once polling stopped, the
 * stopped bar with Refresh. Nothing while the last poll succeeded. */
export function PollStatusBar({
  polling,
  result,
}: {
  readonly polling: Polling;
  readonly result: PolledResult;
}) {
  if (result.data === undefined || !result.isError || polling.failures === 0) return null;
  if (polling.stopped) {
    return (
      <MessageBar intent="warning" layout="multiline">
        <MessageBarBody>{en["admin.poll.stopped"]}</MessageBarBody>
        <MessageBarActions>
          <Button
            onClick={() => {
              void result.refetch();
            }}
          >
            {en["admin.poll.refresh"]}
          </Button>
        </MessageBarActions>
      </MessageBar>
    );
  }
  return (
    <MessageBar intent="warning" layout="multiline">
      <MessageBarBody>
        {fillText(en["admin.poll.stale"], { time: formatTime(result.dataUpdatedAt) })}
      </MessageBarBody>
    </MessageBar>
  );
}
