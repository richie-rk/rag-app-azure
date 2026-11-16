// The client constants of 10-web-client.md section 1.10. They shape the interface and change only
// with a release; none is a setting. Limits that come from the API (`limits.maxFactChars`,
// `uploadLimits.*`) or from `limits.gen.ts` live there, not here.

/** Question limit in Unicode code points, and the length the counter shows from (section 8.5). */
export const QUESTION_MAX_CODE_POINTS = 4_000;
export const QUESTION_COUNTER_FROM = 3_600;

/** The feedback comment counter shows from this share of its limit (section 8.16). */
export const FEEDBACK_COUNTER_SHARE = 0.9;

/** The feedback comment limit in code points (sections 1.10 and 8.16): the bound the API applies
 * after normalising the comment (`11-api.md` section 3.4). `limits.gen.ts` holds the request
 * schema's bound as sent, 8,000, which the normalised comment never reaches. */
export const FEEDBACK_COMMENT_MAX_CODE_POINTS = 2_000;

/** Stall timeout of a chat stream (section 8.10). */
export const STALL_TIMEOUT_MS = 45_000;

/** Largest stream line, in bytes (section 8.7). */
export const MAX_STREAM_LINE_BYTES = 1_048_576;

/** Render throttle while an answer streams, and the plain-text bound (section 9.6). */
export const RENDER_THROTTLE_MS = 100;
export const PLAIN_TEXT_ANSWER_ABOVE = 200_000;

/** Allowed links that render as links in one answer, counted in document order (section 9.3). */
export const MAX_LINKS_PER_ANSWER = 10;

/** The `409 turn_in_progress` resend (section 8.6). */
export const TURN_IN_PROGRESS_WINDOW_MS = 10_000;
export const TURN_IN_PROGRESS_RESEND_AFTER_MS = 1_000;
export const TURN_IN_PROGRESS_MAX_RESENDS = 2;

/** Page sizes (section 5.8). */
export const SESSION_PAGE_SIZE = 30;
export const TURNS_PAGE_SIZE = 30;
export const ADMIN_LIST_PAGE_SIZE = 50;
export const MEMBERS_PAGE_SIZE = 100;
export const RUN_ITEMS_PAGE_SIZE = 100;
export const USER_SEARCH_PAGE_SIZE = 20;

/** Recent chats in the navigation drawer (section 6.3). */
export const RECENT_CHATS = 6;

/** Automatic query retries (section 5.7). */
export const QUERY_RETRIES = 2;
export const QUERY_RETRY_DELAYS_MS: readonly number[] = [1_000, 4_000];
export const QUERY_RETRY_JITTER_MS = 250;
export const QUERY_RETRY_AFTER_LIMIT_S = 30;

/** Upload retries and the no-progress abort (section 10.5). */
export const UPLOAD_RETRIES = 2;
export const UPLOAD_RETRY_DELAYS_MS: readonly number[] = [2_000, 6_000];
export const UPLOAD_STALL_MS = 120_000;
export const UPLOAD_BUSY_WAITS = 6;

/** Waits on a `429` limit in the two batch loops: per person (Add people), per request (uploads). */
export const BATCH_RATE_LIMIT_WAITS = 3;

/** A time from `retryAfterSeconds` shows seconds when it is closer than this (section 5.10). */
export const RETRY_TIME_WITH_SECONDS_BELOW_S = 60;

/** Largest PDF page the viewer accepts, and largest whole-document PDF (sections 7.9, 8.14). */
export const PDF_PAGE_MAX_BYTES = 20_971_520;
export const PDF_DOCUMENT_MAX_BYTES = 104_857_600;

/** Object URL revocation after a download (section 8.14). */
export const OBJECT_URL_REVOKE_MS = 30_000;

/** Polling intervals and bounds (sections 7.11 and 8.12). */
export const POLL_RUN_MS = 5_000;
export const POLL_LIST_MS = 15_000;
export const POLL_SESSION_MS = 5_000;
export const POLL_SESSION_MAX_MS = 250_000;
export const POLL_MAX_FAILURES = 3;

/** Copy confirmation, toast duration, search debounce and minimum search length. */
export const COPY_CONFIRMATION_MS = 2_000;
export const TOAST_MS = 6_000;
export const SEARCH_DEBOUNCE_MS = 300;
export const SEARCH_MIN_CHARS = 2;

/** Live log children kept (section 8.18). */
export const LIVE_LOG_CHILDREN = 20;

/** The message list follows new text within this distance of the bottom (section 8.13). */
export const FOLLOW_THRESHOLD_PX = 120;

/** Breakpoints (section 8.1). */
export const BREAKPOINT_MEDIUM_PX = 768;
export const BREAKPOINT_WIDE_PX = 1_280;

/** Longest `state` return path (section 4.3). */
export const RETURN_PATH_MAX = 512;

/** Longest download file name stem (section 8.14). */
export const DOWNLOAD_STEM_MAX = 150;
