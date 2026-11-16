// The one component that inserts HTML (10-web-client.md sections 9.1 and 9.6): the sanitised
// answer string, memoised on the text, the citations and the allowlist. While a turn streams the
// pipeline runs at most once per 100 ms; at once on citations, the outcome and a stop. One click
// listener on the container opens the viewer for a citation button. No `aria-live` sits here,
// because a region that re-renders would be read over and over.

import { makeStyles, mergeClasses, tokens } from "@fluentui/react-components";
import { useEffect, useMemo, useRef, useState } from "react";

import { RENDER_THROTTLE_MS } from "../../platform/constants";
import { answerView } from "./pipeline";
import type { CitationLabel } from "./policy";

const useStyles = makeStyles({
  root: {
    overflowWrap: "anywhere",
    lineHeight: tokens.lineHeightBase400,
    "& p": { marginTop: "0", marginBottom: tokens.spacingVerticalS },
    "& h3, & h4, & h5, & h6": {
      marginTop: tokens.spacingVerticalM,
      marginBottom: tokens.spacingVerticalS,
      fontWeight: tokens.fontWeightSemibold,
    },
    "& ul, & ol": { marginTop: "0", marginBottom: tokens.spacingVerticalS, paddingLeft: "24px" },
    "& blockquote": {
      marginLeft: "0",
      paddingLeft: tokens.spacingHorizontalM,
      borderLeft: `${tokens.strokeWidthThick} solid ${tokens.colorNeutralStroke1}`,
      color: tokens.colorNeutralForeground2,
    },
    "& pre": {
      overflowX: "auto",
      padding: tokens.spacingHorizontalS,
      backgroundColor: tokens.colorNeutralBackground3,
      borderRadius: tokens.borderRadiusMedium,
    },
    "& code": { fontFamily: tokens.fontFamilyMonospace },
    "& div[role=region]": { overflowX: "auto", marginBottom: tokens.spacingVerticalS },
    "& table": { borderCollapse: "collapse" },
    "& th, & td": {
      border: `${tokens.strokeWidthThin} solid ${tokens.colorNeutralStroke1}`,
      padding: `${tokens.spacingVerticalXS} ${tokens.spacingHorizontalS}`,
    },
    "& a": { color: tokens.colorBrandForegroundLink },
    '& a[target="_blank"]::after': { content: '" ↗" / ""' },
    "& span[data-link-host]": {
      color: tokens.colorNeutralForeground3,
      unicodeBidi: "isolate",
      direction: "ltr",
    },
    "& button[data-citation]": {
      minWidth: "20px",
      padding: `0 ${tokens.spacingHorizontalXXS}`,
      border: `${tokens.strokeWidthThin} solid ${tokens.colorBrandStroke1}`,
      borderRadius: tokens.borderRadiusSmall,
      backgroundColor: tokens.colorBrandBackground2,
      color: tokens.colorBrandForeground2,
      fontSize: tokens.fontSizeBase100,
      lineHeight: tokens.lineHeightBase100,
      cursor: "pointer",
    },
    "& button[data-citation]:focus-visible": {
      outline: `${tokens.strokeWidthThick} solid ${tokens.colorStrokeFocus2}`,
    },
    "@media (forced-colors: active)": {
      "& button[data-citation]": {
        border: "1px solid ButtonText",
        backgroundColor: "ButtonFace",
        color: "ButtonText",
      },
      "& span[data-link-host]": { color: "GrayText" },
      "& li::marker": { color: "CanvasText" },
    },
  },
  plain: { whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: "0" },
});

export interface AnswerBodyProps {
  /** The answer text with its citation tokens. */
  readonly text: string;
  /** The turn's citations; null until they arrive. */
  readonly citations: readonly CitationLabel[] | null;
  /** The turn's allowed links; null until they arrive. */
  readonly allowedLinks: readonly string[] | null;
  /** While true the pipeline runs at most every 100 ms. */
  readonly streaming: boolean;
  /** A citation button was activated. */
  readonly onCitation?: (citationNumber: number, button: HTMLElement) => void;
  readonly className?: string;
}

/** The text to render: `text` itself unless the turn streams, then the text as of the last run,
 * moved on at most once per 100 ms (trailing edge). */
function useThrottledText(text: string, streaming: boolean): string {
  const [shown, setShown] = useState(text);
  const lastRun = useRef(Number.NEGATIVE_INFINITY);
  useEffect(() => {
    if (!streaming || shown === text) return;
    const wait = Math.max(0, lastRun.current + RENDER_THROTTLE_MS - Date.now());
    const timer = setTimeout(() => {
      lastRun.current = Date.now();
      setShown(text);
    }, wait);
    return () => {
      clearTimeout(timer);
    };
  }, [text, streaming, shown]);
  return streaming ? shown : text;
}

export function AnswerBody({
  text,
  citations,
  allowedLinks,
  streaming,
  onCitation,
  className,
}: AnswerBodyProps) {
  const styles = useStyles();
  const ref = useRef<HTMLDivElement>(null);
  const shownText = useThrottledText(text, streaming);
  const view = useMemo(
    () => answerView(shownText, { citations, allowedLinks }),
    [shownText, citations, allowedLinks],
  );
  const onCitationRef = useRef(onCitation);
  useEffect(() => {
    onCitationRef.current = onCitation;
  }, [onCitation]);

  useEffect(() => {
    const container = ref.current;
    const onClick = (event: MouseEvent) => {
      // Clicks reach the container only from the elements inside it.
      const button = (event.target as Element).closest<HTMLElement>("button[data-citation]");
      if (button === null) return;
      onCitationRef.current?.(Number(button.dataset["citation"]), button);
    };
    container?.addEventListener("click", onClick);
    return () => {
      container?.removeEventListener("click", onClick);
    };
  }, []);

  if (view.kind === "plain") {
    return (
      <div ref={ref} className={mergeClasses(styles.plain, className)}>
        {view.text}
      </div>
    );
  }
  return (
    <div
      ref={ref}
      className={mergeClasses(styles.root, className)}
      dangerouslySetInnerHTML={{ __html: view.html }}
    />
  );
}
