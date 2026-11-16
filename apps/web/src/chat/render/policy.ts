// The two policy plugins of the answer pipeline (10-web-client.md sections 9.2 to 9.4 and 9.8).
// `remarkDocchatPolicy` makes the Markdown tree inert: raw HTML and images become text, footnotes
// and task lists become plain text and glyphs, headings move down two levels, and citation tokens
// become citation elements. `rehypeDocchatPolicy` then redacts every refused URL in the text,
// keeps only allowed links, wraps tables in scroll regions and drops classes and IDs.

import type {
  Element as HastElement,
  Root as HastRoot,
  RootContent as HastContent,
  Text as HastText,
} from "hast";
import type {
  Heading,
  Nodes as MdastNodes,
  Paragraph,
  Parents as MdastParents,
  Root as MdastRoot,
  Text,
} from "mdast";
import { SKIP, visit } from "unist-util-visit";

import { en } from "../../copy/en";
import { MAX_LINKS_PER_ANSWER } from "../../platform/constants";
import { fillText } from "../../ui/format";
import { allowedLinkHref, allowedLinkSet, refusedRangesWith, type TextRange } from "./redact";

declare module "mdast" {
  /** A citation token turned into its element (section 9.4), emitted through `data.hName` and
   * `data.hChildren`. */
  interface DocchatCitation extends Node {
    type: "docchatCitation";
  }
  interface PhrasingContentMap {
    docchatCitation: DocchatCitation;
  }
  interface RootContentMap {
    docchatCitation: DocchatCitation;
  }
}

/** What one citation needs for its button. */
export interface CitationLabel {
  readonly number: number;
  readonly title: string;
}

/** The turn's citations and allowed links, each absent until the turn has them. */
export interface RenderContext {
  readonly citations?: readonly CitationLabel[] | null;
  readonly allowedLinks?: readonly string[] | null;
}

/** A citation token: only the API mints them (`12-chat-pipeline.md` section 11.1). */
export const CITATION_TOKEN = /⟦([1-9][0-9]?)⟧/g;

/** Each `⟦n⟧` as `[n]`: inside code, inside links and in the plain-text fallbacks. */
export function tokensAsText(value: string): string {
  return value.replace(CITATION_TOKEN, "[$1]");
}

/** Code points the text of an allowed link may not hold: categories Cc, Cf, Co, Cs and Cn. */
const LINK_TEXT_REMOVED = /[\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Cn}]/gu;

/** Parents whose children are phrasing content: raw HTML inside them becomes a text node; raw
 * HTML anywhere else becomes a paragraph holding that text, so it stays a block. */
const PHRASING_PARENTS: ReadonlySet<string> = new Set([
  "paragraph",
  "heading",
  "emphasis",
  "strong",
  "delete",
  "link",
  "linkReference",
  "tableCell",
]);

function text(value: string): Text {
  return { type: "text", value };
}

function replaceChild(parent: MdastParents, index: number, ...nodes: MdastNodes[]): void {
  (parent.children as MdastNodes[]).splice(index, 1, ...nodes);
}

/** Puts `prefix` at the start of the first paragraph, or of a new paragraph when the first child
 * isn't one. */
function prefixFirstParagraph(children: MdastNodes[], prefix: string): void {
  const first = children[0];
  if (first?.type === "paragraph") first.children.unshift(text(prefix));
  else children.unshift({ type: "paragraph", children: [text(prefix)] } satisfies Paragraph);
}

/** Steps 1 to 6 of section 9.2. */
function neutralise(tree: MdastRoot): void {
  visit(tree, (node, index, parent) => {
    if (parent === undefined || index === undefined) return;
    switch (node.type) {
      case "html":
        replaceChild(
          parent,
          index,
          PHRASING_PARENTS.has(parent.type)
            ? text(node.value)
            : { type: "paragraph", children: [text(node.value)] },
        );
        return SKIP;
      case "image":
      case "imageReference":
        if (node.alt) {
          replaceChild(parent, index, text(node.alt));
          return SKIP;
        }
        replaceChild(parent, index);
        return index;
      case "footnoteReference":
        replaceChild(parent, index, text(`[^${node.label ?? node.identifier}]`));
        return SKIP;
      case "footnoteDefinition": {
        const children: MdastNodes[] = [...node.children];
        prefixFirstParagraph(children, `[^${node.label ?? node.identifier}]: `);
        replaceChild(parent, index, ...children);
        // The definition's own paragraphs are visited next, for their images and HTML.
        return index;
      }
      case "listItem":
        if (typeof node.checked === "boolean") {
          prefixFirstParagraph(node.children, node.checked ? "☑ " : "☐ ");
          node.checked = null;
        }
        return;
      case "heading":
        node.depth = Math.min(node.depth + 2, 6) as Heading["depth"];
        return;
      case "code":
      case "inlineCode":
        node.value = tokensAsText(node.value);
        return;
      default:
        return;
    }
  });
}

