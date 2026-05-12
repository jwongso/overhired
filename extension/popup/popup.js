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
  const tomorrow = new Date(Date.now() + 86400000);
  const twoWeeks = new Date(Date.now() + 15 * 86400000);
  const fmt = d => d.toISOString().slice(0, 10);
  try {
    const [dayRes, bestRes] = await Promise.all([
      fetch(`${AUSPICE_URL}/today`).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch(`${AUSPICE_URL}/best?activity=interview&from=${fmt(tomorrow)}&to=${fmt(twoWeeks)}&weekend=false`)
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
        <span class="fengshui-badge">fengshui</span>
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
  resume:    'resume_text',
  profile:   'user_profile',
  settings:  'settings',
};
const DEFAULT_SETTINGS = {
  provider:            '',               // empty = use companion's config.toml
  endpoint:            '',
  model:               '',
  api_key:             '',
  global_instructions: '',
  easter_egg:          false,
  easter_egg_text:     '',
  companion_url:       'http://localhost:7878',
  companion_token:     '',
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

// Self-contained page scraper injected via chrome.scripting.executeScript.
// Must NOT reference any outer variables - it is serialised and run in the page.
function scrapeJobFromPage() {
  const ATS_PATTERNS = [
    { name: 'greenhouse',      pattern: /greenhouse\.io|boards\.greenhouse/i },
    { name: 'ashby',           pattern: /ashbyhq\.com/i },
    { name: 'workable',        pattern: /workable\.com/i },
    { name: 'workday',         pattern: /myworkdayjobs\.com/i },
    { name: 'lever',           pattern: /jobs\.lever\.co/i },
    { name: 'linkedin',        pattern: /linkedin\.com\/jobs/i },
    { name: 'smartrecruiters', pattern: /smartrecruiters\.com/i },
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

  function meta(name) {
    const el = document.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
    return el?.content?.trim() || '';
  }

  function stripHtml(html) {
    const div = document.createElement('div');
    div.innerHTML = html;
    return div.textContent?.trim() || '';
  }

  function findDescBlock() {
    const selectors = [
      '[class*="job-description"]', '[class*="jobDescription"]',
      '[class*="job_description"]', '[id*="job-description"]',
      '[class*="posting-body"]',    '[class*="description"]',
      '[class*="job-detail"]',      'article', 'main',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) {
        const text = el.innerText?.trim();
        if (text && text.length > 200) return text.slice(0, 6000);
      }
    }
    return document.body?.innerText?.slice(0, 6000) || '';
  }

  const info = { title: '', company: '', location: '', description: '', ats: detectATS() };

  // 1. LinkedIn: parse bpr-guid hidden JSON elements
  if (/linkedin\.com\/jobs/i.test(window.location.href)) {
    const codeEls = document.querySelectorAll('code[id^="bpr-guid-"]');
    for (const el of codeEls) {
      try {
        const parsed = JSON.parse(el.textContent);
        for (const item of (parsed.included || [])) {
          if (!info.title && item.$type?.includes('jobs.JobPosting') && item.title) {
            info.title = item.title;
            if (item.companyDetails?.name) info.company = item.companyDetails.name;
            if (item.description?.text)    info.description = item.description.text.slice(0, 6000);
          }
          if (!info.location && item.$type?.includes('.Geo') && item.defaultLocalizedName) {
            info.location = item.defaultLocalizedName;
          }
          if (!info.company && item.$type?.includes('organization.Company') && item.name) {
            info.company = item.name;
          }
        }
        if (info.title) break;
      } catch { /* skip malformed */ }
    }
    if (!info.description) {
      const descEl = document.querySelector('#job-details, .jobs-description-content__text');
      if (descEl) {
        info.description = descEl.innerText.trim()
          .replace(/\nSee how you compare[\s\S]*/i, '')
          .replace(/\nCandidates who clicked apply[\s\S]*/i, '')
          .trim().slice(0, 6000);
      }
    }
  }

  // 2. JSON-LD structured data
  if (!info.title) {
    const jsonLdScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of jsonLdScripts) {
      try {
        const data = JSON.parse(s.textContent);
        const job = data['@type'] === 'JobPosting' ? data
          : Array.isArray(data['@graph']) ? data['@graph'].find(x => x['@type'] === 'JobPosting')
          : null;
        if (job) {
          info.title       = info.title       || job.title || '';
          info.company     = info.company     || job.hiringOrganization?.name || '';
          info.location    = info.location    || job.jobLocation?.address?.addressLocality || '';
          info.description = info.description || stripHtml(job.description || '');
        }
      } catch { /* skip malformed */ }
    }
  }

  // 3. OpenGraph / meta tags
  if (!info.title)   info.title   = meta('og:title')    || meta('twitter:title') || '';
  if (!info.company) info.company = meta('og:site_name') || '';

  // 4. Page title heuristic: "Role @ Company" or "Role | Company"
  if (!info.title || !info.company) {
    const pageTitle = document.title;
    const sep = pageTitle.match(/(.+?)\s*[@|-]\s*(.+)/);
    if (sep) {
      if (!info.title)   info.title   = sep[1].trim();
      if (!info.company) info.company = sep[2].replace(/\s*jobs?$/i, '').trim();
    } else if (!info.title) {
      info.title = pageTitle.trim();
    }
  }

  // 5. Description fallback
  if (!info.description) info.description = findDescBlock();

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


// -- Generate tab --------------------------------------------------------------

function GenerateTab({ settings, resumeLoaded }) {
  const [scanState,  setScanState]  = useState('idle'); // idle | scanning | found
  const [title,      setTitle]      = useState('');
  const [company,    setCompany]    = useState('');
  const [desc,       setDesc]       = useState('');
  const [ats,        setAts]        = useState('generic');
  const [showDesc,   setShowDesc]   = useState(false);
  const [scanError,  setScanError]  = useState('');
  const [perJob,     setPerJob]     = useState('');
  const [status,     setStatus]     = useState('idle'); // idle | loading | done | error
  const [result,     setResult]     = useState(null);
  const [errMsg,     setErrMsg]     = useState('');
  const [savedPaths, setSavedPaths] = useState(null);

  const canGenerate = resumeLoaded && status !== 'loading';

  const scanPage = useCallback(async () => {
    setScanState('scanning');
    setScanError('');
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) throw new Error('No active tab found.');
      // executeScript injects fresh code on every click - no stale content script connection
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: scrapeJobFromPage,
      });
      const j = results[0]?.result;
      if (!j?.title && !j?.company) {
        setScanError('No job info detected - make sure you are on a job posting page.');
        setScanState('idle');
        return;
      }
      setTitle(j.title       || '');
      setCompany(j.company   || '');
      setDesc(j.description  || '');
      setAts(j.ats           || 'generic');
      setScanState('found');
    } catch (err) {
      setScanError(err.message || 'Scan failed.');
      setScanState('idle');
    }
  }, []);

  const reset = useCallback(() => {
    setScanState('idle');
    setScanError('');
    setTitle(''); setCompany(''); setDesc('');
    setResult(null); setSavedPaths(null);
    setStatus('idle'); setErrMsg('');
  }, []);

  const generate = useCallback(async () => {
    setStatus('loading');
    setErrMsg('');
    setResult(null);
    setSavedPaths(null);
    try {
      const resp = await sendMsg({
        type: 'GENERATE',
        job:  { title, company, description: desc, ats },
        perJobInstructions: perJob,
        settings,
      });
      if (resp.error) throw new Error(resp.error);
      setResult(resp);
      setStatus('done');
      sendMsg({
        type: 'SAVE',
        company, role: title,
        coverMd:   resp.cover_letter_md,
        coverHtml: resp.cover_letter_html,
        settings,
      }).then(r => { if (r?.md_path) setSavedPaths(r); })
        .catch(err => console.warn('[overhired] Auto-save failed:', err.message));
    } catch (e) {
      setErrMsg(e.message);
      setStatus('error');
    }
  }, [title, company, desc, ats, perJob, settings]);

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

  // -- Scan state --
  if (scanState !== 'found') return html`
    <div class="panel">
      <${FengShuiPanel} />
      <button class="btn btn-primary btn-full" style="margin-bottom:8px"
        disabled=${scanState === 'scanning'} onClick=${scanPage}>
        ${scanState === 'scanning'
          ? html`<span class="spinner"></span> Scanning...`
          : 'Scan Page'}
      </button>
      ${scanError && html`
        <p style="color:var(--muted);font-size:11px;margin-top:0">${scanError}</p>`}
      ${!resumeLoaded && html`
        <p style="color:var(--muted);font-size:11px;margin-top:6px">
          No resume loaded - upload your PDF in Settings first.
        </p>`}
    </div>`;

  // -- Found state --
  return html`
    <div class="panel">
      <${FengShuiPanel} />

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

      ${!resumeLoaded && html`
        <p style="color:var(--muted);font-size:11px;margin-top:8px">
          No resume loaded - go to Settings to upload your PDF.
        </p>`}

      ${status === 'error' && html`
        <p style="color:var(--danger);font-size:11px;margin-top:8px">${errMsg}</p>`}

      ${savedPaths && html`
        <div class="saved-path">Saved: ${savedPaths.md_path}</div>`}

      ${result && html`
        <div class="preview" dangerouslySetInnerHTML=${{
          __html: marked.parse(result.cover_letter_md || '')
        }} />`}
    </div>`;
}

