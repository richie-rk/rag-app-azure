// Refused URLs in text (10-web-client.md sections 9.3 and 9.8): the one normalisation rule for
// links, and the redaction that replaces every URL with a scheme and `//`, every `www.` address
// and every protocol-relative address that isn't an allowed link by `[link removed]`, wherever
// model-written text is shown, copied, spoken or sent back as a question.

import { en } from "../../copy/en";

/** The longest URL that can be an allowed link (section 9.3). */
export const MAX_ALLOWED_URL_LENGTH = 2_048;

/** A URL with a scheme and `//`, a `www.` address in any letter case, or a protocol-relative
 * address. The scheme is one letter and at most 31 more, so the search stays linear. */
const CANDIDATE =
  /[A-Za-z][A-Za-z0-9+.-]{0,31}:\/\/[^\s<>"'`]+|[Ww]{3}\.[^\s<>"'`]+|(?<![A-Za-z0-9:/])\/\/[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+[^\s<>"'`]*/g;

/** The three kinds, anchored: a candidate that trimming reduces below its kind's shape (such as
 * `https://` alone) no longer names an address and stays as text. */
const SCHEME_FORM = /^[A-Za-z][A-Za-z0-9+.-]{0,31}:\/\/[^\s<>"'`]+$/;
const WWW_FORM = /^[Ww]{3}\.[^\s<>"'`]+$/;
const RELATIVE_FORM = /^\/\/[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+[^\s<>"'`]*$/;

const FORMAT_CHARACTER = /\p{Cf}/u;
const TRAILING_PUNCTUATION = new Set(["?", "!", ".", ",", ":", "*", "_", "~"]);

export interface TextRange {
  /** The first code unit of the refused URL. */
  readonly start: number;
  /** One past its last code unit. */
  readonly end: number;
}

/** The normalised form of a URL string (section 9.3): `new URL(s).href`, or null when it doesn't
 * parse. */
export function normaliseUrl(value: string): string | null {
  try {
    return new URL(value).href;
  } catch {
    return null;
  }
}

/** The turn's `allowedLinks`, normalised, or null before the turn has them. An entry that doesn't
 * parse is dropped. */
export function allowedLinkSet(
  allowedLinks: readonly string[] | null | undefined,
): ReadonlySet<string> | null {
  if (allowedLinks === null || allowedLinks === undefined) return null;
  const set = new Set<string>();
  for (const entry of allowedLinks) {
    const normalised = normaliseUrl(entry);
    if (normalised !== null) set.add(normalised);
  }
  return set;
}

/** Conditions 1 to 3 of section 9.3: the normalised URL when `value` is an allowed link, else
 * null. */
export function allowedLinkHref(value: string, allowed: ReadonlySet<string> | null): string | null {
  if (allowed === null || value.length > MAX_ALLOWED_URL_LENGTH) return null;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "") return null;
  return allowed.has(url.href) ? url.href : null;
}

/** The text with its format characters (Unicode category Cf) left out, and for each code unit
 * kept, its offset in the original text; so no invisible character can split `://`. */
function withoutFormatCharacters(text: string): { visible: string; origin: number[] | null } {
  if (!FORMAT_CHARACTER.test(text)) return { visible: text, origin: null };
  let visible = "";
  const origin: number[] = [];
  let offset = 0;
  for (const character of text) {
    if (!FORMAT_CHARACTER.test(character)) {
      visible += character;
      for (let unit = 0; unit < character.length; unit += 1) origin.push(offset + unit);
    }
    offset += character.length;
  }
  return { visible, origin };
}

/** The length of `&` followed by one or more ASCII letters or digits and `;` at the end of
 * `value`, or 0. */
function trailingEntityLength(value: string): number {
  let index = value.length - 2;
  while (index >= 0 && /[A-Za-z0-9]/.test(value.charAt(index))) index -= 1;
  return index >= 0 && value.charAt(index) === "&" && index < value.length - 2
    ? value.length - index
    : 0;
}

/** Trims a candidate's end by the GFM rules for extended autolinks, applied until none applies;
 * the parentheses are counted once, so a long run of `)` stays linear. */
export function trimCandidate(candidate: string): string {
  let end = candidate.length;
  let open = 0;
  let close = 0;
  for (const character of candidate) {
    if (character === "(") open += 1;
    else if (character === ")") close += 1;
  }
  for (;;) {
    const last = candidate.charAt(end - 1);
    if (TRAILING_PUNCTUATION.has(last)) {
      end -= 1;
    } else if (last === ")" && close > open) {
      end -= 1;
      close -= 1;
    } else if (last === ";") {
      const entity = trailingEntityLength(candidate.slice(0, end));
      if (entity === 0) return candidate.slice(0, end);
      end -= entity;
    } else {
      return candidate.slice(0, end);
    }
  }
}

/** Whether a trimmed candidate is kept: only a URL with a scheme that is an allowed link. A `www.`
 * candidate reads as `http://` before it and a protocol-relative one as `http:` before it, so
 * both are always refused, as the link policy refuses the `http:` link remark-gfm makes. */
function isKept(candidate: string, allowed: ReadonlySet<string> | null): boolean {
  let url = candidate;
  if (WWW_FORM.test(candidate)) url = `http://${candidate}`;
  else if (candidate.startsWith("//")) url = `http:${candidate}`;
  return allowedLinkHref(url, allowed) !== null;
}

function isCandidateForm(candidate: string): boolean {
  return SCHEME_FORM.test(candidate) || WWW_FORM.test(candidate) || RELATIVE_FORM.test(candidate);
}

/** The offsets of every refused URL in `text`, given the turn's normalised allowed links. */
export function refusedRangesWith(text: string, allowed: ReadonlySet<string> | null): TextRange[] {
  const { visible, origin } = withoutFormatCharacters(text);
  const ranges: TextRange[] = [];
  for (const match of visible.matchAll(CANDIDATE)) {
    const candidate = trimCandidate(match[0]);
    if (!isCandidateForm(candidate) || isKept(candidate, allowed)) continue;
    const start = match.index;
    const last = start + candidate.length - 1;
    // Every visible offset has its original one.
    ranges.push(
      origin === null
        ? { start, end: last + 1 }
        : { start: Number(origin[start]), end: Number(origin[last]) + 1 },
    );
  }
  return ranges;
}

/** The start and end offsets of every refused URL in `text`; `allowedLinks` is the turn's list,
 * or absent before the turn has it. */
export function refusedUrlRanges(
  text: string,
  allowedLinks: readonly string[] | null | undefined,
): TextRange[] {
  return refusedRangesWith(text, allowedLinkSet(allowedLinks));
}

/** `text` with every refused URL replaced by `[link removed]`; text outside them is unchanged, so
 * trailing punctuation stays after the marker. It never throws: a candidate that `new URL()` can't
 * parse is refused. */
export function redactRefusedUrls(
  text: string,
  allowedLinks: readonly string[] | null | undefined,
): string {
  const ranges = refusedUrlRanges(text, allowedLinks);
  if (ranges.length === 0) return text;
  let result = "";
  let position = 0;
  for (const range of ranges) {
    result += text.slice(position, range.start) + en["chat.turn.linkRemoved"];
    position = range.end;
  }
  return result + text.slice(position);
}
