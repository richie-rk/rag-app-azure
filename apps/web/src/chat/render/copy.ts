// Copy answer's text (10-web-client.md section 8.17): plain text built from the answer's rendered,
// policy-applied and redacted tree, never HTML and never the model's raw Markdown, so a pasted
// answer holds no URL, link or image the page refused.

import type { Element as HastElement, Root as HastRoot, RootContent } from "hast";

import { PLAIN_TEXT_ANSWER_ABOVE } from "../../platform/constants";
import { answerTree } from "./pipeline";
import { hastText, type RenderContext } from "./policy";

/** Elements that end with a line break (rule 1). */
const LINE_BLOCKS: ReadonlySet<string> = new Set([
  "p",
  "h3",
  "h4",
  "h5",
  "h6",
  "li",
  "tr",
  "blockquote",
  "pre",
]);

/** Parents whose white-space-only text is layout between blocks, not answer text. */
const CONTAINERS: ReadonlySet<string> = new Set([
  "root",
  "ul",
  "ol",
  "li",
  "blockquote",
  "table",
  "thead",
  "tbody",
  "tr",
  "div",
]);

/** The answer can't be copied: the tree can't be built, or the answer renders without it. */
export class AnswerNotCopyable extends Error {
  constructor(cause?: unknown) {
    super("answer_not_copyable", { cause });
    this.name = "AnswerNotCopyable";
  }
}

class PlainTextWriter {
  private text = "";
  /** At the start of a line: nothing written yet, a line break, or a list item's marker. */
  private lineStart = true;
  /** After ` (<URL>)` or ` (<host>)`: a space goes in when the next character isn't white space. */
  private spaceAfterLink = false;

  result(): string {
    return this.text;
  }

  write(value: string): void {
    if (value === "") return;
    if (this.spaceAfterLink && !/^\s/.test(value)) this.text += " ";
    this.spaceAfterLink = false;
    this.text += value;
    this.lineStart = value.endsWith("\n");
  }

  lineBreak(): void {
    this.spaceAfterLink = false;
    if (this.lineStart) return;
    this.text += "\n";
    this.lineStart = true;
  }

  /** A list item's `- ` or number, after which the line still counts as started. */
  marker(value: string): void {
    this.write(value);
    this.lineStart = true;
  }

  linkSuffix(value: string): void {
    this.write(value);
    this.spaceAfterLink = true;
  }
}

function isHostLabel(node: HastElement): boolean {
  return node.tagName === "span" && "dataLinkHost" in node.properties;
}

/** An allowed link that renders as a link: the policy left `href` only on those. */
function isLinkedAnchor(node: RootContent | undefined): boolean {
  return (
    node?.type === "element" && node.tagName === "a" && typeof node.properties.href === "string"
  );
}

function walkChildren(parent: HastRoot | HastElement, writer: PlainTextWriter): void {
  const container = CONTAINERS.has(parent.type === "root" ? "root" : parent.tagName);
  let previous: RootContent | undefined;
  for (const child of parent.children) {
    if (child.type === "text") {
      // The line break after a `br` is already written.
      const afterBreak = previous?.type === "element" && previous.tagName === "br";
      const value = afterBreak ? child.value.replace(/^\n/, "") : child.value;
      if (!(container && value.trim() === "")) writer.write(value);
    } else if (child.type === "element") {
      // A linked `a` has written its URL in place of the host label that follows it (rule 2).
      if (!(isHostLabel(child) && isLinkedAnchor(previous))) walkElement(child, parent, writer);
    }
    previous = child;
  }
}

function walkElement(
  node: HastElement,
  parent: HastRoot | HastElement,
  writer: PlainTextWriter,
): void {
  switch (node.tagName) {
    case "a":
      walkChildren(node, writer);
      writer.linkSuffix(` (${String(node.properties.href)})`);
      return;
    case "span":
      // Only the link policy makes spans: a host label after an allowed link past the first 10.
      writer.linkSuffix(hastText(node));
      return;
    case "sup": {
      // A citation button or a plain token: `[n]` (rule 7).
      const button = node.children.find(
        (child): child is HastElement => child.type === "element" && child.tagName === "button",
      );
      const citation = button?.properties["dataCitation"];
      writer.write(citation === undefined ? hastText(node) : `[${String(citation)}]`);
      return;
    }
    case "br":
      writer.write("\n");
      return;
    case "hr":
      writer.lineBreak();
      return;
    case "li": {
      writer.lineBreak();
      const ordered = parent.type === "element" && parent.tagName === "ol";
      if (ordered) {
        const start = Number(parent.properties.start ?? 1);
        const position = parent.children
          .filter((child) => child.type === "element" && child.tagName === "li")
          .indexOf(node);
        writer.marker(`${String(start + position)}. `);
      } else {
        writer.marker("- ");
      }
      walkChildren(node, writer);
      writer.lineBreak();
      return;
    }
    case "th":
    case "td": {
      const cells = parent.children.filter(
        (child) => child.type === "element" && (child.tagName === "th" || child.tagName === "td"),
      );
      if (cells.indexOf(node) > 0) writer.write("\t");
      walkChildren(node, writer);
      return;
    }
    default:
      if (LINE_BLOCKS.has(node.tagName)) writer.lineBreak();
      walkChildren(node, writer);
      if (LINE_BLOCKS.has(node.tagName)) writer.lineBreak();
  }
}

/** The plain text Copy answer writes, built from the tree that `rehypeDocchatPolicy` redacted and
 * applied the link policy to. Throws `AnswerNotCopyable` when that tree can't be built, or the
 * answer is longer than 200,000 characters and renders without it. */
export function answerToPlainText(text: string, context: RenderContext): string {
  if (text.length > PLAIN_TEXT_ANSWER_ABOVE) throw new AnswerNotCopyable();
  let tree: HastRoot;
  try {
    tree = answerTree(text, context);
  } catch (cause) {
    throw new AnswerNotCopyable(cause);
  }
  const writer = new PlainTextWriter();
  walkChildren(tree, writer);
  return writer.result();
}
