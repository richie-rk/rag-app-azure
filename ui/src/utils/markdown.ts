/**
 * Citation parsing utilities for markdown content.
 *
 * Extracts [filename.pdf] references from LLM output for
 * rendering as clickable citation links.
 */

export function extractCitations(text: string): string[] {
  const matches = text.match(/\[([^\]]+)\]/g);
  if (!matches) return [];

  return [...new Set(matches.map((m) => m.slice(1, -1)))];
}

/**
 * Recover the original file and page number from a chunk's `sourcepage`.
 *
 * The ingestion chunker names each page `{base}-{page}.{ext}`, so the page is
 * the final `-N` segment. `page` is null when the name carries no such
 * segment, in which case the citation opens the document without a page jump.
 */
export function parseSourcePage(sourcepage: string): {
  sourcefile: string;
  page: number | null;
} {
  const dot = sourcepage.lastIndexOf(".");
  const ext = dot >= 0 ? sourcepage.slice(dot) : "";
  const stem = dot >= 0 ? sourcepage.slice(0, dot) : sourcepage;

  const dash = stem.lastIndexOf("-");
  const pageStr = stem.slice(dash + 1);

  if (dash >= 0 && /^\d+$/.test(pageStr)) {
    return { sourcefile: stem.slice(0, dash) + ext, page: Number(pageStr) };
  }
  return { sourcefile: sourcepage, page: null };
}
