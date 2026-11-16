// The DOMPurify profile (10-web-client.md section 9.5): one instance, an explicit allowlist of
// tags and attributes, per-element attribute hooks and a second application of the link rule, so
// a defect in the Markdown stage can't produce a working link. The answer's allowlist reaches the
// hooks through a module variable set before and cleared after each synchronous `sanitize` call.

import DOMPurify, { type Config } from "dompurify";

import { MAX_LINKS_PER_ANSWER } from "../../platform/constants";
import type { RenderContext } from "./policy";
import { allowedLinkHref, allowedLinkSet } from "./redact";

export const SANITIZE_CONFIG = {
  ALLOWED_TAGS: [
    "p",
    "br",
    "hr",
    "h3",
    "h4",
    "h5",
    "h6",
    "strong",
    "em",
    "del",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "sup",
    "button",
    "div",
    "span",
  ],
  ALLOWED_ATTR: [
    "href",
    "target",
    "rel",
    "aria-label",
    "type",
    "data-citation",
    "align",
    "start",
    "role",
    "tabindex",
    "data-link-host",
  ],
  ALLOWED_URI_REGEXP: /^https:\/\//i,
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false,
  ALLOW_UNKNOWN_PROTOCOLS: false,
  ALLOW_SELF_CLOSE_IN_ATTR: false,
  KEEP_CONTENT: true,
  SAFE_FOR_TEMPLATES: false,
  SAFE_FOR_XML: true,
  SANITIZE_DOM: true,
  SANITIZE_NAMED_PROPS: true,
  WHOLE_DOCUMENT: false,
  RETURN_DOM: false,
  RETURN_DOM_FRAGMENT: false,
  RETURN_TRUSTED_TYPE: false,
  FORBID_TAGS: [
    "style",
    "img",
    "svg",
    "math",
    "form",
    "input",
    "textarea",
    "select",
    "option",
    "iframe",
    "frame",
    "object",
    "embed",
    "video",
    "audio",
    "source",
    "track",
    "picture",
    "link",
    "meta",
    "base",
    "script",
    "template",
    "noscript",
  ],
  FORBID_ATTR: [
    "style",
    "id",
    "name",
    "class",
    "src",
    "srcset",
    "action",
    "formaction",
    "xlink:href",
  ],
} satisfies Config;

const any = () => true;
const align = (value: string) => value === "left" || value === "center" || value === "right";

/** Where each attribute belongs, and the values it may take there; every other attribute goes. */
const ATTRIBUTE_RULES: Readonly<
  Record<string, Readonly<Record<string, (value: string) => boolean>>>
> = {
  a: { href: any, target: (value) => value === "_blank", rel: any, "aria-label": any },
  button: {
    type: (value) => value === "button",
    "data-citation": (value) => /^[1-9][0-9]?$/.test(value),
    "aria-label": any,
  },
  th: { align },
  td: { align },
  ol: { start: (value) => /^[0-9]{1,6}$/.test(value) },
  div: {
    role: (value) => value === "region",
    tabindex: (value) => value === "0",
    "aria-label": any,
  },
  span: { "data-link-host": (value) => value === "" },
};

interface SanitizeState {
  allowed: ReadonlySet<string> | null;
  /** Linked `a` elements of this call, against the ten an answer may have. */
  linked: number;
}

const IDLE: SanitizeState = { allowed: null, linked: 0 };
let state: SanitizeState = IDLE;

/** Whether `node` is the host label of section 9.3 for `href`: `span[data-link-host]` whose text
 * is ` (<host>)`. */
function isHostLabelFor(node: ChildNode | null, href: string): boolean {
  return (
    node instanceof Element &&
    node.tagName === "SPAN" &&
    node.getAttribute("data-link-host") === "" &&
    node.textContent === ` (${new URL(href).host})`
  );
}

const purify = DOMPurify(window);

purify.addHook("uponSanitizeAttribute", (node, event) => {
  const rule = ATTRIBUTE_RULES[node.tagName.toLowerCase()]?.[event.attrName];
  if (rule?.(event.attrValue) !== true) {
    event.keepAttr = false;
    return;
  }
  // `ALLOWED_URI_REGEXP` would otherwise also judge the values that aren't URLs (an aria-label,
  // `type="button"`) and remove every one; the rule above has checked them, and only `href`
  // still goes through DOMPurify's own URI check.
  if (event.attrName !== "href") event.forceKeepAttr = true;
});

purify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "BUTTON") {
    node.setAttribute("type", "button");
    return;
  }
  if (node.tagName !== "A") return;
  const raw = node.getAttribute("href");
  const href = raw === null ? null : allowedLinkHref(raw, state.allowed);
  if (
    href === null ||
    state.linked >= MAX_LINKS_PER_ANSWER ||
    !isHostLabelFor(node.nextSibling, href)
  ) {
    node.removeAttribute("href");
    node.removeAttribute("target");
    node.removeAttribute("rel");
    return;
  }
  state.linked += 1;
  node.setAttribute("rel", "noopener noreferrer");
  node.setAttribute("target", "_blank");
});

/** Sanitises the answer's HTML immediately before its single insertion (section 9.1 step 2). */
export function sanitizeAnswerHtml(html: string, context: RenderContext): string {
  state = { allowed: allowedLinkSet(context.allowedLinks), linked: 0 };
  try {
    return purify.sanitize(html, SANITIZE_CONFIG);
  } finally {
    state = IDLE;
  }
}
