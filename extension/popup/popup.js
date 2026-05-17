/**
 * grapply - popup UI (Preact + htm, no build step)
 */
import { h, render }       from '../vendor/preact.module.js';
import { useState, useEffect, useCallback, useRef } from '../vendor/preact-hooks.module.js';
import { marked }          from '../vendor/marked.esm.js';
import htm                 from '../vendor/htm.module.js';

const html = htm.bind(h);

const AUSPICE_URL = 'https://fengshui.overhired.work';

async function fetchAuspice() {
  // Use local date strings (en-CA locale gives YYYY-MM-DD) so timezone is correct.
  const fmt = d => d.toLocaleDateString('en-CA');
  const today    = fmt(new Date());
  const tomorrow = fmt(new Date(Date.now() + 86400000));
  const twoWeeks = fmt(new Date(Date.now() + 15 * 86400000));
  try {
    const [dayRes, bestRes] = await Promise.all([
      fetch(`${AUSPICE_URL}/day?date=${today}`).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch(`${AUSPICE_URL}/best?activity=interview&from=${tomorrow}&to=${twoWeeks}&weekend=false`)
        .then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    return { day: dayRes, best: bestRes };
  } catch { return null; }
}

function FengShuiPanel() {
  const [data, setData] = useState(null);

  useEffect(() => { fetchAuspice().then(setData); }, []);

  if (!data?.day) return null;

  const { day, best } = data;
  const TYPE_COLOR = { lucky: '#4caf7d', ordinary: '#f59e0b', unlucky: '#e05252' };
  const TYPE_LABEL = { lucky: 'Lucky Day', ordinary: 'Ordinary Day', unlucky: 'Unlucky Day' };
  const color    = TYPE_COLOR[day.type];
  const bestDays = (best?.days || [])
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(0, 3)
    .map(d => {
      const [y, m, day] = d.date.split('-').map(Number);
      return new Date(y, m - 1, day)
        .toLocaleDateString('en', { weekday: 'short', month: 'short', day: 'numeric' });
    });

  return html`
    <div class="fengshui-panel">
      <div class="fengshui-header">
        <span class="fengshui-dot" style="background:${color}"></span>
        <span class="fengshui-type" style="color:${color}">${TYPE_LABEL[day.type]}</span>
        <span class="fengshui-badge">🈴 feng shui</span>
      </div>
      ${day.type !== 'unlucky' && day.favourable.length > 0 && html`
        <div class="fengshui-row">
          <span class="fengshui-key">Good:</span>
          <span>${day.favourable.slice(0, 4).join(', ')}</span>
        </div>`}
      ${day.type !== 'unlucky' && day.unfavourable.length > 0 && html`
        <div class="fengshui-row">
          <span class="fengshui-key">Avoid:</span>
          <span>${day.unfavourable.slice(0, 3).join(', ')}</span>
        </div>`}
      ${bestDays.length > 0 && html`
        <div class="fengshui-row">
          <span class="fengshui-key">Best interview days:</span>
          <span>${bestDays.join(', ')}</span>
        </div>`}
    </div>`;
}

const STORAGE_KEYS = {
  settings: 'settings',
};
const DEFAULT_SETTINGS = {
  companion_url:   'http://localhost:7878',
  companion_token: '',
};

// -- Utility -------------------------------------------------------------------

const load  = (keys) => chrome.storage.local.get(keys);
const store = (obj)  => chrome.storage.local.set(obj);

// sendMsg is kept only for PARSE_PDF (MuPDF WASM lives in the service worker).
// All companion API calls bypass the service worker entirely — use companionUrl/
// companionHeaders + fetch() directly. The side panel is a persistent web page
// with no idle timeout, so long LLM calls (extract, generate, save) never hit
// the MV3 "message channel closed before a response" error.
const sendMsg = (msg) => chrome.runtime.sendMessage(msg);

async function companionHealth(url = 'http://localhost:7878') {
  try {
    const r = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

// Companion fetch helpers — used by all direct companion calls in this file.
function companionUrl(s) {
  return (s?.companion_url || 'http://localhost:7878').replace(/\/$/, '');
}
function companionHeaders(s) {
  const h = { 'Content-Type': 'application/json' };
  if (s?.companion_token) h['X-Grapply-Token'] = s.companion_token;
  return h;
}

// Persist a saved job into the savedJobs list (max 5, newest first).
async function persistSavedJob(entry) {
  const stored = await load(['savedJobs']);
  const list = (stored.savedJobs || []).filter(j =>
    !(j.title === entry.title && j.company === entry.company)
  );
  list.unshift(entry);
  await store({ savedJobs: list.slice(0, 5) });
}

// Extract a company slug from an ATS URL for matching.
// Returns lowercase slug string or '' if none found.
function extractAtsSlug(url) {
  try {
    const u = new URL(url);
    const host = u.hostname; // e.g. jobs.ashby.com, boards.greenhouse.io
    const parts = u.pathname.split('/').filter(Boolean);
    // Workday: companyname.wd1.myworkdayjobs.com
    const wdMatch = host.match(/^([^.]+)\.wd\d+\.myworkdayjobs\.com$/);
    if (wdMatch) return wdMatch[1].toLowerCase();
    // SmartRecruiters: jobs.smartrecruiters.com/CompanyName/...
    if (host.includes('smartrecruiters.com') && parts[0]) return parts[0].toLowerCase();
    // Ashby: jobs.ashby.com/companyslug/...
    if (host.includes('ashby.com') && parts[0]) return parts[0].toLowerCase();
    // Greenhouse: boards.greenhouse.io/companyslug/...
    if (host.includes('greenhouse.io') && parts[0]) return parts[0].toLowerCase();
    // Lever: jobs.lever.co/companyslug/...
    if (host.includes('lever.co') && parts[0]) return parts[0].toLowerCase();
    // Jobvite: jobs.jobvite.com/companyslug/...
    if (host.includes('jobvite.com') && parts[0]) return parts[0].toLowerCase();
    // Generic: first non-empty path segment
    return (parts[0] || '').toLowerCase();
  } catch { return ''; }
}

// Normalise a string for fuzzy matching: lowercase, strip non-alphanumeric.
function norm(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }

// Score how well an ATS slug matches a saved job's company name (0–1).
function matchScore(slug, company) {
  if (!slug) return 0;
  const s = norm(slug), c = norm(company);
  if (!s || !c) return 0;
  if (s === c) return 1;
  if (c.includes(s) || s.includes(c)) return 0.8;
  // Partial overlap: check if slug is a prefix/suffix of company or vice-versa
  const minLen = Math.min(s.length, c.length);
  if (minLen >= 3 && (c.startsWith(s.slice(0, minLen)) || s.startsWith(c.slice(0, minLen)))) return 0.6;
  return 0;
}

// Self-contained page scraper injected via chrome.scripting.executeScript.
// Must NOT reference any outer variables - it is serialised and run in the page.
function scrapeJobFromPage() {
  const info = {
    title: '', company: '', description: '', location: '', ats: 'generic',
    domain: '', page_text: '', page_html: '', url: '',
  };
  info.domain    = window.location.hostname.replace(/^www\./, '');
  info.url       = window.location.href;
  info.page_html = document.documentElement.outerHTML.slice(0, 500_000);
  info.page_text = (document.body?.innerText || '').slice(0, 12000);

  // Structured extraction — works even when the body is a loading skeleton.
  // These fields are sent as pre_* to the companion so it can skip HTML cleaning
  // entirely when they contain enough data (common for React SPAs with JSON-LD).

  // 1. JSON-LD JobPosting
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data  = JSON.parse(s.textContent);
      const items = Array.isArray(data['@graph']) ? data['@graph'] : [data];
      for (const item of items) {
        if (item['@type'] !== 'JobPosting') continue;
        info.title       = info.title       || item.title || '';
        info.company     = info.company     || (item.hiringOrganization && item.hiringOrganization.name) || '';
        info.location    = info.location    || (item.jobLocation && item.jobLocation.address && item.jobLocation.address.addressLocality) || '';
        if (!info.description && item.description) {
          const div = document.createElement('div');
          div.innerHTML = item.description;
          info.description = (div.textContent || '').trim().slice(0, 6000);
        }
      }
    } catch (e) { /* skip malformed */ }
    if (info.title && info.description) break;
  }

  // 2. OpenGraph / meta tags
  const _meta = name => {
    const el = document.querySelector('meta[property="' + name + '"], meta[name="' + name + '"]');
    return (el && el.content && el.content.trim()) || '';
  };
  if (!info.title)   info.title   = _meta('og:title') || _meta('twitter:title');
  if (!info.company) info.company = _meta('og:site_name');

  // 3. Page title heuristic: "Role @ Company" or "Role | Company"
  if (!info.title || !info.company) {
    const sep = document.title.match(/(.+?)\s*[@|–—-]\s*(.+)/);
    if (sep) {
      if (!info.title)   info.title   = sep[1].trim();
      if (!info.company) info.company = sep[2].replace(/\s*jobs?$/i, '').trim();
    } else if (!info.title) {
      info.title = document.title.trim();
    }
  }

  // 4. Heuristic body block for description (selectors ordered by specificity)
  if (!info.description) {
    const selectors = [
      '[class*="job-description"]', '[class*="jobDescription"]',
      '[id*="job-description"]',    '[class*="posting-body"]',
      '[class*="job-detail"]',      'article', 'main',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const text = el.innerText && el.innerText.trim();
      if (text && text.length > 200) { info.description = text.slice(0, 6000); break; }
    }
  }

  return info;
}

function captureFormSnapshot() {
  const fields = [];
  document.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(el => {
    const labelEl = el.id
      ? document.querySelector(`label[for="${el.id}"]`)
      : el.closest('label');
    const label = (labelEl?.textContent || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 100);
    fields.push({
      tag:         el.tagName.toLowerCase(),
      type:        el.type || el.tagName.toLowerCase(),
      id:          el.id || '',
      name:        el.name || '',
      placeholder: el.placeholder || '',
      label,
      aria_label:  el.getAttribute('aria-label') || '',
    });
  });
  return fields.slice(0, 50);
}

// -- Sub-components ------------------------------------------------------------

function CompanionBanner({ health }) {
  if (health === undefined) return null; // still checking
  if (health) {
    const isLocal = health.ai_endpoint?.includes('localhost') || health.ai_endpoint?.includes('127.0.0.1');
    const providerLabel = health.ai_provider === 'claude'  ? 'Anthropic'
      : health.ai_provider === 'ollama' ? 'Ollama'
      : isLocal ? 'local LLM'
      : 'OpenAI';
    const modelName  = (health.ai_model || '').replace(/\.gguf$/i, '').replace(/^local$/, '');
    const modelLabel = modelName ? ` - ${modelName}` : '';
    const ai = health.ai_reachable
      ? `${providerLabel}${modelLabel}`
      : `${providerLabel} - not reachable`;
    const warnings = health.setup_warnings || [];
    return html`
      <div class="banner ok">Companion running - ${ai}</div>
      ${warnings.map(w => html`
        <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:6px 10px;margin-bottom:6px;font-size:11px;color:#856404;">
          ⚠ ${w}
        </div>`)}`;
  }
  const cmd = 'cd companion && python main.py';
  const copy = () => navigator.clipboard.writeText(cmd);
  return html`
    <div class="banner">
      Companion not running
      <button class="banner-cmd" onClick=${copy} title="Click to copy">
        python main.py
      </button>
    </div>`;
}

function ListPageBanner({ url }) {
  const isListPage =
    /linkedin\.com\/jobs\/(collections|search)/i.test(url) ||
    /seek\.co\.nz\/jobs[\/?]/i.test(url) ||
    /indeed\.com\/jobs[\/?]/i.test(url) ||
    /linkedin\.com\/jobs\/?$/i.test(url);
  if (!isListPage) return null;
  return html`
    <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:8px 12px;margin-bottom:10px;font-size:12px;color:#856404;">
      📋 <strong>List page detected</strong> — open a specific job posting, then scan again.
    </div>`;
}

function FileStatusBar({ status }) {
  if (!status) return null;
  const files = [
    { key: 'cover_letter', label: 'Cover letter' },
    { key: 'summary',      label: 'Summary' },
    { key: 'score',        label: 'Score' },
    { key: 'insight',      label: 'Insights' },
  ];
  return html`
    <div style="margin-top:8px;font-size:11px;color:#555;">
      ${files.map(f => html`
        <span style="margin-right:8px;">
          ${status[f.key] ? '✅' : '⏳'} ${f.label}
        </span>`)}
    </div>`;
}

// -- Generate tab --------------------------------------------------------------

function GenerateTab({ settings, health }) {
  const [scanState,  setScanState]  = useState('idle'); // idle | scanning | learning | found
  const [title,      setTitle]      = useState('');
  const [company,    setCompany]    = useState('');
  const [desc,       setDesc]       = useState('');
  const [jobDomain,  setJobDomain]  = useState('');
  const [scanError,  setScanError]  = useState('');
  const [status,     setStatus]     = useState('idle'); // idle | loading | done | error
  const [result,     setResult]     = useState(null);
  const [errMsg,     setErrMsg]     = useState('');
  const [savedPaths, setSavedPaths] = useState(null);
  const [tabUrl,     setTabUrl]     = useState('');
  const [jobId,      setJobId]      = useState(null);
  const [fileStatus, setFileStatus] = useState(null);
  const [fillState, setFillState] = useState('idle'); // idle | loading | done | error
  const [fillCount, setFillCount] = useState(0);
  const [atsMode,    setAtsMode]    = useState(false);
  const [atsCandidates, setAtsCandidates] = useState(null); // null | SavedJob[]

  useEffect(() => {
    chrome.tabs.query({ active: true, currentWindow: true })
      .then(([tab]) => setTabUrl(tab?.url || ''));
  }, []);

  useEffect(() => {
    if (!jobId) return;
    let active = true;
    let attempts = 0;
    const MAX_ATTEMPTS = 120;
    const poll = async () => {
      if (!active || attempts >= MAX_ATTEMPTS) return;
      attempts++;
      try {
        const pollResp = await fetch(`${companionUrl(settings)}/jobs/${encodeURIComponent(jobId)}/files`, {
          headers: companionHeaders(settings),
        });
        const r = pollResp.ok ? await pollResp.json() : null;
        if (r && !r.error) {
          setFileStatus(r);
          const allDone = r.cover_letter && r.summary && r.score && r.insight;
          if (!allDone) setTimeout(poll, 5000);
        } else {
          setTimeout(poll, 5000);
        }
      } catch {
        setTimeout(poll, 5000);
      }
    };
    poll();
    return () => { active = false; };
  }, [jobId, settings]);

  const loadAtsJob = useCallback((job, atsDomain) => {
    setResult({ cover_letter_md: job.cover_letter_md });
    setSavedPaths({ md_path: `Last: ${job.title} @ ${job.company}` });
    setTitle(job.title || '');
    setCompany(job.company || '');
    setJobDomain(atsDomain);
    setAtsMode(true);
    setAtsCandidates(null);
    setScanState('found');
  }, []);

  const scanPage = useCallback(async () => {
    setScanState('scanning');
    setScanError('');
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) throw new Error('No active tab found.');
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: scrapeJobFromPage,
      });
      const j = results[0]?.result;
      const domain = j?.domain || new URL(tab.url || '').hostname.replace(/^www\./, '');

      // Ask companion to determine page mode — reliable HTML+domain analysis,
      // no more noisy client-side formFieldCount heuristic.
      const scanFetch = await fetch(`${companionUrl(settings)}/scan`, {
        method:  'POST',
        headers: companionHeaders(settings),
        body:    JSON.stringify({ domain, page_html: j?.page_html || '', url: j?.url || tab.url || '' }),
      });
      if (!scanFetch.ok) {
        const detail = await scanFetch.json().catch(() => ({}));
        throw new Error(detail.detail || `Scan failed: ${scanFetch.status}`);
      }
      const scanResp = await scanFetch.json();
      if (scanResp?.error) throw new Error(scanResp.error);
      const isAts = scanResp?.mode === 'ats_form';

      if (isAts) {
        const stored = await load(['savedJobs']);
        const savedJobs = stored.savedJobs || [];
        if (!savedJobs.length) {
          setScanError('ATS form detected but no recent applications found — generate a cover letter on the job posting first.');
          setScanState('idle');
          return;
        }
        // Try to match company slug from URL
        const slug = extractAtsSlug(tab.url || '');
        const scored = savedJobs.map(job => ({ job, score: matchScore(slug, job.company) }));
        const best = scored.reduce((a, b) => b.score > a.score ? b : a);
        if (best.score >= 0.8) {
          loadAtsJob(best.job, domain);
        } else {
          setScanState('idle');
          setAtsCandidates(savedJobs);
          setJobDomain(domain);
          setAtsMode(true);
        }
        return;
      }

      setScanState('learning');
      // Clear previous job data immediately so stale results don't show
      setTitle(''); setCompany(''); setDesc(''); setJobDomain(''); setResult(null);
      const extractFetch = await fetch(`${companionUrl(settings)}/extract`, {
        method:  'POST',
        headers: companionHeaders(settings),
        body:    JSON.stringify({ domain, page_text: j?.page_text || '', page_html: j?.page_html || '', url: j?.url || tab.url || '',
                                 pre_title: j?.title || '', pre_company: j?.company || '', pre_location: j?.location || '', pre_description: j?.description || '' }),
      });
      if (!extractFetch.ok) {
        const detail = await extractFetch.json().catch(() => ({}));
        throw new Error(detail.detail || `Extract failed: ${extractFetch.status}`);
      }
      const resp = await extractFetch.json();
      if (resp?.error) throw new Error(resp.error);
      if (!resp?.title) {
        setScanError('Could not detect job info — open a specific job posting and try again.');
        setScanState('idle');
        return;
      }
      setTitle(resp.title || '');
      setCompany(resp.company || '');
      setDesc(resp.description || '');
      setJobDomain(domain);
      setScanState('found');
    } catch (err) {
      setScanError(err.message || 'Grab failed.');
      setScanState('idle');
    }
  }, [settings, loadAtsJob]);

  const reset = useCallback(() => {
    setScanState('idle');
    setScanError('');
    setTitle(''); setCompany(''); setDesc(''); setJobDomain('');
    setResult(null); setSavedPaths(null);
    setStatus('idle'); setErrMsg('');
    setJobId(null); setFileStatus(null); setAtsMode(false); setAtsCandidates(null); setFillState('idle'); setFillCount(0);
  }, []);

  const generate = useCallback(async () => {
    setStatus('loading');
    setErrMsg('');
    setResult(null);
    setSavedPaths(null);
    setJobId(null);
    setFileStatus(null);
    try {
      const genFetch = await fetch(`${companionUrl(settings)}/generate`, {
        method:  'POST',
        headers: companionHeaders(settings),
        body:    JSON.stringify({
          job_title:            title,
          company,
          job_description:      desc,
          resume_text:          '',
          per_job_instructions: '',
        }),
      });
      if (!genFetch.ok) {
        const detail = await genFetch.json().catch(() => ({}));
        throw new Error(detail.detail || `Generate failed: ${genFetch.status}`);
      }
      const resp = await genFetch.json();
      if (resp.error) throw new Error(resp.error);
      setResult(resp);
      setStatus('done');
      fetch(`${companionUrl(settings)}/save`, {
        method:  'POST',
        headers: companionHeaders(settings),
        body:    JSON.stringify({
          company,
          role:              title,
          cover_letter_md:   resp.cover_letter_md,
          cover_letter_html: resp.cover_letter_html,
          domain:            jobDomain        || '',
          job_description:   desc             || '',
          resume_text:       '',
          ai_provider:       settings?.provider  || health?.ai_provider || '',
          ai_model:          settings?.model     || health?.ai_model    || '',
        }),
      }).then(async r => {
        if (!r.ok) return;
        const saved = await r.json();
        if (saved?.md_path) {
          setSavedPaths(saved);
          persistSavedJob({ title, company, jobDomain, cover_letter_md: resp.cover_letter_md, savedAt: Date.now() });
        }
        if (saved?.job_id) setJobId(saved.job_id);
      }).catch(err => console.warn('[grapply] Auto-save failed:', err.message));
    } catch (e) {
      setErrMsg(e.message);
      setStatus('error');
    }
  }, [title, company, desc, jobDomain, settings]);

  const fillForm = useCallback(async () => {
    if (fillState === 'loading') return;
    try {
      setFillState('loading');
      setErrMsg('');
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) { setFillState('idle'); return; }
      const snapResults = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: captureFormSnapshot,
      });
      const formSnapshot = snapResults[0]?.result || [];
      const p = health?.profile || {};
      const fillData = {
        name:                 p.name || '',
        email:                p.email || '',
        phone:                p.phone || '',
        linkedin:             p.linkedin || '',
        github:               p.github || '',
        website:              p.website || '',
        location:             p.location || '',
        work_authorization:   p.work_authorization || '',
        availability:         p.availability || '',
        salary_expectation:   p.salary_expectation || '',
        cover_letter:         result?.cover_letter_md || '',
      };

      // Call /fill directly — bypasses the service worker to avoid MV3 idle timeout
      // on long LLM calls (first-time filler generation can take ~2 min).
      const headers = { 'Content-Type': 'application/json' };
      if (settings?.companion_token) headers['X-Grapply-Token'] = settings.companion_token;
      const resp = await fetch(`${settings?.companion_url || 'http://localhost:7878'}/fill`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ domain: jobDomain, form_snapshot: formSnapshot, fill_data: fillData }),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        setErrMsg(detail.detail || `Fill failed: ${resp.status}`);
        setFillState('error');
        return;
      }
      const r = await resp.json();

      const fillResults = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: (operations, fillData) => {
          let filled = 0;
          const errors = [];
          const inputSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          const textareaSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
          for (const op of operations) {
            const el = document.querySelector(op.selector);
            if (!el) { errors.push(`Not found: ${op.selector}`); continue; }
            const value = fillData[op.value_key] || '';
            const setter = el.tagName === 'TEXTAREA' ? textareaSetter : inputSetter;
            try {
              setter.call(el, value);
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              filled++;
            } catch (e) {
              errors.push(`Failed ${op.selector}: ${e.message}`);
            }
          }
          return { filled, errors };
        },
        args: [r.operations, fillData],
      });
      const fillResult = fillResults[0]?.result;
      if (fillResult?.filled === 0) {
        const errs = fillResult.errors?.join('; ') || 'No fields matched';
        setErrMsg(`Filler ran but filled 0 fields — ${errs}. Regenerating next time.`);
        // Delete cached filler so it regenerates fresh next attempt
        fetch(`${settings?.companion_url || 'http://localhost:7878'}/fillers/${encodeURIComponent(jobDomain)}`, {
          method: 'DELETE', headers,
        }).catch(() => {});
        setFillState('error');
        return;
      }
      setFillCount(fillResult?.filled || 0);
      setFillState('done');
    } catch (err) {
      setErrMsg('Fill form failed: ' + err.message);
      setFillState('error');
    }
  }, [result, jobDomain, settings, health, fillState]);

  // ── ATS job picker (ambiguous match) ─────────────────────────────────────────
  if (atsCandidates) return html`
    <div class="panel">
      <${FengShuiPanel} />
      <p style="font-size:12px;color:var(--muted);margin-bottom:8px">
        ATS form detected — which application are you filling in?
      </p>
      ${atsCandidates.map(job => html`
        <button key=${job.title + job.company} class="btn btn-secondary btn-full"
          style="margin-bottom:6px;text-align:left"
          onClick=${() => loadAtsJob(job, jobDomain)}>
          <strong>${job.title}</strong> <span style="color:var(--muted)">@ ${job.company}</span>
        </button>`)}
      <button class="btn" style="font-size:11px;margin-top:4px" onClick=${reset}>↩ Cancel</button>
    </div>`;

  // ── Grab screen ───────────────────────────────────────────────────────────────
  if (scanState !== 'found') return html`
    <div class="panel">
      <${FengShuiPanel} />
      ${ListPageBanner({ url: tabUrl })}
      <button class="btn btn-primary btn-full" style="margin-bottom:8px"
        disabled=${scanState === 'scanning' || scanState === 'learning'} onClick=${scanPage}>
        ${scanState === 'scanning'
          ? html`<span class="spinner"></span> Grabbing...`
          : scanState === 'learning'
          ? html`<span class="spinner"></span> Learning this site...`
          : 'Grab Page'}
      </button>
      ${scanState === 'learning' && html`
        <p style="color:var(--muted);font-size:11px;margin-top:0">
          First visit — companion is building a parser. Takes ~30s once, instant after.
        </p>`}
      ${scanError && html`
        <p style="color:var(--danger);font-size:11px;margin-top:0">${scanError}</p>`}
    </div>`;

  // ── Generate screen ───────────────────────────────────────────────────────────
  return html`
    <div class="panel">
      <${FengShuiPanel} />

      <div style="margin-bottom:10px;font-size:13px;">
        <strong>${title}</strong>
        <span style="color:var(--muted)"> @ ${company}</span>
        ${atsMode && html`<span style="font-size:11px;color:var(--muted);display:block;margin-top:2px">
          🖊 ATS form ready to fill
        </span>`}
      </div>

      ${!atsMode && html`
        <div class="btn-row">
          <button class="btn btn-primary btn-full"
            disabled=${status === 'loading'} onClick=${generate}>
            ${status === 'loading'
              ? html`<span class="spinner"></span> Generating...`
              : 'Generate Cover Letter'}
          </button>
          <button class="btn btn-secondary" style="font-size:11px"
            onClick=${reset}>↩</button>
        </div>`}

      ${atsMode && html`
        <button class="btn btn-secondary" style="font-size:11px;margin-bottom:8px"
          onClick=${reset}>↩ Back</button>`}

      ${errMsg && html`
        <p style="color:var(--danger);font-size:11px;margin-top:8px">${errMsg}</p>`}

      ${savedPaths && html`
        <div class="saved-path">${atsMode ? savedPaths.md_path : 'Saved: ' + savedPaths.md_path}</div>`}
      ${savedPaths && result && html`
        <div class="btn-row" style="margin-top:8px;">
          <button class="btn btn-secondary btn-full"
            disabled=${fillState === 'loading'} onClick=${fillForm}>
            ${fillState === 'loading'
              ? html`<span class="spinner"></span> Filling form...`
              : fillState === 'done'
              ? `✅ ${fillCount} field${fillCount !== 1 ? 's' : ''} filled`
              : 'Fill Form'}
          </button>
        </div>`}
      ${FileStatusBar({ status: fileStatus })}

      ${!atsMode && result && html`
        <div class="preview" dangerouslySetInnerHTML=${{
          __html: marked.parse(result.cover_letter_md || '')
        }} />`}
    </div>`;
}

