/**
 * overhired - popup UI (Preact + htm, no build step)
 */
import { h, render }       from '../vendor/preact.module.js';
import { useState, useEffect, useCallback } from '../vendor/preact-hooks.module.js';
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
const sendMsg = (msg) => chrome.runtime.sendMessage(msg);

async function companionHealth(url = 'http://localhost:7878') {
  try {
    const r = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
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
    domain: '', page_text: '', formFieldCount: 0,
  };
  info.domain = window.location.hostname.replace(/^www\./, '');
  info.page_text = (document.body?.innerText || '').slice(0, 12000);
  info.formFieldCount = document.querySelectorAll('input:not([type=hidden]), textarea, select').length;
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
    return html`<div class="banner ok">Companion running - ${ai}</div>`;
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
        const r = await sendMsg({ type: 'POLL_FILES', jobId, settings });
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

      // Detect ATS application form (≥3 visible fields) — skip extraction
      if ((j?.formFieldCount || 0) >= 3) {
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
          // Clear winner — auto-load
          loadAtsJob(best.job, j.domain);
        } else {
          // Ambiguous — show picker
          setScanState('idle');
          setAtsCandidates(savedJobs);
          setJobDomain(j.domain);
          setAtsMode(true);
        }
        return;
      }

      setScanState('learning');
      const url = tab.url || '';
      const resp = await sendMsg({
        type: 'EXTRACT',
        domain: j?.domain || new URL(url).hostname.replace(/^www\./, ''),
        page_text: j?.page_text || '',
        settings,
      });
      if (resp?.error) throw new Error(resp.error);
      if (!resp?.title) {
        setScanError('Could not detect job info — open a specific job posting and try again.');
        setScanState('idle');
        return;
      }
      setTitle(resp.title || '');
      setCompany(resp.company || '');
      setDesc(resp.description || '');
      setJobDomain(j?.domain || new URL(url).hostname.replace(/^www\./, ''));
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
    setJobId(null); setFileStatus(null); setAtsMode(false); setAtsCandidates(null); setFillState('idle');
  }, []);

  const generate = useCallback(async () => {
    setStatus('loading');
    setErrMsg('');
    setResult(null);
    setSavedPaths(null);
    setJobId(null);
    setFileStatus(null);
    try {
      const resp = await sendMsg({
        type: 'GENERATE',
        job: { title, company, description: desc },
        settings,
      });
      if (resp.error) throw new Error(resp.error);
      setResult(resp);
      setStatus('done');
      sendMsg({
        type: 'SAVE',
        company, role: title,
        coverMd: resp.cover_letter_md,
        coverHtml: resp.cover_letter_html,
        domain: jobDomain,
        jobDescription: desc,
        resumeText: '',
        settings,
      }).then(r => {
        if (r?.md_path) {
          setSavedPaths(r);
          persistSavedJob({ title, company, jobDomain, cover_letter_md: resp.cover_letter_md, savedAt: Date.now() });
        }
        if (r?.job_id) setJobId(r.job_id);
      }).catch(err => console.warn('[overhired] Auto-save failed:', err.message));
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
      const fillData = {
        name:         health?.profile?.name || '',
        email:        health?.profile?.email || '',
        phone:        health?.profile?.phone || '',
        cover_letter: result?.cover_letter_md || '',
      };

      // Call /fill directly — bypasses the service worker to avoid MV3 idle timeout
      // on long LLM calls (first-time filler generation can take ~2 min).
      const headers = { 'Content-Type': 'application/json' };
      if (settings?.companion_token) headers['X-Overhired-Token'] = settings.companion_token;
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

      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: (code, fillData) => {
          try {
            const fn = new Function('data', `${code}\nreturn fill(data);`);
            return fn(fillData);
          } catch (e) {
            return { filled: 0, errors: [e.message] };
          }
        },
        args: [r.code, fillData],
      });
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
              ? '✅ Form filled'
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
                console.log('[overhired] Seeded', data.jobs.length, 'saved jobs from disk');
              }
            }
          } catch (e) {
            console.warn('[overhired] Could not seed savedJobs:', e.message);
          }
        }
      }
    });
  }, []);

  return html`
    <div>
      <div class="header">
        <div class="logo">over<span>hired</span></div>
        <span class="pill ${health ? 'pill-ok' : health === undefined ? 'pill-loading' : 'pill-err'}">
          ${health ? 'online' : health === undefined ? '...' : 'offline'}
        </span>
      </div>

      <${CompanionBanner} health=${health} />
      <${GenerateTab} settings=${settings} health=${health} />
    </div>`;
}

// -- Mount ---------------------------------------------------------------------

render(html`<${App} />`, document.getElementById('app'));
