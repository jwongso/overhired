/**
 * overhired — service worker (MV3 background)
 *
 * Intentionally minimal. Only two jobs:
 *
 *   1. Open the side panel when the toolbar icon is clicked.
 *
 *   2. Handle PARSE_PDF — MuPDF WASM lives here because WASM modules require
 *      a persistent background context; the service worker is suitable for
 *      PDF parsing because it completes in < 1 second (no idle-timeout risk).
 *
 * ── Why companion calls are NOT here ──────────────────────────────────────────
 * Chrome MV3 service workers are killed after ~30 s of inactivity. Routing
 * long-running LLM calls (extract, generate, save …) through the SW causes the
 * infamous "message channel closed before a response was received" error.
 *
 * The side panel (popup.js) is a normal persistent web page — it lives as long
 * as it is open and has no idle timeout. All companion HTTP calls are therefore
 * made directly from popup.js via fetch(). The SW is never in the critical path
 * for those operations.
 */

// Open the side panel when the toolbar icon is clicked.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

// ── Message router ────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  switch (msg.type) {
    case 'PARSE_PDF':
      handleParsePdf(msg).then(sendResponse).catch(err => sendResponse({ error: err.message }));
      break;
    default:
      return false; // synchronous — no async response
  }
  return true; // keep channel open for PARSE_PDF async response
});

// ── PARSE_PDF (MuPDF WASM) ────────────────────────────────────────────────────

let _mupdfReady = null;
let _mupdf      = null;

async function getMuPDF() {
  if (_mupdf) return _mupdf;
  if (_mupdfReady) return _mupdfReady;

  _mupdfReady = (async () => {
    try {
      const wasmUrl = chrome.runtime.getURL('wasm/mupdf.js');
      const mod = await import(wasmUrl);
      _mupdf = await mod.default();
      return _mupdf;
    } catch (e) {
      _mupdfReady = null; // allow retry on next call
      throw new Error(`MuPDF WASM failed to load: ${e.message}. ` +
        'Run: cd extension/wasm && node setup.js');
    }
  })();

  return _mupdfReady;
}

async function handleParsePdf(msg) {
  const { fileData } = msg; // base64-encoded PDF bytes

  const binary = atob(fileData);
  const bytes  = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

  const mupdf = await getMuPDF();

  const doc   = mupdf.Document.openDocument(bytes, 'application/pdf');
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

  const resumeText = parts.join('\n\n').trim();
  if (!resumeText) throw new Error('Could not extract text from PDF (may be image-only).');

  return { resumeText };
}
