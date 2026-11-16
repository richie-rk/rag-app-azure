// One turn of the conversation (10-web-client.md sections 8.1, 8.11, 8.12 and 8.16): an `article`
// labelled by its question, holding the question, the progress line, the answer, the Sources list,
// the outcome's caption or error bar, and the action row with Helpful, Not helpful and Copy
// answer. Every text from the API or the model, except the answer, renders as a text node.

import {
  Button,
  Caption1,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useId, type ReactNode } from "react";

import { isProblem, retryAtOf } from "../../api/problem";
import { en } from "../../copy/en";
import { turnErrorText } from "../../copy/turnErrorTexts";
import { ErrorBar, errorText } from "../../ui/ErrorBar";
import { fillText, formatRetryTime } from "../../ui/format";
import { SourcesList } from "../citations/SourcesList";
import { FeedbackControls } from "../feedback/FeedbackControls";
import { AnswerBody } from "../render/AnswerBody";
import type { Citation } from "../stream/events";
import type { TurnFailure } from "../stream/turnStore";
import { CopyAnswer } from "./CopyAnswer";
import { StreamingCaption } from "./StreamingCaption";
import { useWaiting } from "./time";
import type { TurnModel } from "./turnModel";

const useStyles = makeStyles({
  article: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
  question: {
    alignSelf: "flex-end",
    maxWidth: "85%",
    padding: `${tokens.spacingVerticalS} ${tokens.spacingHorizontalM}`,
    borderRadius: tokens.borderRadiusLarge,
    backgroundColor: tokens.colorNeutralBackground3,
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalXXS,
  },
  questionText: { margin: "0", whiteSpace: "pre-wrap", overflowWrap: "anywhere" },
  answer: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
  label: { color: tokens.colorNeutralForeground2 },
  progress: { display: "flex", alignItems: "center", columnGap: tokens.spacingHorizontalS },
  caption: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalM,
    color: tokens.colorNeutralForeground2,
  },
  actions: { display: "flex", flexWrap: "wrap", alignItems: "center" },
});

const STREAM_TEXT: Readonly<Record<string, string>> = {
  stream_incomplete: en["chat.error.cutOff"],
  stream_stalled: en["chat.error.stalled"],
  stream_unreadable: en["chat.error.unreadable"],
  stream_interrupted: en["chat.error.network"],
};

/** The title, body and lines of a live failure (sections 8.6 and 8.12). */
export function liveFailureText(
  failure: TurnFailure,
): { title: string; body: string; lines: string[] } | null {
  if (failure.kind === "event") {
    return {
      title: turnErrorText(failure.code)?.title ?? en["chat.error.title"],
      body: failure.message ?? "",
      lines: retryLines(failure.retryAt),
    };
  }
  if (failure.kind === "stream") {
    return {
      title: en["chat.error.title"],
      body: STREAM_TEXT[failure.code ?? ""] ?? en["chat.error.network"],
      lines: [],
    };
  }
  const error = failure.error;
  if (error === null) return null;
  const text =
    error.kind === "network"
      ? { title: en["chat.error.title"], body: en["chat.error.network"] }
      : errorText(error);
  return text === null ? null : { ...text, lines: retryLines(retryAtOf(error)) };
}

/** `error.retryAt` with the time a refused request may be repeated, when there is one. */
function retryLines(retryAt: number | null): string[] {
  return retryAt === null
    ? []
    : [fillText(en["error.retryAt"], { time: formatRetryTime(retryAt) })];
}

export interface TurnArticleProps {
  readonly model: TurnModel;
  /** The session's newest turn: Retry and the streaming poll apply only to it. */
  readonly newest: boolean;
  /** Sending is blocked: Retry waits too. */
  readonly sendingBlocked: boolean;
  readonly onRetry: (model: TurnModel) => void;
  readonly onNewChat: () => void;
  readonly onCitation: (model: TurnModel, citation: Citation, button: HTMLElement) => void;
  /** Refetches the open session for the streaming-turn poll. */
  readonly pollSession: () => Promise<boolean>;
}