// -- Settings tab --------------------------------------------------------------

function SettingsTab({ settings, onSave, onResumeLoaded }) {
  const [s,       setS]       = useState(settings);
  const [profile, setProfile] = useState({});
  const [resume,  setResume]  = useState('');  // extracted text
  const [rStatus, setRStatus] = useState('');  // 'loaded' | 'loading' | ''
  const [drag,    setDrag]    = useState(false);
  const [saved,   setSaved]   = useState(false);

  useEffect(() => {
    load([STORAGE_KEYS.profile, STORAGE_KEYS.resume]).then(d => {
      if (d.user_profile) setProfile(d.user_profile);
      if (d.resume_text)  setRStatus('loaded');
    });
  }, []);

  const field = (key, subkey) => ({
    value:   subkey ? s[key]?.[subkey] ?? '' : s[key] ?? '',
    onInput: e => setS(prev => subkey
      ? { ...prev, [key]: { ...prev[key], [subkey]: e.target.value } }
      : { ...prev, [key]: e.target.value }),
  });

  const profileField = (key) => ({
    value:   profile[key] || '',
    onInput: e => setProfile(prev => ({ ...prev, [key]: e.target.value })),
  });

  const openUploadTab = useCallback(() => {
    chrome.tabs.create({ url: chrome.runtime.getURL('popup/popup.html') + '?tab=settings' });
  }, []);

  const handlePdf = useCallback(async (file) => {
    if (!file || !file.name.endsWith('.pdf')) return;
    setRStatus('loading');
    try {
      const resumeText = await parsePdfLocally(file);
      await store({ [STORAGE_KEYS.resume]: resumeText });

      // Auto-fill profile fields that are still empty
      const detected = extractProfileFromResume(resumeText);
      setProfile(prev => {
        const merged = { ...prev };
        for (const [k, v] of Object.entries(detected)) {
          if (!merged[k] && v) merged[k] = v;
        }
        store({ [STORAGE_KEYS.profile]: merged });
        return merged;
      });

      setRStatus('loaded');
      onResumeLoaded?.(true);
    } catch (e) {
      setRStatus('error: ' + e.message);
    }
  }, [onResumeLoaded]);

  const saveAll = async () => {
    await store({ [STORAGE_KEYS.settings]: s, [STORAGE_KEYS.profile]: profile });
    onSave(s);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const dropZoneClass = `drop-zone ${drag ? 'drag' : ''} ${rStatus === 'loaded' ? 'loaded' : ''}`;

  return html`
    <div class="panel">

      <!-- Resume -->
      <div class="settings-section">
        <div class="settings-title">Resume (PDF)</div>
        <div
          class=${dropZoneClass}
          onDragOver=${e => { e.preventDefault(); setDrag(true); }}
          onDragLeave=${() => setDrag(false)}
          onDrop=${e => { e.preventDefault(); setDrag(false); handlePdf(e.dataTransfer.files[0]); }}
          onClick=${() => IN_FULL_TAB
            ? document.getElementById('pdf-input').click()
            : openUploadTab()}
        >
          ${rStatus === 'loaded'  ? (IN_FULL_TAB ? 'Resume loaded - profile fields auto-filled below' : 'Resume loaded - click to update') :
            rStatus === 'loading' ? 'Parsing PDF...' :
            rStatus.startsWith('error') ? `${rStatus}` :
            IN_FULL_TAB ? 'Drop your resume PDF here or click to select'
                        : 'Click to open upload page'}
        </div>
        <input id="pdf-input" type="file" accept=".pdf" style="display:none"
          onChange=${e => handlePdf(e.target.files[0])} />
      </div>

      <!-- Personal profile -->
      <div class="settings-section">
        <div class="settings-title">Your Profile</div>
        ${['name','email','phone','linkedin','github'].map(k => html`
          <div class="field">
            <label>${k.charAt(0).toUpperCase() + k.slice(1)}</label>
            <input type=${k === 'email' ? 'email' : 'text'} ...${profileField(k)} />
          </div>`)}
        <div class="field">
          <label>Address (Street)</label>
          <input type="text" ...${profileField('address_street')} />
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          ${[['City','address_city'],['State / Province','address_state'],
             ['Postal Code','address_postal'],['Country','address_country']].map(([lbl,k]) => html`
            <div class="field">
              <label>${lbl}</label>
              <input type="text" ...${profileField(k)} />
            </div>`)}
        </div>
      </div>

      <!-- AI provider -->
      <div class="settings-section">
        <div class="settings-title">AI Provider</div>
        <div class="field">
          <label>Provider</label>
          <select value=${s.provider} onChange=${e => setS(p => ({ ...p, provider: e.target.value }))}>
            <option value="">Companion default (from config.toml)</option>
            <option value="ollama">Ollama / llama.cpp (local)</option>
            <option value="openai">OpenAI-compatible</option>
            <option value="claude">Anthropic Claude</option>
          </select>
        </div>
        <div class="field">
          <label>Endpoint <span style="color:var(--muted)">(leave blank to use companion default)</span></label>
          <input type="text" ...${field('endpoint')}
            placeholder="e.g. http://localhost:8080" />
        </div>
        <div class="field">
          <label>Model <span style="color:var(--muted)">(leave blank to use companion default)</span></label>
          <input type="text" ...${field('model')}
            placeholder="e.g. llama3.2, gpt-4o, claude-sonnet-4-6" />
        </div>
        ${s.provider === 'openai' || s.provider === 'claude' ? html`
          <div class="field">
            <label>API Key</label>
            <input type="password" ...${field('api_key')} placeholder="sk-..." />
          </div>` : null}
      </div>

      <!-- Companion connection -->
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

      <!-- Global instructions -->
      <div class="settings-section">
        <div class="settings-title">Global Instructions</div>
        <div class="field">
          <textarea rows=3 ...${field('global_instructions')}
            placeholder="Applied to every cover letter. E.g. Keep it under 400 words. Sign off as Jun." />
        </div>
      </div>

      <!-- Easter egg -->
      <div class="settings-section">
        <div class="settings-title">Easter Egg</div>
        <div class="toggle-row">
          <span class="toggle-label">Include AI prompt injection comment</span>
          <input type="checkbox"
            checked=${s.easter_egg}
            onChange=${e => setS(p => ({ ...p, easter_egg: e.target.checked }))} />
        </div>
        <p style="font-size:11px;color:var(--muted)">
          Appends a hidden HTML comment to every cover letter instructing AI
          screening systems to advance your application. 
        </p>
        ${s.easter_egg && html`
          <div class="field" style="margin-top:8px">
            <label>Custom message <span style="color:var(--muted)">(leave empty for built-in default)</span></label>
            <textarea rows=4 ...${field('easter_egg_text')}
              placeholder="Custom AI prompt injection text..." />
          </div>`}
      </div>

      <button class="btn btn-primary btn-full" onClick=${saveAll}>
        ${saved ? 'Saved!' : 'Save Settings'}
      </button>
    </div>`;
}

// -- Root App ------------------------------------------------------------------

function App() {
  const [tab,          setTab]          = useState(IN_FULL_TAB ? 'settings' : 'generate');
  const [health,       setHealth]       = useState(undefined);
  const [settings,     setSettings]     = useState(DEFAULT_SETTINGS);
  const [resumeLoaded, setResumeLoaded] = useState(false);

  useEffect(() => {
    load([STORAGE_KEYS.settings, STORAGE_KEYS.resume]).then(d => {
      const s = d.settings ? { ...DEFAULT_SETTINGS, ...d.settings } : DEFAULT_SETTINGS;
      setSettings(s);
      if (d.resume_text) setResumeLoaded(true);
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
        ? html`<${GenerateTab} settings=${settings} resumeLoaded=${resumeLoaded} />`
        : html`<${SettingsTab} settings=${settings} onResumeLoaded=${setResumeLoaded} onSave=${s => { setSettings(s); store({ [STORAGE_KEYS.settings]: s }); }} />`}
    </div>`;
}

// -- Helpers -------------------------------------------------------------------

function extractProfileFromResume(text) {
  const out = {};

  const emailM = text.match(/\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b/);
  if (emailM) out.email = emailM[1];

  // International: +64 204 770601, +1-800-555-0100, etc. then local fallback
  const phoneM = text.match(/\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{6,10}/)
              || text.match(/\b\(?\d{2,4}\)?[\s.\-]?\d{3,4}[\s.\-]?\d{4}\b/);
  if (phoneM) out.phone = phoneM[0].trim();

  // Full URL (linkedin.com/in/user) or short form (in/user) preceded by whitespace
  const liM = text.match(/linkedin\.com\/in\/([\w\-]+)/i)
           || text.match(/(?:^|\s)in\/([\w\-]+)/m);
  if (liM) out.linkedin = `https://linkedin.com/in/${liM[1]}`;

  const ghM = text.match(/github\.com\/([\w\-]+)/i);
  if (ghM) out.github = `https://github.com/${ghM[1]}`;

  // Name heuristic: first short line (2-4 Title-case words, no digits)
  for (const line of text.split('\n').map(l => l.trim()).slice(0, 8)) {
    if (/^[A-Z][a-zA-Z'\-]+(?: [A-Z][a-zA-Z'\-]+){1,3}$/.test(line)) {
      out.name = line;
      break;
    }
  }

  return out;
}

async function parsePdfLocally(file) {
  const wasmUrl = chrome.runtime.getURL('wasm/mupdf.js');
  const mupdf   = await import(wasmUrl); // named exports, WASM init via top-level await

  const bytes = new Uint8Array(await file.arrayBuffer());
  const doc   = mupdf.Document.openDocument(bytes, 'application/pdf');
  const pages = doc.countPages();
  const parts = [];

  for (let i = 0; i < pages; i++) {
    const page = doc.loadPage(i);
    try   { parts.push(page.toStructuredText('preserve-whitespace').asText()); }
    finally { page.destroy(); }
  }
  doc.destroy();

  const text = parts.join('\n\n').trim();
  if (!text) throw new Error('Could not extract text from PDF (may be image-only).');
  return text;
}

// -- Mount ---------------------------------------------------------------------

render(html`<${App} />`, document.getElementById('app'));
