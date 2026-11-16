// A small, valid PDF for the citation and document downloads: one page per line of text, each
// drawn in the standard Helvetica font, with a correct cross-reference table, so a browser's PDF
// viewer renders it and a saved file opens.

function escapeText(text: string): string {
  return text.replace(/[\\()]/g, (character) => `\\${character}`).replace(/[^\x20-\x7e]/g, "?");
}

export function buildPdf(pages: readonly string[]): Buffer {
  const objects: string[] = [];
  const pageCount = pages.length;
  const fontObject = 3 + pageCount * 2;
  objects.push("<< /Type /Catalog /Pages 2 0 R >>");
  const kids = pages.map((_, index) => `${String(3 + index * 2)} 0 R`).join(" ");
  objects.push(`<< /Type /Pages /Kids [${kids}] /Count ${String(pageCount)} >>`);
  for (const [index, text] of pages.entries()) {
    const contentObject = 4 + index * 2;
    const stream = `BT /F1 24 Tf 72 700 Td (${escapeText(text)}) Tj ET`;
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents ${String(contentObject)} 0 R ` +
        `/Resources << /Font << /F1 ${String(fontObject)} 0 R >> >> >>`,
    );
    objects.push(
      `<< /Length ${String(Buffer.byteLength(stream, "latin1"))} >>\nstream\n${stream}\nendstream`,
    );
  }
  objects.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");

  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(body, "latin1"));
    body += `${String(index + 1)} 0 obj\n${object}\nendobj\n`;
  }
  const xrefOffset = Buffer.byteLength(body, "latin1");
  body += `xref\n0 ${String(objects.length + 1)}\n0000000000 65535 f \n`;
  for (const offset of offsets) body += `${String(offset).padStart(10, "0")} 00000 n \n`;
  body += `trailer\n<< /Size ${String(objects.length + 1)} /Root 1 0 R >>\nstartxref\n${String(xrefOffset)}\n%%EOF\n`;
  return Buffer.from(body, "latin1");
}
