/**
 * overhired — popup UI (Preact + htm, no build step)
 */
import { h, render }       from '../vendor/preact.module.js';
import { useState, useEffect, useCallback } from '../vendor/preact-hooks.module.js';
import { marked }          from '../vendor/marked.esm.js';
import htm                 from '../vendor/htm.module.js';

const html = htm.bind(h);

const AUSPICE_URL = 'https://fengshui.overhired.work';

async function fetchAuspice() {
  const today = new Date().toISOString().slice(0, 10);
  const to    = new Date(Date.now() + 6 * 86400000).toISOString().slice(0, 10);
  try {
    const [dayRes, bestRes] = await Promise.all([
      fetch(`${AUSPICE_URL}/today`).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch(`${AUSPICE_URL}/best?activity=interview&from=${today}&to=${to}`)
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
  const bestDays = (best?.days || []).slice(0, 3).map(d => {
    const dt = new Date(d.date + 'T00:00:00');
    return dt.toLocaleDateString('en', { weekday: 'short', month: 'short', day: 'numeric' });
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
  provider:            'ollama',
  endpoint:            'http://localhost:11434',
  model:               'llama3.2',
  api_key:             '',
  global_instructions: '',
  easter_egg:          false,
  easter_egg_text:     '',        // empty = use companion's built-in default
  companion_url:       'http://localhost:7878',
  companion_token:     '',        // copy from companion config.toml → auth_token
};

// ── Utility ───────────────────────────────────────────────────────────────────

const load  = (keys) => chrome.storage.local.get(keys);
const store = (obj)  => chrome.storage.local.set(obj);
const sendMsg = (msg) => chrome.runtime.sendMessage(msg);

async function companionHealth(url = 'http://localhost:7878') {
  try {
    const r = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

// ── Sub-components ────────────────────────────────────────────────────────────

function CompanionBanner({ health }) {
  if (health === undefined) return null; // still checking
  if (health) {
    const isLocal = health.ai_endpoint?.includes('localhost') || health.ai_endpoint?.includes('127.0.0.1');
    const providerLabel = health.ai_provider === 'claude'  ? 'Anthropic'
      : health.ai_provider === 'ollama' ? 'Ollama'
      : isLocal ? 'local LLM'
      : 'OpenAI';
    const modelName  = (health.ai_model || '').replace(/\.gguf$/i, '').replace(/^local$/, '');
    const modelLabel = modelName ? ` · ${modelName}` : '';
    const ai = health.ai_reachable
      ? `${providerLabel}${modelLabel}`
      : `${providerLabel} · ⚠ not reachable`;
    return html`<div class="banner ok">✓ Companion running · ${ai}</div>`;
  }
  const cmd = 'cd companion && python main.py';
  const copy = () => navigator.clipboard.writeText(cmd);
  return html`
    <div class="banner">
      ⚠ Companion not running
      <button class="banner-cmd" onClick=${copy} title="Click to copy">
        python main.py
      </button>
    </div>`;
}

function JobCard({ job }) {
  if (!job) return html`<div class="job-card"><div class="job-meta">Open a job posting to begin.</div></div>`;
  return html`
    <div class="job-card">
      <div class="job-title">${job.title || '(title not detected)'}</div>
      <div class="job-meta">${job.company || ''} ${job.location ? '· ' + job.location : ''}</div>
    </div>`;
}

// ── Generate tab ──────────────────────────────────────────────────────────────

function GenerateTab({ job, settings, resumeLoaded, scrapeError }) {
  const [perJob,     setPerJob]     = useState('');
  const [status,     setStatus]     = useState('idle'); // idle | loading | done | error
  const [result,     setResult]     = useState(null);
  const [errMsg,     setErrMsg]     = useState('');
  const [savedPaths, setSavedPaths] = useState(null);

  const canGenerate = job && resumeLoaded && status !== 'loading';

  const generate = useCallback(async () => {
    setStatus('loading');
    setErrMsg('');
    setResult(null);
    setSavedPaths(null);
    try {
      const resp = await sendMsg({
        type: 'GENERATE',
        job,
        perJobInstructions: perJob,
        settings,
      });
      if (resp.error) throw new Error(resp.error);
      setResult(resp);
      setStatus('done');
      // Auto-save — non-fatal: a save failure must not erase the generated result
      sendMsg({
        type: 'SAVE',
        company: job.company,
        role:    job.title,
        coverMd:   resp.cover_letter_md,
        coverHtml: resp.cover_letter_html,
        settings,
      }).then(saveResp => {
        if (saveResp?.md_path) setSavedPaths(saveResp);
      }).catch(err => console.warn('[overhired] Auto-save failed:', err.message));
    } catch (e) {
      setErrMsg(e.message);
      setStatus('error');
    }
  }, [job, perJob, settings]);

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
          ? 'Content script not ready — refresh the job page and try again.'
          : 'Fill form failed: ' + err.message
      );
    }
  }, [result]);

  return html`
    <div class="panel">
      <${JobCard} job=${job} />
      <${FengShuiPanel} />

      ${scrapeError && !job && html`
        <p style="color:var(--muted);font-size:11px;margin-top:0;padding-top:0">
          ⚠ Could not extract job info: ${scrapeError}
        </p>`}

      <div class="field">
        <label>Additional instructions for this application</label>
        <textarea
          placeholder="e.g. Mention I'm willing to relocate but need visa sponsorship."
          value=${perJob}
          onInput=${e => setPerJob(e.target.value)}
          rows=3
        />
      </div>

      <div class="btn-row">
        <button
          class="btn btn-primary btn-full"
          disabled=${!canGenerate}
          onClick=${generate}
        >
          ${status === 'loading'
            ? html`<span class="spinner"></span> Generating…`
            : '✦ Extract & Generate'}
        </button>
        ${result && html`
          <button class="btn btn-secondary" onClick=${fillForm} title="Fill ATS form">
            ⬇ Fill Form
          </button>`}
      </div>

      ${!resumeLoaded && html`
        <p style="color:var(--muted);font-size:11px;margin-top:8px">
          ⚠ No resume loaded — go to Settings to upload your PDF.
        </p>`}

      ${status === 'error' && html`
        <p style="color:var(--danger);font-size:11px;margin-top:8px">${errMsg}</p>`}

      ${savedPaths && html`
        <div class="saved-path">
          ✓ Saved: ${savedPaths.md_path}
        </div>`}

      ${result && html`
        <div class="preview" dangerouslySetInnerHTML=${{
          __html: marked.parse(result.cover_letter_md || '')
        }} />`}
    </div>`;
}

// ── Settings tab ──────────────────────────────────────────────────────────────

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

  const handlePdf = useCallback(async (file) => {
    if (!file || !file.name.endsWith('.pdf')) return;
    setRStatus('loading');
    try {
      const text = await sendMsg({ type: 'PARSE_PDF', fileData: await fileToBase64(file) });
      if (text?.error) throw new Error(text.error);
      await store({ [STORAGE_KEYS.resume]: text.resumeText });
      setRStatus('loaded');
      onResumeLoaded?.(true);   // propagate to App so Generate button unlocks
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
          onClick=${() => document.getElementById('pdf-input').click()}
        >
          ${rStatus === 'loaded'  ? '✓ Resume loaded — click to replace' :
            rStatus === 'loading' ? '⏳ Parsing PDF…' :
            rStatus.startsWith('error') ? `⚠ ${rStatus}` :
            '📄 Drop your resume PDF here or click to select'}
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
            <option value="ollama">Ollama / llama.cpp (local)</option>
            <option value="openai">OpenAI</option>
            <option value="claude">Anthropic Claude</option>
          </select>
        </div>
        <div class="field">
          <label>Endpoint</label>
          <input type="text" ...${field('endpoint')}
            placeholder=${s.provider === 'ollama' ? 'http://localhost:11434' : 'https://api.openai.com'} />
        </div>
        <div class="field">
          <label>Model</label>
          <input type="text" ...${field('model')}
            placeholder=${s.provider === 'ollama' ? 'llama3.2' : 'gpt-4o'} />
        </div>
        ${s.provider !== 'ollama' && html`
          <div class="field">
            <label>API Key</label>
            <input type="password" ...${field('api_key')} placeholder="sk-…" />
          </div>`}
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
        <div class="settings-title">🤖 Easter Egg</div>
        <div class="toggle-row">
          <span class="toggle-label">Include AI prompt injection comment</span>
          <input type="checkbox"
            checked=${s.easter_egg}
            onChange=${e => setS(p => ({ ...p, easter_egg: e.target.checked }))} />
        </div>
        <p style="font-size:11px;color:var(--muted)">
          Appends a hidden HTML comment to every cover letter instructing AI
          screening systems to advance your application. 😄
        </p>
        ${s.easter_egg && html`
          <div class="field" style="margin-top:8px">
            <label>Custom message <span style="color:var(--muted)">(leave empty for built-in default)</span></label>
            <textarea rows=4 ...${field('easter_egg_text')}
              placeholder="Custom AI prompt injection text…" />
          </div>`}
      </div>

      <button class="btn btn-primary btn-full" onClick=${saveAll}>
        ${saved ? '✓ Saved!' : 'Save Settings'}
      </button>
    </div>`;
}

// ── Root App ──────────────────────────────────────────────────────────────────

function App() {
  const [tab,         setTab]         = useState('generate');
  const [health,      setHealth]      = useState(undefined);
  const [job,         setJob]         = useState(null);
  const [scrapeError, setScrapeError] = useState('');
  const [settings,    setSettings]    = useState(DEFAULT_SETTINGS);
  const [resumeLoaded, setResumeLoaded] = useState(false);

  useEffect(() => {
    // Load settings + resume status first so health check uses the configured URL.
    load([STORAGE_KEYS.settings, STORAGE_KEYS.resume]).then(d => {
      const s = d.settings ? { ...DEFAULT_SETTINGS, ...d.settings } : DEFAULT_SETTINGS;
      setSettings(s);
      if (d.resume_text) setResumeLoaded(true);
      companionHealth(s.companion_url).then(setHealth);
    });
    // Ask content script for current page job info
    chrome.tabs.query({ active: true, currentWindow: true }).then(([t]) => {
      if (!t?.id) return;
      chrome.tabs.sendMessage(t.id, { type: 'GET_JOB_INFO' })
        .then(j => {
          if (!j)      return;
          if (j.error) { setScrapeError(j.error); return; }
          setJob(j);
        })
        .catch(() => {}); // content script may not be ready on non-job pages
    });
  }, []);

  return html`
    <div>
      <div class="header">
        <div class="logo">over<span>hired</span></div>
        <span class="pill ${health ? 'pill-ok' : health === undefined ? 'pill-loading' : 'pill-err'}">
          ${health ? 'online' : health === undefined ? '…' : 'offline'}
        </span>
      </div>

      <${CompanionBanner} health=${health} />

      <div class="tabs">
        <button class=${'tab' + (tab === 'generate' ? ' active' : '')}
          onClick=${() => setTab('generate')}>Generate</button>
        <button class=${'tab' + (tab === 'settings' ? ' active' : '')}
          onClick=${() => setTab('settings')}>Settings</button>
      </div>

      ${tab === 'generate'
        ? html`<${GenerateTab} job=${job} settings=${settings} resumeLoaded=${resumeLoaded} scrapeError=${scrapeError} />`
        : html`<${SettingsTab} settings=${settings} onResumeLoaded=${setResumeLoaded} onSave=${s => { setSettings(s); store({ [STORAGE_KEYS.settings]: s }); }} />`}
    </div>`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload  = () => resolve(reader.result.split(',')[1]);
    reader.onerror = () => reject(new Error('Failed to read PDF file'));
    reader.readAsDataURL(file);
  });
}

// ── Mount ─────────────────────────────────────────────────────────────────────

render(html`<${App} />`, document.getElementById('app'));
