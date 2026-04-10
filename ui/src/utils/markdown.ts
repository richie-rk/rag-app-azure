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

export function buildCitationUrl(
  filename: string,
  container: string,
  apiBase: string,
): string {
  return `${apiBase}/documents?file_name=${encodeURIComponent(filename)}&container=${encodeURIComponent(container)}`;
}
