/**
 * overhired - popup UI (Preact + htm, no build step)
 */
import { h, render }       from '../vendor/preact.module.js';
import { useState, useEffect, useCallback } from '../vendor/preact-hooks.module.js';
import { marked }          from '../vendor/marked.esm.js';
import htm                 from '../vendor/htm.module.js';

const html = htm.bind(h);

// Detect whether we are running as a popup or as a full browser tab.
// When opened via chrome.tabs.create the URL carries ?tab=settings.
const SEARCH      = new URLSearchParams(window.location.search);
const IN_FULL_TAB = SEARCH.has('tab');

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

function sanitizeSettings(raw = {}) {
  return {
    companion_url: typeof raw.companion_url === 'string' && raw.companion_url.trim()
      ? raw.companion_url
      : DEFAULT_SETTINGS.companion_url,
    companion_token: typeof raw.companion_token === 'string'
      ? raw.companion_token
      : DEFAULT_SETTINGS.companion_token,
  };
}

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

// Self-contained page scraper injected via chrome.scripting.executeScript.
// Must NOT reference any outer variables - it is serialised and run in the page.
function scrapeJobFromPage() {
  const info = {
    title: '', company: '', description: '', location: '', ats: 'generic',
    domain: '', page_text: '',
  };
  info.domain = window.location.hostname.replace(/^www\./, '');
  info.page_text = (document.body?.innerText || '').slice(0, 12000);
  return info;
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

function GenerateTab({ settings }) {
  const [scanState,  setScanState]  = useState('idle'); // idle | scanning | learning | found
  const [title,      setTitle]      = useState('');
  const [company,    setCompany]    = useState('');
  const [desc,       setDesc]       = useState('');
  const [jobDomain,  setJobDomain]  = useState('');
  const [ats,        setAts]        = useState('generic');
  const [showDesc,   setShowDesc]   = useState(false);
  const [scanError,  setScanError]  = useState('');
  const [perJob,     setPerJob]     = useState('');
  const [status,     setStatus]     = useState('idle'); // idle | loading | done | error
  const [result,     setResult]     = useState(null);
  const [errMsg,     setErrMsg]     = useState('');
  const [savedPaths, setSavedPaths] = useState(null);
  const [tabUrl,     setTabUrl]     = useState('');
  const [jobId,      setJobId]      = useState(null);
  const [fileStatus, setFileStatus] = useState(null);

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

  const canGenerate = status !== 'loading';

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

      if (j?.title) {
        setTitle(j.title || '');
        setCompany(j.company || '');
        setDesc(j.description || '');
        setJobDomain(j.domain || '');
        setAts(j.ats || 'generic');
        setScanState('found');
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
        setScanError('Could not detect job info - make sure you are on a job posting page.');
        setScanState('idle');
        return;
      }
      setTitle(resp.title || '');
      setCompany(resp.company || '');
      setDesc(resp.description || '');
      setJobDomain(new URL(url).hostname.replace(/^www\./, ''));
      setAts('generic');
      setScanState('found');
    } catch (err) {
      setScanError(err.message || 'Scan failed.');
      setScanState('idle');
    }
  }, [settings]);

  const reset = useCallback(() => {
    setScanState('idle');
    setScanError('');
    setTitle('');
    setCompany('');
    setDesc('');
    setJobDomain('');
    setResult(null);
    setSavedPaths(null);
    setStatus('idle');
    setErrMsg('');
    setJobId(null);
    setFileStatus(null);
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
        job: { title, company, description: desc, ats },
        perJobInstructions: perJob,
        settings,
      });
      if (resp.error) throw new Error(resp.error);
      setResult(resp);
      setStatus('done');
      sendMsg({
        type: 'SAVE',
        company,
        role: title,
        coverMd: resp.cover_letter_md,
        coverHtml: resp.cover_letter_html,
        domain: jobDomain,
        jobDescription: desc,
        resumeText: '',
        settings,
      }).then(r => {
        if (r?.md_path) setSavedPaths(r);
        if (r?.job_id) setJobId(r.job_id);
      }).catch(err => console.warn('[overhired] Auto-save failed:', err.message));
    } catch (e) {
      setErrMsg(e.message);
      setStatus('error');
    }
  }, [title, company, desc, jobDomain, ats, perJob, settings]);

  const fillForm = useCallback(async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) { setErrMsg('Could not access the active tab.'); return; }
    try {
      const resp = await chrome.tabs.sendMessage(tab.id, {
        type: 'FILL_FORM',
        coverLetter: result?.cover_letter_md || '',
      });
      if (resp?.error) setErrMsg('Fill form: ' + resp.error);
    } catch (err) {
      setErrMsg(
        err.message?.includes('Receiving end does not exist')
          ? 'Content script not ready - refresh the job page and try again.'
          : 'Fill form failed: ' + err.message
      );
    }
  }, [result]);

  if (scanState !== 'found') return html`
    <div class="panel">
      <${FengShuiPanel} />
      ${ListPageBanner({ url: tabUrl })}
      <button class="btn btn-primary btn-full" style="margin-bottom:8px"
        disabled=${scanState === 'scanning' || scanState === 'learning'} onClick=${scanPage}>
        ${scanState === 'scanning'
          ? html`<span class="spinner"></span> Scanning...`
          : scanState === 'learning'
          ? html`<span class="spinner"></span> Learning this site...`
          : 'Scan Page'}
      </button>
      ${scanState === 'learning' && html`
        <p style="color:var(--muted);font-size:11px;margin-top:0">
          This site hasn't been seen before. The companion is generating a parser - this takes ~30s once, then it's instant.
        </p>`}
      ${scanError && html`
        <p style="color:var(--muted);font-size:11px;margin-top:0">${scanError}</p>`}
    </div>`;

  return html`
    <div class="panel">
      <${FengShuiPanel} />
      ${ListPageBanner({ url: tabUrl })}

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <div class="field" style="margin-bottom:0">
          <label>Role</label>
          <input type="text" value=${title} onInput=${e => setTitle(e.target.value)} />
        </div>
        <div class="field" style="margin-bottom:0">
          <label>Company</label>
          <input type="text" value=${company} onInput=${e => setCompany(e.target.value)} />
        </div>
      </div>

      <div class="field">
        <label style="cursor:pointer" onClick=${() => setShowDesc(v => !v)}>
          Job Description ${desc ? '(loaded)' : '(none)'} ${showDesc ? '[hide]' : '[show/edit]'}
        </label>
        ${showDesc && html`
          <textarea rows=5 value=${desc} onInput=${e => setDesc(e.target.value)}
            placeholder="Paste job description..." />`}
      </div>

      <div class="field">
        <label>Notes for this application</label>
        <textarea placeholder="e.g. Mention I'm willing to relocate..."
          value=${perJob} onInput=${e => setPerJob(e.target.value)} rows=2 />
      </div>

      <div class="btn-row">
        <button class="btn btn-primary btn-full" disabled=${!canGenerate} onClick=${generate}>
          ${status === 'loading'
            ? html`<span class="spinner"></span> Generating...`
            : 'Generate Cover Letter'}
        </button>
        ${result && html`
          <button class="btn btn-secondary" onClick=${fillForm}>Fill Form</button>`}
      </div>

      <div style="margin-top:8px;text-align:right">
        <button class="btn btn-secondary" style="font-size:11px;padding:4px 10px"
          onClick=${reset}>Scan another page</button>
      </div>

      ${status === 'error' && html`
        <p style="color:var(--danger);font-size:11px;margin-top:8px">${errMsg}</p>`}

      ${savedPaths && html`
        <div class="saved-path">Saved: ${savedPaths.md_path}</div>`}
      ${FileStatusBar({ status: fileStatus })}

      ${result && html`
        <div class="preview" dangerouslySetInnerHTML=${{
          __html: marked.parse(result.cover_letter_md || '')
        }} />`}
    </div>`;
}

