// The title, text and retry rule of each turn error code (10-web-client.md sections 8.12 and
// 12.6), word for word from `11-api.md` sections 2.3 and 2.4: the 14 stream codes and
// `turn_abandoned`. A live `error` event shows its code's title with the event's own message; a
// stored failed turn shows the title and text from here. The record is typed over the generated
// enumeration, so a code without an entry fails `tsc`.

import type { TurnView } from "../api/generated/types.gen";

export type TurnErrorCode = NonNullable<TurnView["errorCode"]>;

export interface TurnErrorText {
  readonly title: string;
  readonly text: string;
  /** Whether Retry is offered for a stored failed turn with this code (section 8.10). */
  readonly retryable: boolean;
}

/** The entry of a code from the API, or undefined for one this client doesn't know. */
export function turnErrorText(code: string | null): TurnErrorText | undefined {
  return code !== null && Object.hasOwn(turnErrorTexts, code)
    ? turnErrorTexts[code as TurnErrorCode]
    : undefined;
}

export const turnErrorTexts: Readonly<Record<TurnErrorCode, TurnErrorText>> = {
  conversation_unavailable: {
    title: "Conversation unavailable",
    text: "Your conversation couldn't be loaded. Retry in a moment.",
    retryable: true,
  },
  retrieval_unavailable: {
    title: "Document search unavailable",
    text: "The document search isn't responding. Retry in a moment.",
    retryable: true,
  },
  source_screening_unavailable: {
    title: "Source safety check unavailable",
    text: "The safety check for sources isn't available right now. Retry in a moment.",
    retryable: true,
  },
  prompt_too_large: {
    title: "Conversation too long",
    text: "This conversation is too long to continue. Start a new chat.",
    retryable: false,
  },
  question_filtered: {
    title: "Question blocked",
    text: "This question was blocked by the content safety filter. Rephrase it, then send it.",
    retryable: false,
  },
  answer_filtered: {
    title: "Answer blocked",
    text: "The answer was stopped by the content safety filter. Try asking in a different way.",
    retryable: false,
  },
  model_busy: {
    title: "Answer service busy",
    text: "The answer service is busy. Retry shortly.",
    retryable: true,
  },
  model_unavailable: {
    title: "Answer service unavailable",
    text: "The answer service isn't responding. Retry in a moment.",
    retryable: true,
  },
  model_request_rejected: {
    title: "Request not processed",
    text: "The answer service couldn't process this request. Try a shorter question.",
    retryable: false,
  },
  answer_incomplete: {
    title: "Answer incomplete",
    text: "The answer couldn't be completed. Retry or narrow the question.",
    retryable: true,
  },
  turn_timeout: {
    title: "Answer took too long",
    text: "The answer took too long. Retry the question.",
    retryable: true,
  },
  turn_save_failed: {
    title: "Answer not saved",
    text: "The answer was shown but couldn't be saved to your history. Resend the question if you need it saved.",
    retryable: true,
  },
  service_restarting: {
    title: "Service restarting",
    text: "The service is restarting. Resend your question in a moment.",
    retryable: true,
  },
  internal_error: {
    title: "Unexpected error",
    text: "Something went wrong on our side. Retry, and quote the reference if it keeps happening.",
    retryable: true,
  },
  turn_abandoned: {
    title: "Answer interrupted",
    text: "This answer was interrupted before it finished.",
    retryable: true,
  },
};
