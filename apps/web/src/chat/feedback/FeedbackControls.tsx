// Helpful and Not helpful (10-web-client.md section 8.16): toggles that show only the rating the
// API confirmed. Helpful saves at once; Not helpful and the comment link open the dialog. Each
// `200` is written into the session's cached turns, so a reopened or refetched turn shows it.

import {
  Link,
  Spinner,
  ToggleButton,
  Tooltip,
  makeStyles,
  tokens,
  useRestoreFocusTarget,
} from "@fluentui/react-components";
import {
  ThumbDislike20Filled,
  ThumbDislike20Regular,
  ThumbLike20Filled,
  ThumbLike20Regular,
} from "@fluentui/react-icons";
import { useQueryClient, type InfiniteData, type QueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { announceStatus } from "../../a11y/announce";
import type { FeedbackRequest } from "../../api/generated/types.gen";
import { request } from "../../api/http";
import type { ResponseOf } from "../../api/operations";
import { apiErrorOf, clientError, isProblem, retryAtOf, type ApiError } from "../../api/problem";
import { queryKeys } from "../../api/queryClient";
import { en } from "../../copy/en";
import { useCurrentUser } from "../../shell/AppShell";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { useWaiting } from "../turns/time";
import type { StoredFeedback } from "../turns/turnModel";
import { FeedbackDialog, type FeedbackMode } from "./FeedbackDialog";

const useStyles = makeStyles({
  row: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalXS,
  },
  below: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    rowGap: tokens.spacingVerticalXS,
  },
});

/** Writes saved feedback into every cached copy of the turn: the session and its earlier pages. */
export function writeFeedback(
  client: QueryClient,
  sessionId: string,
  turnId: string,
  feedback: StoredFeedback,
): void {
  client.setQueryData<ResponseOf<"getSession">>(queryKeys.session(sessionId), (session) =>
    session === undefined
      ? session
      : {
          ...session,
          turns: session.turns.map((turn) =>
            turn.turnId === turnId ? { ...turn, feedback } : turn,
          ),
        },
  );
  client.setQueryData<InfiniteData<ResponseOf<"listSessionTurns">>>(
    queryKeys.sessionEarlier(sessionId),
    (data) =>
      data === undefined
        ? data
        : {
            ...data,
            pages: data.pages.map((page) => ({
              ...page,
              items: page.items.map((turn) =>
                turn.turnId === turnId ? { ...turn, feedback } : turn,
              ),
            })),
          },
  );
}

function isWriteLimit(error: ApiError): boolean {
  return isProblem(error, 429, "too_many_requests") || isProblem(error, 429, "write_rate_limited");
}

export interface FeedbackControlsProps {
  readonly sessionId: string;
  readonly turnId: string;
  /** The stored feedback, from the session read. */
  readonly stored: StoredFeedback | null;
  /** Copy answer, which shares the action row. */
  readonly children?: ReactNode;
}

export function FeedbackControls({ sessionId, turnId, stored, children }: FeedbackControlsProps) {
  const styles = useStyles();
  const client = useQueryClient();
  const user = useCurrentUser();
  const [saved, setSaved] = useState<StoredFeedback | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  // The dialog stays mounted while it closes, so Fluent ends its modal state itself; each opening
  // mounts a fresh one, which starts from the saved feedback.
  const [dialog, setDialog] = useState<{ mode: FeedbackMode; open: boolean; key: number } | null>(
    null,
  );
  const openDialog = (mode: FeedbackMode) => {
    setDialog((current) => ({ mode, open: true, key: (current?.key ?? 0) + 1 }));
  };
  const closeDialog = () => {
    setDialog((current) => (current === null ? null : { ...current, open: false }));
  };
  const confirmed = saved ?? stored;
  const waiting = useWaiting(error !== null && isWriteLimit(error) ? retryAtOf(error) : null);
  const commentsEnabled = user.features.feedbackComments;
  // The dialog has no DialogTrigger, so the controls that open it are marked as the place Fluent
  // returns focus to when it closes (10-web-client.md section 11.4).
  const restoreFocusTarget = useRestoreFocusTarget();

  const send = async (body: FeedbackRequest): Promise<void> => {
    const feedback = await request("putTurnFeedback", { path: { sessionId, turnId }, body });
    setSaved(feedback);
    writeFeedback(client, sessionId, turnId, feedback);
    announceStatus(en["feedback.saved"]);
  };

  const helpful = () => {
    if (confirmed?.rating === "up") return;
    setPending(true);
    setError(null);
    send({ rating: "up", category: null, comment: null }).then(
      () => {
        setPending(false);
      },
      (reason: unknown) => {
        setPending(false);
        setError(apiErrorOf(reason) ?? clientError("network"));
      },
    );
  };

  const disabled = pending || waiting;
  return (
    <>
      <div className={styles.row}>
        <Tooltip content={en["chat.turn.helpful"]} relationship="label">
          <ToggleButton
            appearance="subtle"
            checked={confirmed?.rating === "up"}
            disabled={disabled}
            icon={confirmed?.rating === "up" ? <ThumbLike20Filled /> : <ThumbLike20Regular />}
            onClick={helpful}
          />
        </Tooltip>
        <Tooltip content={en["chat.turn.notHelpful"]} relationship="label">
          <ToggleButton
            appearance="subtle"
            checked={confirmed?.rating === "down"}
            disabled={disabled}
            icon={
              confirmed?.rating === "down" ? <ThumbDislike20Filled /> : <ThumbDislike20Regular />
            }
            {...restoreFocusTarget}
            onClick={() => {
              setError(null);
              openDialog("down");
            }}
          />
        </Tooltip>
        {pending ? <Spinner size="tiny" aria-label={en["feedback.sending"]} /> : null}
        {children}
      </div>
      <div className={styles.below}>
        {confirmed?.rating === "up" && commentsEnabled ? (
          <Link
            as="button"
            {...restoreFocusTarget}
            onClick={() => {
              openDialog("up");
            }}
          >
            {confirmed.comment === null ? en["feedback.addComment"] : en["feedback.editComment"]}
          </Link>
        ) : null}
        {error === null ? null : <ApiErrorBar error={error} />}
      </div>
      {dialog === null ? null : (
        <FeedbackDialog
          key={dialog.key}
          open={dialog.open}
          mode={dialog.mode}
          saved={confirmed}
          commentsEnabled={commentsEnabled}
          onSubmit={async (body) => {
            await send(body);
            closeDialog();
          }}
          onClose={closeDialog}
          onCommentsDisabled={() => void client.invalidateQueries({ queryKey: queryKeys.me })}
        />
      )}
    </>
  );
}
