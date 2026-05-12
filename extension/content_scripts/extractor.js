/**
 * overhired — content script: job info extractor + ATS form filler
 *
 * Injected into all pages. Listens for messages from the popup/service worker:
 *   GET_JOB_INFO  → scrape job title, company, description from current page
 *   FILL_FORM     → detect ATS, inject profile data + cover letter into form
 */

(function () {
  'use strict';

  // Guard against double-injection
  if (window.__overhired_injected) return;
  window.__overhired_injected = true;

  // ── ATS detection ─────────────────────────────────────────────────────────

  const ATS_PATTERNS = [
    { name: 'greenhouse',      pattern: /greenhouse\.io|boards\.greenhouse/i },
    { name: 'ashby',           pattern: /ashbyhq\.com/i },
    { name: 'workable',        pattern: /workable\.com/i },
    { name: 'smartrecruiters', pattern: /smartrecruiters\.com/i },
    { name: 'lever',           pattern: /lever\.co/i },
    { name: 'icims',           pattern: /icims\.com/i },
    { name: 'successfactors',  pattern: /successfactors\.com|successfactors\.eu|sapsf\.com/i },
  ];

  function detectATS() {
    const url = window.location.href;
    for (const { name, pattern } of ATS_PATTERNS) {
      if (pattern.test(url)) return name;
    }
    return 'generic';
  }

  // ── Generic job info scraper ──────────────────────────────────────────────
  //
  // Strategy: look for common meta tags and structured data first (most
  // reliable), then fall back to heuristic DOM scanning.

  function scrapeJobInfo() {
    const info = {
      title:       '',
      company:     '',
      location:    '',
      description: '',
      ats:         detectATS(),
    };

    // 1. JSON-LD structured data (many job boards use this)
    const jsonLdScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of jsonLdScripts) {
      try {
        const data = JSON.parse(s.textContent);
        const job  = data['@type'] === 'JobPosting' ? data
                   : Array.isArray(data['@graph']) ? data['@graph'].find(x => x['@type'] === 'JobPosting')
                   : null;
        if (job) {
          info.title       = info.title       || job.title || '';
          info.company     = info.company     || job.hiringOrganization?.name || '';
          info.location    = info.location    || job.jobLocation?.address?.addressLocality || '';
          info.description = info.description || _stripHtml(job.description || '');
        }
      } catch { /* skip malformed */ }
    }

    // 2. OpenGraph / meta tags
    if (!info.title)   info.title   = _meta('og:title')   || _meta('twitter:title') || '';
    if (!info.company) info.company = _meta('og:site_name') || '';

    // 3. Page title heuristic: "Role @ Company" or "Role | Company"
    if (!info.title || !info.company) {
      const title = document.title;
      const sep = title.match(/(.+?)\s*[@|–-]\s*(.+)/);
      if (sep) {
        if (!info.title)   info.title   = sep[1].trim();
        if (!info.company) info.company = sep[2].replace(/\s*jobs?$/i, '').trim();
      } else if (!info.title) {
        info.title = title.trim();
      }
    }

    // 4. Heuristic DOM: find the largest text block likely to be the JD
    if (!info.description) {
      info.description = _findJobDescriptionBlock();
    }

    return info;
  }

  function _meta(name) {
    const el = document.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
    return el?.content?.trim() || '';
  }

  function _stripHtml(html) {
    const div = document.createElement('div');
    div.innerHTML = html;
    return div.textContent?.trim() || '';
  }

  function _findJobDescriptionBlock() {
    // Candidates: elements with 'description', 'job-detail', 'posting-body' etc. in id/class
    const selectors = [
      '[class*="job-description"]', '[class*="jobDescription"]',
      '[class*="job_description"]', '[id*="job-description"]',
      '[class*="posting-body"]',    '[class*="description"]',
      '[class*="job-detail"]',      'article',
      'main',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) {
        const text = el.innerText?.trim();
        if (text && text.length > 200) return text.slice(0, 6000); // cap at 6 KB
      }
    }
    return document.body?.innerText?.slice(0, 6000) || '';
  }

  // ── ATS form filler ───────────────────────────────────────────────────────

  async function fillForm(coverLetter) {
    const ats = detectATS();
    const stored = await chrome.storage.local.get(['user_profile']);
    const profile = stored.user_profile || {};

    // ATS handlers are pre-injected via manifest content_scripts (no import needed).
    // window.__overhiredATS is populated by common.js + each handler file.
    const handlers = window.__overhiredATS || {};
    const fill = handlers[ats] || handlers.generic;
    if (fill) {
      await fill(profile, coverLetter);
    } else {
      _genericFill(profile, coverLetter);
    }
  }

  function _genericFill(profile, coverLetter) {
    const map = {
      // Common field name/id patterns → profile key
      'first.?name|fname':        'first_name',
      'last.?name|lname':         'last_name',
      'full.?name|name':          'name',
      'email':                    'email',
      'phone|mobile|telephone':   'phone',
      'linkedin':                 'linkedin',
      'github':                   'github',
      'address|street':           'address_street',
      'city':                     'address_city',
      'state|province|region':    'address_state',
      'zip|postal':               'address_postal',
      'country':                  'address_country',
    };

    const inputs = document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"]');
    for (const input of inputs) {
      const attr = (input.name + ' ' + input.id + ' ' + (input.placeholder || '')).toLowerCase();
      for (const [pattern, key] of Object.entries(map)) {
        if (new RegExp(pattern, 'i').test(attr) && profile[key]) {
          _setValue(input, profile[key]);
          break;
        }
      }
    }

    // Cover letter: find the biggest textarea or one with "cover" in its attrs
    const textareas = [...document.querySelectorAll('textarea')];
    const coverTA = textareas.find(ta =>
      /cover|letter|motivation|introduction/i.test(ta.name + ta.id + (ta.placeholder || ''))
    ) || textareas.sort((a, b) => b.rows - a.rows)[0];

    if (coverTA && coverLetter) _setValue(coverTA, coverLetter);
  }

  // React/Vue-compatible value setter: triggers the synthetic onChange event
  function _setValue(el, value) {
    const nativeInputValue = Object.getOwnPropertyDescriptor(
      el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
      'value'
    );
    nativeInputValue?.set?.call(el, value);
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // ── Message listener ──────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === 'GET_JOB_INFO') {
      sendResponse(scrapeJobInfo());
      return false;
    }
    if (msg.type === 'FILL_FORM') {
      fillForm(msg.coverLetter).then(() => sendResponse({ ok: true }));
      return true;
    }
  });

})();