export function TurnArticle({
  model,
  newest,
  sendingBlocked,
  onRetry,
  onNewChat,
  onCitation,
  pollSession,
}: TurnArticleProps) {
  const styles = useStyles();
  const questionId = useId();
  const retryWaiting = useWaiting(model.retry?.retryAt ?? null);
  const retryButton =
    newest && model.retry !== null ? (
      <Button
        disabled={retryWaiting || sendingBlocked}
        onClick={() => {
          onRetry(model);
        }}
      >
        {en["chat.turn.retry"]}
      </Button>
    ) : null;

  const openCitation = (citationNumber: number, button: HTMLElement) => {
    const citation = model.citations?.find((item) => item.number === citationNumber);
    if (citation !== undefined) onCitation(model, citation, button);
  };

  let outcome: ReactNode = null;
  switch (model.outcome.kind) {
    case "truncated":
      outcome = <Caption1 className={styles.caption}>{en["chat.turn.truncated"]}</Caption1>;
      break;
    case "stopped":
      outcome = (
        <div className={styles.caption}>
          <Caption1>
            {model.outcome.stored ? en["chat.turn.stoppedStored"] : en["chat.turn.stopped"]}
          </Caption1>
          {retryButton}
        </div>
      );
      break;
    case "liveFailure": {
      const failure = model.outcome.failure;
      const text = liveFailureText(failure);
      const turnLimit = isProblem(failure.error, 409, "session_turn_limit");
      if (text !== null) {
        outcome = (
          <ErrorBar
            title={text.title}
            body={text.body}
            lines={text.lines}
            referenceId={failure.referenceId}
            actions={
              turnLimit ? (
                <Button onClick={onNewChat}>{en["chat.unavailable.action"]}</Button>
              ) : (
                (retryButton ?? undefined)
              )
            }
          />
        );
      }
      break;
    }
    case "storedFailure": {
      const known = turnErrorText(model.outcome.code);
      outcome = (
        <MessageBar intent="error" layout="multiline">
          <MessageBarBody>
            <MessageBarTitle>{known?.title ?? en["chat.turn.failedStored"]}</MessageBarTitle>
            {known?.text}
          </MessageBarBody>
          {retryButton === null ? null : <MessageBarActions>{retryButton}</MessageBarActions>}
        </MessageBar>
      );
      break;
    }
    case "inProgress":
      outcome = <StreamingCaption polls={newest} poll={pollSession} />;
      break;
    case "none":
      break;
  }

  const copy = model.copyable ? (
    <CopyAnswer text={model.text} citations={model.citations} allowedLinks={model.allowedLinks} />
  ) : null;
  let actions: ReactNode = null;
  if (model.feedback !== null && model.sessionId !== null && model.turnId !== null) {
    actions = (
      <FeedbackControls
        sessionId={model.sessionId}
        turnId={model.turnId}
        stored={model.feedback.stored}
      >
        {copy}
      </FeedbackControls>
    );
  } else if (copy !== null) {
    actions = <div className={styles.actions}>{copy}</div>;
  }

  return (
    <article aria-labelledby={questionId} className={styles.article}>
      <div className={styles.question}>
        <Text size={200} className={styles.label}>
          {en["chat.turn.youAsked"]}
        </Text>
        <p id={questionId} className={styles.questionText}>
          {model.question}
        </p>
      </div>
      <div className={styles.answer}>
        <Text size={200} weight="semibold" className={styles.label}>
          {en["chat.turn.answerLabel"]}
        </Text>
        {model.progress === null ? null : (
          <div className={styles.progress}>
            {/* The line's text is the spinner's label, so the progress bar is named by it. */}
            <Spinner
              size="tiny"
              label={
                model.progress === "broadened"
                  ? en["chat.turn.broadened"]
                  : en["chat.turn.searching"]
              }
            />
          </div>
        )}
        {model.text === "" ? null : (
          <AnswerBody
            text={model.text}
            citations={model.citations}
            allowedLinks={model.allowedLinks}
            streaming={model.streaming}
            onCitation={openCitation}
          />
        )}
        {model.citations === null || model.turnId === null ? null : (
          <SourcesList
            citations={model.citations}
            onOpen={(citation, button) => {
              onCitation(model, citation, button);
            }}
          />
        )}
        {outcome}
        {actions}
      </div>
    </article>
  );
}