// -- Settings tab --------------------------------------------------------------

function SettingsTab({ settings, onSave }) {
  const [s, setS] = useState(settings);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setS(settings);
  }, [settings]);

  const field = (key) => ({
    value: s[key] ?? '',
    onInput: e => setS(prev => ({ ...prev, [key]: e.target.value })),
  });

  const saveAll = async () => {
    const next = sanitizeSettings(s);
    await store({ [STORAGE_KEYS.settings]: next });
    onSave(next);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  return html`
    <div class="panel">
      <div class="settings-section">
        <div class="settings-title">Companion Connection</div>
        <div class="field">
          <label>URL</label>
          <input type="text" ...${field('companion_url')}
            placeholder="http://localhost:7878" />
        </div>
        <div class="field">
          <label>Auth Token <span style="color:var(--muted)">(optional)</span></label>
          <input type="password" ...${field('companion_token')}
            placeholder="Match auth_token in companion config.toml" />
        </div>
      </div>

      <button class="btn btn-primary btn-full" onClick=${saveAll}>
        ${saved ? 'Saved!' : 'Save Settings'}
      </button>
    </div>`;
}

// -- Root App ------------------------------------------------------------------

function App() {
  const [tab,      setTab]      = useState(IN_FULL_TAB ? 'settings' : 'generate');
  const [health,   setHealth]   = useState(undefined);
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);

  useEffect(() => {
    load([STORAGE_KEYS.settings]).then(d => {
      const s = sanitizeSettings(d.settings || {});
      setSettings(s);
      store({ [STORAGE_KEYS.settings]: s });
      companionHealth(s.companion_url).then(setHealth);
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

      <div class="tabs">
        <button class=${'tab' + (tab === 'generate' ? ' active' : '')}
          onClick=${() => setTab('generate')}>Generate</button>
        <button class=${'tab' + (tab === 'settings' ? ' active' : '')}
          onClick=${() => IN_FULL_TAB
            ? setTab('settings')
            : chrome.tabs.create({ url: chrome.runtime.getURL('popup/popup.html') + '?tab=settings' })
          }>Settings</button>
      </div>

      ${tab === 'generate'
        ? html`<${GenerateTab} settings=${settings} />`
        : html`<${SettingsTab} settings=${settings} onSave=${s => {
            setSettings(s);
            companionHealth(s.companion_url).then(setHealth);
          }} />`}
    </div>`;
}

// -- Mount ---------------------------------------------------------------------

render(html`<${App} />`, document.getElementById('app'));
