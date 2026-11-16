// The two polite live regions (10-web-client.md section 11.2), rendered once at the app root and
// present and empty from the first paint. Fluent's AriaLiveAnnouncer isn't used, because without
// `document.ariaNotify` it falls back to an assertive region.

import { makeStyles } from "@fluentui/react-components";
import { useEffect, useRef } from "react";

import { registerLiveRegions } from "./announce";

export const useVisuallyHiddenStyles = makeStyles({
  root: {
    position: "absolute",
    width: "1px",
    height: "1px",
    margin: "-1px",
    padding: "0",
    border: "0",
    overflow: "hidden",
    clipPath: "inset(50%)",
    whiteSpace: "nowrap",
  },
});

export function LiveRegions() {
  const styles = useVisuallyHiddenStyles();
  const logRef = useRef<HTMLDivElement>(null);
  const statusRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const log = logRef.current;
    const status = statusRef.current;
    if (log === null || status === null) return;
    return registerLiveRegions(log, status);
  }, []);

  return (
    <>
      <div ref={logRef} className={styles.root} role="log" aria-live="polite" />
      <div
        ref={statusRef}
        className={styles.root}
        role="status"
        aria-live="polite"
        aria-atomic="true"
      />
    </>
  );
}