function citationNode(numberText: string, context: RenderContext): MdastNodes | null {
  const citations = context.citations;
  if (citations === null || citations === undefined) {
    return {
      type: "docchatCitation",
      data: { hName: "sup", hChildren: [{ type: "text", value: `[${numberText}]` }] },
    };
  }
  const cited = citations.find((citation) => String(citation.number) === numberText);
  if (cited === undefined) return null;
  const button: HastElement = {
    type: "element",
    tagName: "button",
    properties: {
      type: "button",
      dataCitation: numberText,
      ariaLabel: fillText(en["chat.turn.citation"], { n: numberText, title: cited.title }),
    },
    children: [{ type: "text", value: numberText }],
  };
  return { type: "docchatCitation", data: { hName: "sup", hChildren: [button] } };
}

/** A text node's value with its tokens turned into citation nodes, or null when it holds none. */
function citedNodes(value: string, context: RenderContext): MdastNodes[] | null {
  const nodes: MdastNodes[] = [];
  let position = 0;
  for (const match of value.matchAll(CITATION_TOKEN)) {
    if (match.index > position) nodes.push(text(value.slice(position, match.index)));
    // The token without its brackets: the citation number's digits.
    const citation = citationNode(match[0].slice(1, -1), context);
    if (citation !== null) nodes.push(citation);
    position = match.index + match[0].length;
  }
  if (position === 0) return null;
  if (position < value.length) nodes.push(text(value.slice(position)));
  return nodes;
}

/** Step 7: tokens in text nodes become citations, except inside a link, where a button can't
 * sit, and where they become `[n]`. */
function citeTokens(parent: MdastParents, insideLink: boolean, context: RenderContext): void {
  const children: MdastNodes[] = [];
  for (const child of parent.children as MdastNodes[]) {
    if (child.type === "text" && !insideLink) {
      children.push(...(citedNodes(child.value, context) ?? [child]));
      continue;
    }
    if (child.type === "text") child.value = tokensAsText(child.value);
    else if ("children" in child) {
      const link = insideLink || child.type === "link" || child.type === "linkReference";
      citeTokens(child, link, context);
    }
    children.push(child);
  }
  (parent as { children: MdastNodes[] }).children = children;
}

/** The Markdown stage of section 9.2, in its order. */
export function remarkDocchatPolicy(context: RenderContext) {
  return (tree: MdastRoot): void => {
    neutralise(tree);
    citeTokens(tree, false, context);
  };
}

const BLOCKS: ReadonlySet<string> = new Set([
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "p",
  "ul",
  "ol",
  "li",
  "blockquote",
  "pre",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "div",
]);

export type HastParent = HastRoot | HastElement;

/** One text node of a run, or a citation, which counts as one space. */
type Segment = HastText | null;

/** The runs of section 9.8: consecutive text nodes of one block, with no other block, `br` or
 * `hr` between them; the text of inline elements joins the run. Every `sup` is a citation
 * element, because only citation tokens produce one. */
function textRuns(root: HastRoot): Segment[][] {
  const runs: Segment[][] = [];
  let current: Segment[] = [];
  const end = () => {
    if (current.length > 0) runs.push(current);
    current = [];
  };
  const walk = (parent: HastParent) => {
    for (const child of parent.children) {
      if (child.type === "text") {
        current.push(child);
      } else if (child.type === "element") {
        if (child.tagName === "sup") {
          current.push(null);
        } else if (BLOCKS.has(child.tagName)) {
          end();
          walk(child);
          end();
        } else if (child.tagName === "br" || child.tagName === "hr") {
          end();
        } else {
          walk(child);
        }
      }
    }
  };
  walk(root);
  end();
  return runs;
}

/** Applies a run's refused ranges: the part of the first text node a range covers becomes the
 * marker, and the parts of later nodes it covers are deleted. */
function redactRun(run: Segment[], ranges: readonly TextRange[]): void {
  let offset = 0;
  for (const segment of run) {
    if (segment === null) {
      offset += 1;
      continue;
    }
    const value = segment.value;
    const nodeEnd = offset + value.length;
    let result = "";
    let position = offset;
    for (const range of ranges) {
      if (range.end <= offset || range.start >= nodeEnd) continue;
      result += value.slice(position - offset, Math.max(range.start, offset) - offset);
      if (range.start >= offset) result += en["chat.turn.linkRemoved"];
      position = Math.min(range.end, nodeEnd);
    }
    segment.value = result + value.slice(position - offset);
    offset = nodeEnd;
  }
}

