// A stored turn that is still `streaming` (10-web-client.md section 8.12): another tab's answer,
// or a stream a page reload cut, which the API cancels within seconds. While it is the session's
// newest turn and this tab isn't streaming it, `getSession` is polled every 5,000 ms while the tab
// is visible and Auto-refresh is on, for at most 250 s; 3 failed polls in a row or the end of the
// 250 s turn the caption into the Refresh state. A poll that finds the turn finished renders the
// stored outcome, which unmounts this caption and ends the poll.

import { Button, Switch, Text, makeStyles, tokens } from "@fluentui/react-components";
import { useEffect, useRef, useState } from "react";

import { en } from "../../copy/en";
import { POLL_MAX_FAILURES, POLL_SESSION_MAX_MS, POLL_SESSION_MS } from "../../platform/constants";

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalXS,
    color: tokens.colorNeutralForeground2,
  },
});

export interface StreamingCaptionProps {
  /** Whether this caption polls: the session's newest turn, not streamed by this tab. */
  readonly polls: boolean;
  /** Refetches `getSession`; resolves true when the read succeeded. */
  readonly poll: () => Promise<boolean>;
}

export function StreamingCaption({ polls, poll }: StreamingCaptionProps) {
  const styles = useStyles();
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [long, setLong] = useState(false);
  const [round, setRound] = useState(0);
  const startedAt = useRef<number | null>(null);
  const pollRef = useRef(poll);
  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  useEffect(() => {
    if (!polls || !autoRefresh || long) return;
    startedAt.current ??= Date.now();
    const deadline = startedAt.current + POLL_SESSION_MAX_MS;
    let failures = 0;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (document.visibilityState === "visible") {
        const succeeded = await pollRef.current();
        if (!active) return;
        failures = succeeded ? 0 : failures + 1;
      }
      if (failures >= POLL_MAX_FAILURES || Date.now() >= deadline) {
        setLong(true);
        return;
      }
      timer = setTimeout(() => void tick(), POLL_SESSION_MS);
    };
    timer = setTimeout(() => void tick(), POLL_SESSION_MS);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [polls, autoRefresh, long, round]);

  const refresh = () => {
    void pollRef.current().then(() => {
      // Still streaming: poll for at most 250 s more.
      startedAt.current = Date.now();
      setLong(false);
      setRound((value) => value + 1);
    });
  };

  if (long) {
    return (
      <div className={styles.root}>
        <Text>{en["chat.turn.inProgressLong"]}</Text>
        <Button onClick={refresh}>{en["chat.turn.refresh"]}</Button>
      </div>
    );
  }
  return (
    <div className={styles.root}>
      <Text>{en["chat.turn.inProgress"]}</Text>
      {polls ? (
        <Switch
          label={en["chat.turn.autoRefresh"]}
          checked={autoRefresh}
          onChange={(_, data) => {
            setAutoRefresh(data.checked);
          }}
        />
      ) : null}
    </div>
  );
}