// -- Root App ------------------------------------------------------------------

function App() {
  const [health,   setHealth]   = useState(undefined);
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);

  // Keep a ref to the current settings so the heartbeat interval always uses
  // the latest companion_url without needing to re-register the timer.
  const settingsRef = useRef(DEFAULT_SETTINGS);

  useEffect(() => {
    load([STORAGE_KEYS.settings]).then(async d => {
      const raw = d.settings || {};
      const s = {
        companion_url: typeof raw.companion_url === 'string' && raw.companion_url.trim()
          ? raw.companion_url : DEFAULT_SETTINGS.companion_url,
        companion_token: typeof raw.companion_token === 'string'
          ? raw.companion_token : DEFAULT_SETTINGS.companion_token,
      };
      setSettings(s);
      settingsRef.current = s;
      store({ [STORAGE_KEYS.settings]: s });

      const h = await companionHealth(s.companion_url);
      setHealth(h);

      // Seed savedJobs from disk if storage is empty and companion is up
      if (h) {
        const stored = await load(['savedJobs']);
        if (!stored.savedJobs?.length) {
          try {
            const headers = s.companion_token
              ? { Authorization: `Bearer ${s.companion_token}` } : {};
            const r = await fetch(`${s.companion_url}/jobs/recent`, { headers });
            if (r.ok) {
              const data = await r.json();
              if (data.jobs?.length) {
                await store({ savedJobs: data.jobs });
                console.log('[grapply] Seeded', data.jobs.length, 'saved jobs from disk');
              }
            }
          } catch (e) {
            console.warn('[grapply] Could not seed savedJobs:', e.message);
          }
        }
      }
    });

    // Heartbeat: poll /health every 10 s so the pill goes red immediately
    // after the companion is killed, without requiring a panel re-open.
    const timer = setInterval(async () => {
      const h = await companionHealth(settingsRef.current.companion_url);
      setHealth(h);
    }, 10_000);

    return () => clearInterval(timer);
  }, []);

  const statsUrl    = `${companionUrl(settings)}/stats`;
  const settingsUrl = `${companionUrl(settings)}/settings`;

  return html`
    <div>
      <div class="header">
        <div class="logo">gr<span>apply</span></div>
        <span class="pill ${health ? 'pill-ok' : health === undefined ? 'pill-loading' : 'pill-err'}">
          ${health ? 'online' : health === undefined ? '...' : 'offline'}
        </span>
      </div>

      <${CompanionBanner} health=${health} />
      <${GenerateTab} settings=${settings} health=${health} />

      <div style="padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px">
        <button class="btn btn-secondary" style="font-size:11px;padding:6px;flex:1"
          onClick=${() => chrome.tabs.create({ url: settingsUrl })}>
          Settings
        </button>
        <button class="btn btn-secondary" style="font-size:11px;padding:6px;flex:1"
          onClick=${() => chrome.tabs.create({ url: statsUrl })}>
          Usage Stats
        </button>
      </div>
    </div>`;
}

// -- Mount ---------------------------------------------------------------------

render(html`<${App} />`, document.getElementById('app'));
