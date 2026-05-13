/**
 * overhired — service worker (MV3 background)
 *
 * Handles messages from the popup:
 *   GENERATE   → build prompt, call companion /generate
 *   SAVE       → call companion /save
 *   PARSE_PDF  → load MuPDF WASM, extract text from base64 PDF
 */

const COMPANION_DEFAULT = 'http://localhost:7878';

/** Build headers for companion requests, injecting auth token when configured. */
function companionHeaders(settings) {
  const h = { 'Content-Type': 'application/json' };
  if (settings?.companion_token) h['X-Overhired-Token'] = settings.companion_token;
  return h;
}

function companionUrl(settings) {
  return (settings?.companion_url || COMPANION_DEFAULT).replace(/\/$/, '');
}

// ── Message router ────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  switch (msg.type) {
    case 'GENERATE':      handleGenerate(msg).then(sendResponse).catch(err => sendResponse({ error: err.message }));      break;
    case 'SAVE':          handleSave(msg).then(sendResponse).catch(err => sendResponse({ error: err.message }));           break;
    case 'PARSE_PDF':     handleParsePdf(msg).then(sendResponse).catch(err => sendResponse({ error: err.message }));      break;
    case 'EXTRACT':       handleExtract(msg).then(sendResponse).catch(err => sendResponse({ error: err.message }));       break;
    case 'DELETE_PARSER': handleDeleteParser(msg).then(sendResponse).catch(err => sendResponse({ error: err.message })); break;
    default: return false;
  }
  return true; // keep channel open for async response
});

// ── GENERATE ──────────────────────────────────────────────────────────────────

async function handleGenerate(msg) {
  const { job, perJobInstructions, settings } = msg;

  // Load resume + profile from storage
  const stored = await chrome.storage.local.get(['resume_text', 'user_profile']);
  const resumeText  = stored.resume_text  || '';
  const userProfile = stored.user_profile || {};

  if (!resumeText) throw new Error('No resume found. Please upload your PDF in Settings.');

  const body = {
    job_title:            job?.title        || 'Unknown Role',
    company:              job?.company      || 'Unknown Company',
    job_description:      job?.description  || '',
    resume_text:          resumeText,
    user_profile:         userProfile,
    global_instructions:  settings?.global_instructions || '',
    per_job_instructions: perJobInstructions || '',
    easter_egg:           settings?.easter_egg      || false,
    easter_egg_text:      settings?.easter_egg_text || null,
    // Forward extension AI settings to companion so the UI controls generation.
    ai_provider:          settings?.provider  || null,
    ai_endpoint:          settings?.endpoint  || null,
    ai_model:             settings?.model     || null,
    ai_key:               settings?.api_key   || null,
  };

  const resp = await fetch(`${companionUrl(settings)}/generate`, {
    method:  'POST',
    headers: companionHeaders(settings),
    body:    JSON.stringify(body),
  });

  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `Companion returned ${resp.status}`);
  }

  return resp.json();
}

// ── SAVE ──────────────────────────────────────────────────────────────────────

async function handleSave(msg) {
  const { company, role, coverMd, coverHtml, domain, jobDescription, resumeText, settings } = msg;
  const resp = await fetch(`${companionUrl(settings)}/save`, {
    method:  'POST',
    headers: companionHeaders(settings),
    body:    JSON.stringify({
      company,
      role,
      cover_letter_md:   coverMd,
      cover_letter_html: coverHtml,
      domain:            domain         || '',
      job_description:   jobDescription || '',
      resume_text:       resumeText     || '',
    }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `Save failed: ${resp.status}`);
  }
  return resp.json();
}

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
  const { fileData } = msg; // base64 string

  // Decode base64 → Uint8Array
  const binary = atob(fileData);
  const bytes  = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

  const mupdf = await getMuPDF();

  // Open document
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

// ── EXTRACT ───────────────────────────────────────────────────────────────────

async function handleExtract(msg) {
  const { domain, page_text, settings } = msg;
  const resp = await fetch(`${companionUrl(settings)}/extract`, {
    method:  'POST',
    headers: companionHeaders(settings),
    body:    JSON.stringify({ domain, page_text }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `Extract failed: ${resp.status}`);
  }
  return resp.json(); // { title, company, description, location }
}

// ── DELETE_PARSER ─────────────────────────────────────────────────────────────

async function handleDeleteParser(msg) {
  const { domain, settings } = msg;
  const safe = domain.toLowerCase().replace(/^www\./, '');
  const resp = await fetch(`${companionUrl(settings)}/parsers/${encodeURIComponent(safe)}`, {
    method:  'DELETE',
    headers: companionHeaders(settings),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `Delete parser failed: ${resp.status}`);
  }
  return resp.json();
}
