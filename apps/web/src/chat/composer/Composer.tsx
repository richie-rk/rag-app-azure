// The composer (10-web-client.md section 8.5): a text area with a visually hidden label, growing
// from 1 to 8 lines, and Send, which Stop replaces while the session's newest turn hasn't reached
// `citations`. Enter sends; Shift, Alt or Ctrl with Enter inserts a line break; Escape stops the
// answer. Length counts Unicode code points, the API's unit. The text area stays enabled while an
// answer streams, and focus stays in it when a question is sent.

import { Button, Field, Textarea, makeStyles, tokens } from "@fluentui/react-components";
import { Send24Filled, Stop24Filled } from "@fluentui/react-icons";
import { useLayoutEffect, useRef, type KeyboardEvent } from "react";

import { useVisuallyHiddenStyles } from "../../a11y/LiveRegions";
import { en } from "../../copy/en";
import { QUESTION_COUNTER_FROM, QUESTION_MAX_CODE_POINTS } from "../../platform/constants";
import { codePointLength, fillText, formatRetryTime } from "../../ui/format";
import { useDraft } from "./drafts";

const MAX_LINES = 8;

const useStyles = makeStyles({
  root: { display: "flex", alignItems: "flex-end", columnGap: tokens.spacingHorizontalS },
  field: { flexGrow: 1, minWidth: 0 },
  textarea: { width: "100%" },
  input: { overflowY: "hidden", maxHeight: "none" },
});

const numbers = new Intl.NumberFormat("en-US");

export interface ComposerProps {
  /** The draft's key: the session, or `new:<projectId>`. */
  readonly draftKey: string;
  /** No project is chosen, the project isn't ready, or the projects didn't load. */
  readonly disabled: boolean;
  /** Sending is blocked: no project, a project that isn't ready, an answer before `citations`, or
   * a wait from a `429`. */
  readonly blocked: boolean;
  /** The wait's end, for `chat.composer.blockedUntil`. */
  readonly blockedUntil: number | null;
  /** The newest turn hasn't reached `citations`: Stop shows in Send's place. */
  readonly stoppable: boolean;
  readonly onSend: (question: string) => void;
  readonly onStop: () => void;
  /** Reports the composer's height, for the conversation's scroll padding. */
  readonly onHeight?: (height: number) => void;
}

/** Grows the text area with its content, from 1 line to 8, then lets it scroll. */
function fitToContent(element: HTMLTextAreaElement): void {
  const style = getComputedStyle(element);
  const lineHeight = Number.parseFloat(style.lineHeight);
  element.style.height = "auto";
  const content = element.scrollHeight;
  if (!Number.isFinite(lineHeight) || content === 0) return;
  const padding = Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom);
  const limit = lineHeight * MAX_LINES + (Number.isFinite(padding) ? padding : 0);
  element.style.height = `${String(Math.min(content, limit))}px`;
  element.style.overflowY = content > limit ? "auto" : "hidden";
}

export function Composer({
  draftKey,
  disabled,
  blocked,
  blockedUntil,
  stoppable,
  onSend,
  onStop,
  onHeight,
}: ComposerProps) {
  const styles = useStyles();
  const hidden = useVisuallyHiddenStyles();
  const draft = useDraft(draftKey);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const length = codePointLength(draft.text);
  const question = draft.text.trim();
  const tooLong = length > QUESTION_MAX_CODE_POINTS;
  const canSend = !disabled && !blocked && !tooLong && question !== "";

  useLayoutEffect(() => {
    if (textarea.current !== null) fitToContent(textarea.current);
    if (root.current !== null) onHeight?.(root.current.offsetHeight);
  }, [draft.text, onHeight]);

  const send = () => {
    if (!canSend) return;
    onSend(question);
    textarea.current?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Escape" && stoppable) {
      event.preventDefault();
      onStop();
      return;
    }
    if (event.key !== "Enter") return;
    // Section 8.5 names keyCode 229: some browsers mark an input method's Enter only that way.
    // eslint-disable-next-line @typescript-eslint/no-deprecated
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.shiftKey) return;
    event.preventDefault();
    if (event.altKey || event.ctrlKey) {
      // Neither inserts a line break by itself in a text area.
      const element = event.currentTarget;
      const start = element.selectionStart;
      const end = element.selectionEnd;
      draft.setText(`${draft.text.slice(0, start)}\n${draft.text.slice(end)}`);
      requestAnimationFrame(() => {
        element.setSelectionRange(start + 1, start + 1);
      });
      return;
    }
    send();
  };

  const messages: string[] = [];
  if (draft.reason !== null) messages.push(draft.reason);
  else if (tooLong) messages.push(en["chat.composer.tooLong"]);
  const hints = [en["chat.composer.hint"]];
  if (length >= QUESTION_COUNTER_FROM) {
    hints.push(fillText(en["chat.composer.counter"], { count: numbers.format(length) }));
  }
  if (blockedUntil !== null) {
    hints.push(fillText(en["chat.composer.blockedUntil"], { time: formatRetryTime(blockedUntil) }));
  }

  return (
    <div ref={root} className={styles.root}>
      <Field
        className={styles.field}
        label={{ children: en["chat.composer.label"], className: hidden.root }}
        hint={hints.join(" ")}
        {...(messages.length === 0
          ? {}
          : { validationState: "error" as const, validationMessage: messages.join(" ") })}
      >
        <Textarea
          className={styles.textarea}
          textarea={{ ref: textarea, className: styles.input }}
          resize="none"
          rows={1}
          disabled={disabled}
          placeholder={en["chat.composer.placeholder"]}
          value={draft.text}
          onChange={(_, data) => {
            draft.setText(data.value);
          }}
          onKeyDown={onKeyDown}
        />
      </Field>
      {stoppable ? (
        <Button
          appearance="primary"
          aria-label={en["chat.composer.stop"]}
          icon={<Stop24Filled />}
          onClick={onStop}
        />
      ) : (
        <Button
          appearance="primary"
          aria-label={en["chat.composer.send"]}
          icon={<Send24Filled />}
          disabled={!canSend}
          onClick={send}
        />
      )}
    </div>
  );
}
