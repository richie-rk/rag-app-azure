// Every error the client shows (10-web-client.md sections 12.1 and 12.3): a Fluent MessageBar
// with intent `error`, the title, the body and the client's added lines, the reference ID with its
// copy button, and the actions. Errors are inline, never toasts, because a toast can vanish before
// it is read.

import {
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { ReactNode } from "react";

import { announceStatus } from "../a11y/announce";
import { retryAtOf, type ApiError } from "../api/problem";
import { en } from "../copy/en";
import { fillText, formatRetryTime } from "./format";

const useStyles = makeStyles({
  body: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalXS },
  reference: {
    display: "flex",
    alignItems: "center",
    flexWrap: "wrap",
    columnGap: tokens.spacingHorizontalS,
  },
  monospace: { fontFamily: tokens.fontFamilyMonospace },
});

export interface ErrorBarProps {
  readonly title: string;
  readonly body?: string | null;
  /** The client's added lines: the retry time, next steps (section 12.3 rule 1). */
  readonly lines?: readonly string[];
  /** Shown with its copy button; null when no request failed (configuration, clipboard, files). */
  readonly referenceId?: string | null;
  /** Retry and the other actions a screen lists, in `MessageBarActions`. */
  readonly actions?: ReactNode;
}

/** The reference ID line: `error.reference` in a monospace font, and a button that copies it. */
export function ReferenceIdLine({ referenceId }: { readonly referenceId: string }) {
  const styles = useStyles();
  const copy = () => {
    navigator.clipboard.writeText(referenceId).then(
      () => {
        announceStatus(en["error.referenceCopied"]);
      },
      () => undefined,
    );
  };
  return (
    <span className={styles.reference}>
      <span className={styles.monospace}>
        {fillText(en["error.reference"], { id: referenceId })}
      </span>
      <Button size="small" appearance="transparent" onClick={copy}>
        {en["error.copyReference"]}
      </Button>
    </span>
  );
}

export function ErrorBar({ title, body, lines = [], referenceId, actions }: ErrorBarProps) {
  const styles = useStyles();
  return (
    <MessageBar intent="error" layout="multiline">
      <MessageBarBody>
        <MessageBarTitle>{title}</MessageBarTitle>
        <span className={styles.body}>
          {body === undefined || body === null ? null : <span>{body}</span>}
          {lines.map((line) => (
            <span key={line}>{line}</span>
          ))}
          {referenceId === undefined || referenceId === null ? null : (
            <ReferenceIdLine referenceId={referenceId} />
          )}
        </span>
      </MessageBarBody>
      {actions === undefined ? null : <MessageBarActions>{actions}</MessageBarActions>}
    </MessageBar>
  );
}

export interface ErrorText {
  readonly title: string;
  readonly body: string;
}

/** The text of a failed call (section 12.3): a problem's own `title` and `detail`, the client's
 * text for every other kind, nothing for `aborted`. */
export function errorText(error: ApiError): ErrorText | null {
  switch (error.kind) {
    case "problem":
      return { title: error.title ?? en["error.server.title"], body: error.detail ?? "" };
    case "http":
      return { title: en["error.server.title"], body: en["error.server.body"] };
    case "network":
      return { title: en["error.network.title"], body: en["error.network.body"] };
    case "unexpected":
      return { title: en["error.unexpected.title"], body: en["error.unexpected.body"] };
    case "session_expired":
      return { title: en["session.expired.title"], body: en["error.sessionExpired"] };
    case "aborted":
      return null;
  }
}

export interface ApiErrorBarProps {
  readonly error: ApiError;
  /** Next-step lines a screen adds (`admin.conflict.body`, `memory.conflict`, …). */
  readonly lines?: readonly string[];
  readonly onRetry?: () => void;
  /** Retry stays disabled until a `retryAfterSeconds` time passes, where the screen says so. */
  readonly retryDisabled?: boolean;
  readonly actions?: ReactNode;
  /** For tests: the clock the retry time is formatted against. */
  readonly now?: number;
}

/** The error bar of a failed call: its text, `error.retryAt` when the answer carries
 * `retryAfterSeconds`, the screen's lines, the reference ID and Retry. */
export function ApiErrorBar({
  error,
  lines = [],
  onRetry,
  retryDisabled,
  actions,
  now,
}: ApiErrorBarProps) {
  const text = errorText(error);
  if (text === null) return null;
  const retryAt = retryAtOf(error);
  const allLines =
    retryAt === null
      ? lines
      : [fillText(en["error.retryAt"], { time: formatRetryTime(retryAt, now) }), ...lines];
  return (
    <ErrorBar
      title={text.title}
      body={text.body}
      lines={allLines}
      referenceId={error.referenceId}
      actions={
        onRetry === undefined && actions === undefined ? undefined : (
          <>
            {onRetry === undefined ? null : (
              <Button onClick={onRetry} disabled={retryDisabled === true}>
                {en["error.retry"]}
              </Button>
            )}
            {actions}
          </>
        )
      }
    />
  );
}
