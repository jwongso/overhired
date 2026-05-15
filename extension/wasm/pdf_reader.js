/**
 * grapply — MuPDF WASM wrapper
 *
 * Loaded lazily by the service worker on first PDF parse request.
 * Exports a single function: extractText(pdfBytes: Uint8Array) -> string
 */

import * as mupdf from './mupdf.js';

let _ready = false;

export async function extractText(pdfBytes) {
  if (!_ready) {
    await mupdf.ready;
    _ready = true;
  }

  const doc   = mupdf.Document.openDocument(pdfBytes, 'application/pdf');
  const pages = doc.countPages();
  const parts = [];

  for (let i = 0; i < pages; i++) {
    const page = doc.loadPage(i);
    try {
      parts.push(page.toStructuredText('preserve-whitespace').asText());
    } finally {
      page.destroy();
    }
  }
  doc.destroy();

  return parts.join('\n\n').trim();
}
