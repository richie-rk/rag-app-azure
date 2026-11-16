// The only writer of the two live regions (10-web-client.md section 11.2). `aria-live` is never
// put on the message list or an answer container, because a region that re-renders would be read
// over and over; announcements go through here instead.

import { LIVE_LOG_CHILDREN } from "../platform/constants";

/** The status region is emptied first and filled this much later, so that a repeated message is
 * announced each time. */
export const STATUS_REFILL_MS = 50;

let answerLog: HTMLElement | null = null;
let statusRegion: HTMLElement | null = null;
let pendingStatus: ReturnType<typeof setTimeout> | undefined;

/** Called by `LiveRegions` when its two elements mount; the returned function unregisters them. */
export function registerLiveRegions(log: HTMLElement, status: HTMLElement): () => void {
  answerLog = log;
  statusRegion = status;
  return () => {
    if (answerLog === log) answerLog = null;
    if (statusRegion === status) {
      clearTimeout(pendingStatus);
      statusRegion = null;
    }
  };
}

/** Replaces the status region's text: copied, saved, stopped, the broadened search, answer
 * complete, run finished. */
export function announceStatus(text: string): void {
  const region = statusRegion;
  if (region === null) return;
  clearTimeout(pendingStatus);
  region.textContent = "";
  pendingStatus = setTimeout(() => {
    region.textContent = text;
  }, STATUS_REFILL_MS);
}

/** Appends streamed answer text, sentence by sentence, as a new paragraph of the answer log; the
 * oldest paragraphs go once more than 20 are kept (section 8.18). */
export function appendToAnswerLog(text: string): void {
  const log = answerLog;
  if (log === null || text === "") return;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  log.append(paragraph);
  while (log.childElementCount > LIVE_LOG_CHILDREN) log.firstElementChild?.remove();
}
