// The answer pipeline (10-web-client.md sections 9.1 and 9.6, ADR 0084): Markdown becomes an
// HTML string through the two policy plugins, and DOMPurify sanitises it immediately before its
// single insertion. Answers are the only formatted text in the product.

import type { Root as HastRoot } from "hast";
import rehypeStringify from "rehype-stringify";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import remarkRehype from "remark-rehype";
import { unified } from "unified";

import { PLAIN_TEXT_ANSWER_ABOVE } from "../../platform/constants";
import { redactRefusedUrls } from "./redact";
import {
  rehypeDocchatPolicy,
  remarkDocchatPolicy,
  tokensAsText,
  type RenderContext,
} from "./policy";
import { sanitizeAnswerHtml } from "./sanitize";

/** The chain of step 1 up to and including `rehypeDocchatPolicy`. `remark-rehype` keeps its
 * default `allowDangerousHtml: false`. */
function policyChain(context: RenderContext) {
  return unified()
    .use(remarkParse)
    .use(remarkGfm, { singleTilde: false })
    .use(remarkDocchatPolicy, context)
    .use(remarkRehype)
    .use(rehypeDocchatPolicy, context);
}

/** Steps 1 and 2 of section 9.1: the sanitised HTML string that `AnswerBody` inserts. */
export function renderAnswer(text: string, context: RenderContext): string {
  const html = String(policyChain(context).use(rehypeStringify).processSync(text));
  return sanitizeAnswerHtml(html, context);
}

/** The rendered, policy-applied tree, without a string: what Copy answer walks (section 8.17). */
export function answerTree(text: string, context: RenderContext): HastRoot {
  const chain = policyChain(context);
  return chain.runSync(chain.parse(text));
}

/** The plain-text form of the fallbacks (section 9.6): refused URLs redacted, then each `⟦n⟧`
 * as `[n]`; no HTML is inserted. */
export function plainTextAnswer(text: string, allowedLinks: readonly string[] | null): string {
  return tokensAsText(redactRefusedUrls(text, allowedLinks));
}

export type AnswerView =
  | { readonly kind: "html"; readonly html: string }
  | { readonly kind: "plain"; readonly text: string };

/** What `AnswerBody` shows: the sanitised HTML, or plain text for an answer longer than 200,000
 * characters (which skips the pipeline) and for one whose pipeline throws. */
export function answerView(
  text: string,
  context: RenderContext,
  render: (text: string, context: RenderContext) => string = renderAnswer,
): AnswerView {
  const allowedLinks = context.allowedLinks ?? null;
  if (text.length > PLAIN_TEXT_ANSWER_ABOVE) {
    return { kind: "plain", text: plainTextAnswer(text, allowedLinks) };
  }
  try {
    return { kind: "html", html: render(text, context) };
  } catch {
    return { kind: "plain", text: plainTextAnswer(text, allowedLinks) };
  }
}