/** Step 1 of `rehypeDocchatPolicy`: every refused URL in the tree's text becomes
 * `[link removed]`, matched over runs, so markup can't split a URL. */
function redactTree(root: HastRoot, allowed: ReadonlySet<string> | null): void {
  for (const run of textRuns(root)) {
    const joined = run.map((segment) => (segment === null ? " " : segment.value)).join("");
    const ranges = refusedRangesWith(joined, allowed);
    if (ranges.length > 0) redactRun(run, ranges);
  }
}

interface Located {
  readonly element: HastElement;
  readonly parent: HastParent;
}

/** Every element with this tag name, in document order, with its parent. */
function elementsNamed(root: HastRoot, tagName: string): Located[] {
  const found: Located[] = [];
  const walk = (parent: HastParent) => {
    for (const child of parent.children) {
      if (child.type !== "element") continue;
      if (child.tagName === tagName) found.push({ element: child, parent });
      walk(child);
    }
  };
  walk(root);
  return found;
}

/** The text of a hast node: its text nodes joined, in document order. */
export function hastText(node: HastParent): string {
  let value = "";
  for (const child of node.children) {
    if (child.type === "text") value += child.value;
    else if (child.type === "element") value += hastText(child);
  }
  return value;
}

/** Removes Cc, Cf, Co, Cs and Cn code points from every text inside an allowed link. */
function cleanLinkText(node: HastParent): void {
  for (const child of node.children) {
    if (child.type === "text") child.value = child.value.replace(LINK_TEXT_REMOVED, "");
    else if (child.type === "element") cleanLinkText(child);
  }
}

/** The host label after an allowed link: ` (<host>)`, lower-case ASCII with punycode, and the
 * port when it isn't 443. */
export function hostLabel(href: string): HastElement {
  return {
    type: "element",
    tagName: "span",
    properties: { dataLinkHost: "" },
    children: [{ type: "text", value: ` (${new URL(href).host})` }],
  };
}

/** Step 2: the link policy of section 9.3 on every `a`. The first ten allowed links, counted in
 * document order, stay links; every later one becomes its text with its host label; every other
 * link becomes its text. */
function applyLinkPolicy(root: HastRoot, allowed: ReadonlySet<string> | null): void {
  const decisions: { located: Located; href: string | null; linked: boolean }[] = [];
  let linked = 0;
  for (const located of elementsNamed(root, "a")) {
    const raw = located.element.properties.href;
    const href = typeof raw === "string" ? allowedLinkHref(raw, allowed) : null;
    const isLinked = href !== null && linked < MAX_LINKS_PER_ANSWER;
    if (isLinked) linked += 1;
    decisions.push({ located, href, linked: isLinked });
  }
  // Replacing from the end keeps the earlier positions valid.
  for (const { located, href, linked: isLinked } of decisions.reverse()) {
    const { element, parent } = located;
    const index = parent.children.indexOf(element);
    if (href !== null) cleanLinkText(element);
    if (isLinked && href !== null) {
      element.properties = {
        href,
        target: "_blank",
        rel: ["noopener", "noreferrer"],
        ariaLabel: `${hastText(element)} ${en["chat.turn.linkSuffix"]}`,
      };
      parent.children.splice(index + 1, 0, hostLabel(href));
    } else {
      const replacement: HastContent[] = [...element.children];
      if (href !== null) replacement.push(hostLabel(href));
      (parent.children as HastContent[]).splice(index, 1, ...replacement);
    }
  }
}

/** Step 3: each table scrolls inside its own focusable region, `Table k` counting from 1. */
function wrapTables(root: HastRoot): void {
  elementsNamed(root, "table").forEach(({ element, parent }, index) => {
    const region: HastElement = {
      type: "element",
      tagName: "div",
      properties: {
        role: "region",
        tabIndex: 0,
        ariaLabel: fillText(en["chat.turn.table"], { k: index + 1 }),
      },
      children: [element],
    };
    parent.children.splice(parent.children.indexOf(element), 1, region);
  });
}

/** Step 4: no element keeps a class or an ID. */
function dropClassesAndIds(parent: HastParent): void {
  for (const child of parent.children) {
    if (child.type !== "element") continue;
    delete child.properties.className;
    delete child.properties.id;
    dropClassesAndIds(child);
  }
}

/** The HTML stage of section 9.2, in its order. */
export function rehypeDocchatPolicy(context: RenderContext) {
  return (tree: HastRoot): void => {
    const allowed = allowedLinkSet(context.allowedLinks);
    redactTree(tree, allowed);
    applyLinkPolicy(tree, allowed);
    wrapTables(tree);
    dropClassesAndIds(tree);
  };
}
