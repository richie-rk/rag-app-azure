// The conversation (10-web-client.md sections 8.1 and 8.13): a `section` holding one `article` per
// turn, oldest first, with Show earlier messages at the top and Jump to latest at the bottom
// right. It scrolls to the newest turn when a session opens, keeps the visible content still when
// earlier turns are inserted above, and follows a streaming answer only while the user is within
// 120 px of the bottom.

import { Button, Skeleton, SkeletonItem, makeStyles, tokens } from "@fluentui/react-components";
import { ArrowDown20Regular } from "@fluentui/react-icons";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

import type { ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { FOLLOW_THRESHOLD_PX } from "../../platform/constants";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { useMediaQuery } from "../../ui/media";
import { TurnArticle, type TurnArticleProps } from "./TurnArticle";
import type { TurnModel } from "./turnModel";

/** The top bar's height, which the list's scroll padding keeps clear (WCAG 2.4.11). */
const TOP_BAR_PX = 48;
const COMPOSER_GAP_PX = 16;

const useStyles = makeStyles({
  frame: { position: "relative", flexGrow: 1, minHeight: 0, display: "flex" },
  scroller: { flexGrow: 1, overflowY: "auto", minWidth: 0 },
  column: {
    maxWidth: "860px",
    marginLeft: "auto",
    marginRight: "auto",
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalXL,
    paddingTop: tokens.spacingVerticalM,
    paddingBottom: tokens.spacingVerticalM,
  },
  earlier: { alignSelf: "center" },
  skeleton: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  jump: {
    position: "absolute",
    right: tokens.spacingHorizontalL,
    bottom: tokens.spacingVerticalM,
    boxShadow: tokens.shadow8,
  },
});

export interface EarlierTurns {
  /** `earlierTurnsCursor` or the last page's `nextCursor` isn't null. */
  readonly available: boolean;
  readonly loading: boolean;
  readonly error: ApiError | null;
  readonly load: () => void;
}

type TurnHandlers = Pick<TurnArticleProps, "onRetry" | "onNewChat" | "onCitation" | "pollSession">;

export interface ConversationProps extends TurnHandlers {
  /** The open session, or null for a new chat; scrolling restarts when it changes. */
  readonly sessionId: string | null;
  readonly turns: readonly TurnModel[];
  /** `getSession` hasn't answered yet: three turn blocks of skeleton. */
  readonly loading: boolean;
  /** `getSession` failed other than by `404`: the error bar with Retry. */
  readonly loadError: ApiError | null;
  readonly onReload: () => void;
  readonly earlier: EarlierTurns;
  /** Shown in place of the turns: the empty state of a new chat. */
  readonly empty: ReactNode;
  readonly sendingBlocked: boolean;
  /** The composer's height, for the list's bottom scroll padding. */
  readonly composerHeight: number;
}

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_THRESHOLD_PX;
}

export function Conversation({
  sessionId,
  turns,
  loading,
  loadError,
  onReload,
  earlier,
  empty,
  sendingBlocked,
  composerHeight,
  ...handlers
}: ConversationProps) {
  const styles = useStyles();
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const scroller = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const opened = useRef<string | null | undefined>(undefined);
  const insertion = useRef<{ firstKey: string | undefined; height: number; top: number } | null>(
    null,
  );
  const firstKey = turns[0]?.key;
  const streaming = turns.some((turn) => turn.streaming);
  const signature = turns
    .map((turn) => `${turn.key}:${String(turn.text.length)}:${turn.outcome.kind}`)
    .join("|");

  useLayoutEffect(() => {
    const element = scroller.current;
    if (element === null) return;
    const kept = insertion.current;
    if (kept !== null && kept.firstKey !== firstKey) {
      // Earlier turns went in above: the offset grows by their height, so nothing visible moves.
      element.scrollTop = kept.top + (element.scrollHeight - kept.height);
      insertion.current = null;
      return;
    }
    if (opened.current !== sessionId && turns.length > 0) {
      opened.current = sessionId;
      follow.current = true;
    }
    if (follow.current) element.scrollTop = element.scrollHeight;
  }, [sessionId, firstKey, signature, turns.length]);

  const loadEarlier = () => {
    const element = scroller.current;
    if (element !== null) {
      insertion.current = { firstKey, height: element.scrollHeight, top: element.scrollTop };
    }
    earlier.load();
  };

  const jumpToLatest = () => {
    const element = scroller.current;
    follow.current = true;
    setAtBottom(true);
    element?.scrollTo({ top: element.scrollHeight, behavior: reducedMotion ? "auto" : "smooth" });
  };

  let content: ReactNode;
  if (loading) {
    content = (
      <Skeleton className={styles.skeleton}>
        {[0, 1, 2].map((index) => (
          <SkeletonItem key={index} size={72} />
        ))}
      </Skeleton>
    );
  } else if (loadError !== null) {
    content = <ApiErrorBar error={loadError} onRetry={onReload} />;
  } else if (turns.length === 0) {
    content = empty;
  } else {
    content = turns.map((turn, index) => (
      <TurnArticle
        key={turn.key}
        model={turn}
        newest={index === turns.length - 1}
        sendingBlocked={sendingBlocked}
        {...handlers}
      />
    ));
  }

  return (
    <div className={styles.frame}>
      <div
        ref={scroller}
        className={styles.scroller}
        style={{
          scrollPaddingTop: `${String(TOP_BAR_PX)}px`,
          scrollPaddingBottom: `${String(composerHeight + COMPOSER_GAP_PX)}px`,
        }}
        onScroll={(event) => {
          const near = isNearBottom(event.currentTarget);
          follow.current = near;
          setAtBottom(near);
        }}
      >
        <section aria-label={en["chat.conversation.label"]} className={styles.column}>
          {earlier.error === null ? null : (
            <ApiErrorBar
              error={earlier.error}
              lines={[en["chat.earlier.error"]]}
              onRetry={loadEarlier}
            />
          )}
          {earlier.available && !loading && loadError === null ? (
            <Button className={styles.earlier} disabled={earlier.loading} onClick={loadEarlier}>
              {en["chat.earlier.load"]}
            </Button>
          ) : null}
          {content}
        </section>
      </div>
      {streaming && !atBottom ? (
        <Button className={styles.jump} icon={<ArrowDown20Regular />} onClick={jumpToLatest}>
          {en["chat.jumpToLatest"]}
        </Button>
      ) : null}
    </div>
  );
}
