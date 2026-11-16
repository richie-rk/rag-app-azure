// The feedback dialog (10-web-client.md section 8.16): down mode asks for an optional problem type
// and comment, up mode for a comment only, because the API accepts a category only with a down
// rating. A blank comment is sent as null, which removes a saved comment. The dialog stays open
// with its choices on any failure.

import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Radio,
  RadioGroup,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useState } from "react";

import type { FeedbackCategory, FeedbackRequest } from "../../api/generated/types.gen";
import {
  apiErrorOf,
  clientError,
  isProblem,
  retryAtOf,
  splitFieldErrors,
  type ApiError,
} from "../../api/problem";
import { en } from "../../copy/en";
import { FEEDBACK_COMMENT_MAX_CODE_POINTS, FEEDBACK_COUNTER_SHARE } from "../../platform/constants";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { codePointLength, fillText } from "../../ui/format";
import { useWaiting } from "../turns/time";
import type { StoredFeedback } from "../turns/turnModel";

const useStyles = makeStyles({
  content: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
});

export type FeedbackMode = "up" | "down";

const CATEGORIES: readonly { readonly value: FeedbackCategory; readonly label: string }[] = [
  { value: "incorrect", label: en["feedback.category.incorrect"] },
  { value: "incomplete", label: en["feedback.category.incomplete"] },
  { value: "not_relevant", label: en["feedback.category.notRelevant"] },
  { value: "other", label: en["feedback.category.other"] },
];

const COUNTER_FROM = Math.ceil(FEEDBACK_COMMENT_MAX_CODE_POINTS * FEEDBACK_COUNTER_SHARE);

const numbers = new Intl.NumberFormat("en-US");

/** The text under a field for a `422` entry's reason (section 8.16). */
export function feedbackReasonText(reason: string): string {
  switch (reason) {
    case "category_requires_down":
      return en["feedback.reason.category_requires_down"];
    case "too_long":
      return fillText(en["feedback.reason.too_long"], {
        max: numbers.format(FEEDBACK_COMMENT_MAX_CODE_POINTS),
      });
    case "comments_disabled":
      return en["feedback.reason.comments_disabled"];
    default:
      return en["error.validation.field"];
  }
}

/** The first `max` code points of `value`: typing or pasting past the limit keeps the start, as
 * the HTML `maxlength` attribute, which counts UTF-16 code units, can't. */
export function firstCodePoints(value: string, max: number): string {
  return codePointLength(value) <= max ? value : Array.from(value).slice(0, max).join("");
}

export interface FeedbackDialogProps {
  readonly open: boolean;
  readonly mode: FeedbackMode;
  /** The saved feedback the dialog starts from, when its rating matches the mode. */
  readonly saved: StoredFeedback | null;
  readonly commentsEnabled: boolean;
  /** Sends the feedback; resolves on `200`, rejects with the call's failure. */
  readonly onSubmit: (body: FeedbackRequest) => Promise<void>;
  readonly onClose: () => void;
  /** A `422` named `comments_disabled`: the user query is refetched. */
  readonly onCommentsDisabled: () => void;
}

export function FeedbackDialog({
  open,
  mode,
  saved,
  commentsEnabled,
  onSubmit,
  onClose,
  onCommentsDisabled,
}: FeedbackDialogProps) {
  const styles = useStyles();
  const prefill = saved?.rating === mode ? saved : null;
  const [category, setCategory] = useState<FeedbackCategory | null>(prefill?.category ?? null);
  const [comment, setComment] = useState(prefill?.comment ?? "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const fields = error?.status === 422 ? splitFieldErrors(error, ["category", "comment"]) : null;
  const waitUntil =
    error !== null &&
    (isProblem(error, 429, "too_many_requests") || isProblem(error, 429, "write_rate_limited"))
      ? retryAtOf(error)
      : null;
  const waiting = useWaiting(waitUntil);
  const length = codePointLength(comment);

  const submit = () => {
    const trimmed = comment.trim();
    const body: FeedbackRequest = {
      rating: mode,
      category: mode === "down" ? category : null,
      comment: commentsEnabled && trimmed !== "" ? trimmed : null,
    };
    setPending(true);
    setError(null);
    onSubmit(body).then(
      () => {
        setPending(false);
      },
      (reason: unknown) => {
        const failure = apiErrorOf(reason) ?? clientError("network");
        setPending(false);
        setError(failure);
        if (
          failure.status === 422 &&
          failure.errors.some((entry) => entry.reason === "comments_disabled")
        ) {
          onCommentsDisabled();
        }
      },
    );
  };

  const categoryReason = fields?.byField.get("category");
  const commentReason = fields?.byField.get("comment");
  const barError =
    error !== null && (fields === null || fields.unmatched.length > 0) ? error : null;

  return (
    <Dialog
      open={open}
      modalType="modal"
      onOpenChange={(_, data) => {
        if (!data.open) onClose();
      }}
    >
      <DialogSurface>
        <DialogBody>
          <DialogTitle>
            {mode === "down" ? en["feedback.title"] : en["feedback.titleUp"]}
          </DialogTitle>
          <DialogContent className={styles.content}>
            {mode === "down" ? (
              <Field
                label={en["feedback.category.label"]}
                {...(categoryReason === undefined
                  ? {}
                  : {
                      validationState: "error" as const,
                      validationMessage: feedbackReasonText(categoryReason),
                    })}
              >
                <RadioGroup
                  value={category ?? ""}
                  onChange={(_, data) => {
                    setCategory(data.value as FeedbackCategory);
                  }}
                >
                  {CATEGORIES.map((option) => (
                    <Radio key={option.value} value={option.value} label={option.label} />
                  ))}
                </RadioGroup>
              </Field>
            ) : null}
            {commentsEnabled ? (
              <Field
                label={en["feedback.comment.label"]}
                hint={
                  length >= COUNTER_FROM
                    ? `${en["feedback.comment.hint"]} ${fillText(en["feedback.comment.counter"], {
                        count: numbers.format(length),
                        max: numbers.format(FEEDBACK_COMMENT_MAX_CODE_POINTS),
                      })}`
                    : en["feedback.comment.hint"]
                }
                {...(commentReason === undefined
                  ? {}
                  : {
                      validationState: "error" as const,
                      validationMessage: feedbackReasonText(commentReason),
                    })}
              >
                <Textarea
                  value={comment}
                  resize="vertical"
                  onChange={(_, data) => {
                    setComment(firstCodePoints(data.value, FEEDBACK_COMMENT_MAX_CODE_POINTS));
                  }}
                />
              </Field>
            ) : null}
            {barError === null ? null : <ApiErrorBar error={barError} />}
          </DialogContent>
          <DialogActions>
            <Button appearance="primary" disabled={pending || waiting} onClick={submit}>
              {pending ? en["feedback.sending"] : en["feedback.submit"]}
            </Button>
            <Button onClick={onClose}>{en["feedback.cancel"]}</Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
