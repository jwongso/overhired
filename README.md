# grapply

> Grab and Apply

**grapply** is a browser extension + local companion service that reads any job posting, generates a personalized cover letter using your local LLM or online AI provider (Anthropic or OpenAI), and produces a full analysis package — cover letter, job fit score, company summary, and jargon decoder — saved to disk automatically.

<table><tr>
<td><img src="stats.png" height="540" alt="grapply stats dashboard"></td>
<td><img src="extension.png" height="540" alt="grapply extension screenshot"></td>
</tr></table>

---

## Features

- **One-click grab** — scrapes any job page; extraction is instant for known sites (SEEK, Indeed, LinkedIn) via structured programmatic strategies, zero LLM
- **Quality-first parsers** — 3-phase pipeline: extract → generate → validate+fix; parsers are cached and re-used; <1ms on repeat visits
- **Pinned strategies** — SEEK, Indeed and LinkedIn always use their native structured data (inline JSON / JSON-LD / title tag), not fragile CSS selectors
- **Self-healing** — stale or broken parsers are auto-detected and regenerated
- **AI cover letter** — personalized to your resume and the specific role
- **Post-save analysis** — `insight.md` (jargon decoder), `score.md` (fit score), `summary.md` (company profile)
- **ATS form filler** — detects application forms, generates field-mapping ops once per site, then fills instantly
- **Local-first AI** — defaults to [Ollama](https://ollama.com); OpenAI and Claude supported
- **Privacy by design** — resume stays on disk; only job text reaches the AI
- **Feng shui panel** — daily lucky day + best interview dates 🈴

---

## Quick Start

### 1 — Configure `~/.grapply/config.toml`

```toml
output_dir     = "~/Documents/job-applications"
companion_port = 7878
auth_token     = ""

[ai]
provider = "ollama"
endpoint = "http://localhost:11434"
model    = "qwen3:8b"
timeout      = 180
tool_timeout = 600

[resume]
path = "~/Documents/my-resume.pdf"   # PDF, MD or TXT
```

### 2 — Start companion

```bash
cd grapply/companion
pip install -r requirements.txt
./start.sh          # kills nothing if already running; starts fresh otherwise
# or manually:
#   python -m uvicorn main:app --host 127.0.0.1 --port 7878
```

### 3 — Load the extension

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `extension/` folder
4. Pin the grapply icon to your toolbar

---

## How It Works

1. Navigate to any job posting
2. Click the grapply icon → **Grab Page**
3. Companion extracts job info (instant for SEEK / Indeed / LinkedIn; ~3–10 min LLM call on first visit to an unknown site)
4. Click **Generate Cover Letter** — companion reads your resume from disk and calls the AI
5. Cover letter is saved; analysis files are written in the background

---

## Extraction Strategy

Extraction is tiered — LLM is only used when nothing else works:

| Tier | Trigger | Speed | LLM? |
|------|---------|-------|------|
| **1 — Cached parser** | Parser file exists for domain | < 1 ms | ❌ |
| **2 — Programmatic** | SEEK / Indeed / LinkedIn (pinned domains) | < 5 ms | ❌ |
| **3 — LLM Phase 1** | Unknown domain, first visit | ~3–10 min | ✅ once |

After Tier 2 or 3, a parser is saved in the background so the **next visit is always Tier 1**.

---

## Parser Generation (Background, Quality-First)

For **pinned domains** (SEEK, Indeed, LinkedIn), `clean_html` always uses the site's native structured extractor, producing a stable 4-line format (`title / company / location / description`). A deterministic positional template is saved immediately — no LLM needed.

For **unknown domains**, the 3-phase pipeline runs in the background after extraction returns to the user:

```
Phase 1 — Extract (LLM A)   focused JSON-only call → confirmed {title, company, location}
Phase 2 — Generate (LLM B)  writes Python parser using confirmed values as ground truth
Phase 3 — Validate+Fix      runs parser → compares → asks LLM to fix (up to 3 retries)
```

---

## Self-Healing Parsers

The parser cache validates every result before trusting it:

| Situation | What happens |
|-----------|-------------|
| Parser raises an exception | Logged ⚠️, deleted, pipeline regenerates |
| Parser returns empty title | Logged ⚠️, deleted, pipeline regenerates |
| Title too short (< 5 chars) | Logged ⚠️, deleted, pipeline regenerates |
| Title matches domain name (`"SEEK"`, `"LinkedIn"`) | Logged ⚠️, deleted, pipeline regenerates |
| Title too long or contains newlines | Logged ⚠️, deleted, pipeline regenerates |
| Title looks legitimate | ✅ `[cache] hit — title='Senior Engineer'` |

---

## Architecture

```mermaid
graph TB
    subgraph Browser["Browser (Chrome)"]
        EXT["Extension Popup\n(Preact UI)"]
        SW["Service Worker"]
    end

    subgraph Companion["Companion (FastAPI :7878)"]
        API["main.py\n/extract /generate /save /fill"]
        EXT_MOD["extractor.py\nOrchestrator"]

        subgraph Strategies["HTML Strategies"]
            PIN["Pinned\nseek · jsonld · linkedin"]
            GEN["Generic\ncontent_area · text_density · strip_all"]
        end

        subgraph ParserGen["Parser Generation (background)"]
            TMPL["Positional Template\n(pinned domains — no LLM)"]
            P2["Phase 2 — Generate\n(LLM B — unknown domains)"]
            P3["Phase 3 — Validate+Fix\n(LLM C×N)"]
        end

        CACHE["Parser Cache\n~/.grapply/parsers/*.py"]
        TRACKER["tracker.py\nStrategy catalog DB"]
        AI_CLI["ai_client.py\nAIClient"]
        ANA["analyzer.py\ninsight / score / summary"]
        ATS["ats_filler.py\nATS field-mapping ops"]
    end

    subgraph LLM["LLM (Ollama :11434)"]
        MODEL["qwen3:8b"]
    end

    subgraph Disk["Disk"]
        OUT["~/Documents/job-applications/\nCompany/Role/\n  cover_letter.md · insight.md\n  score.md · summary.md"]
        RESUME["~/Documents/resume.pdf"]
        CONFIG["~/.grapply/config.toml"]
    end

    EXT -->|"messages"| SW
    SW -->|"POST /extract"| API
    SW -->|"POST /generate"| API
    SW -->|"POST /save"| API
    SW -->|"POST /fill"| API
    API --> EXT_MOD
    EXT_MOD --> PIN
    EXT_MOD --> GEN
    GEN --> TRACKER
    EXT_MOD --> CACHE
    EXT_MOD -.->|"cache miss\n(background)"| TMPL
    EXT_MOD -.->|"cache miss\n(background)"| P2
    P2 --> P3
    TMPL --> CACHE
    P3 --> CACHE
    P2 --> AI_CLI
    P3 --> AI_CLI
    AI_CLI --> MODEL
    API --> ANA
    ANA --> AI_CLI
    API --> ATS
    ATS --> AI_CLI
    API --> OUT
    API -.->|reads| RESUME
    API -.->|reads| CONFIG
```

---

## Sequence: Pinned domain (SEEK / Indeed / LinkedIn)

```mermaid
sequenceDiagram
    actor User
    participant Popup
    participant SW as Service Worker
    participant API as Companion /extract
    participant Ext as extractor.py
    participant Strat as Pinned Strategy\n(seek / jsonld / linkedin)
    participant Cache as Parser Cache

    User->>Popup: Click "Grab Page"
    Popup->>SW: EXTRACT {domain, html}
    SW->>API: POST /extract
    API->>Ext: extract_and_wait(domain, html)
    Ext->>Strat: clean_html(html, domain) — pinned
    Strat-->>Ext: title / company / location / description
    Ext->>Cache: nz.seek.com.py exists?

    alt Parser cached (Tier 1)
        Cache-->>Ext: ✅ run parser → result
    else Not cached (Tier 2 — programmatic)
        Ext->>Strat: _programmatic_extract() → result
        Note over Ext,Cache: background thread saves\npositional template instantly\n(no LLM)
    end

    Ext-->>API: {title, company, ...} ⚡
    API-->>Popup: job info
    Popup->>User: "Senior Engineer @ TVNZ"
```

---

## Sequence: Unknown domain — first visit

```mermaid
sequenceDiagram
    actor User
    participant Popup
    participant SW as Service Worker
    participant API as Companion /extract
    participant Ext as extractor.py
    participant Cache as Parser Cache
    participant LLM as qwen3:8b

    User->>Popup: Click "Grab Page"
    Popup->>SW: EXTRACT {domain, html}
    SW->>API: POST /extract
    API->>Ext: extract(domain, html)
    Ext->>Ext: clean_html — benchmark all strategies
    Ext->>Cache: unknown.com.py exists?
    Cache-->>Ext: ❌ miss
    Ext->>Ext: _programmatic_extract() → ❌ no match

    Ext->>LLM: Phase 1 — extract JSON only
    Note over LLM: single focused call\nreturns {title, company, location}
    LLM-->>Ext: confirmed result
    Ext-->>API: result (user gets answer now ⚡)
    API-->>Popup: job info
    Popup->>User: "Senior Engineer @ Acme"

    Note over Ext,Cache: background thread continues...
    Ext->>LLM: Phase 2 — generate parser\n(knows confirmed values)
    LLM-->>Ext: Python extract() code
    loop up to 3 retries (Phase 3)
        Ext->>Ext: run_parser(code, page_text)
        alt title matches confirmed
            Ext->>Cache: save unknown.com.py ✅
        else wrong output
            Ext->>LLM: fix parser
            LLM-->>Ext: revised code
        end
    end
```

---

## Sequence: Repeat visit (parser cached)

```mermaid
sequenceDiagram
    actor User
    participant Popup
    participant SW as Service Worker
    participant API as Companion /extract
    participant Ext as extractor.py
    participant Cache as Parser Cache

    User->>Popup: Click "Grab Page"
    Popup->>SW: EXTRACT {domain, html}
    SW->>API: POST /extract
    API->>Ext: extract(domain, html)
    Ext->>Cache: unknown.com.py exists?
    Cache-->>Ext: ✅ hit

    Ext->>Ext: exec parser in sandbox
    alt title passes sanity check
        Ext-->>API: {title, company, ...} ⚡ < 1ms
        API-->>Popup: job info
        Popup->>User: "Senior Engineer @ Acme"
    else crash / bad title (self-healing)
        Ext->>Cache: delete unknown.com.py
        Note over Ext: falls through to\nLLM Phase 1 → regenerates
    end
```

---

## LLM Usage Summary

| Task | LLM calls | Frequency |
|------|-----------|-----------|
| Extraction — pinned domain (SEEK/Indeed/LinkedIn) | 0 | Never |
| Extraction — unknown domain, first visit | 1 (Phase 1) | Once per domain |
| Parser generation — pinned domain | 0 | Never |
| Parser generation — unknown domain | 2–5 (Phase 2 + Phase 3 retries) | Once per domain |
| Cover letter | 1 | Every application |
| Analysis (insight / score / summary) | 3 | Every application |
| ATS filler generation | 1–3 | Once per ATS domain |
| ATS filler use (cached) | 0 | Every subsequent fill |

---

## AI Providers

| Provider | Config | Notes |
|----------|--------|-------|
| **Ollama** (default) | `endpoint: http://localhost:11434` | Free, private, local |
| **llama.cpp** | `endpoint: http://localhost:8080` | OpenAI-compatible API |
| **OpenAI** | `provider: openai` + `api_key` | GPT-4o recommended |
| **Anthropic** | `provider: claude` + `api_key` | claude-3-5-sonnet recommended |

---

## Output Files

After each application, the companion writes to `~/Documents/job-applications/<Company>/<Role>/`:

| File | Contents |
|------|----------|
| `cover_letter.md` | Generated cover letter (Markdown) |
| `cover_letter.html` | Same, rendered as HTML |
| `insight.md` | Jargon decoder — red flags, culture signals |
| `score.md` | Job fit score against your resume |
| `summary.md` | Company profile fetched from their website |


Example of insight.md:

--- 
# Role Insights — Embedded Software Engineer at XXX

**Verdict:** Apply with caution ⚠️  **Generated:** 2026-05-16

> A stable, well-compensated role at a genuinely reputable medical device company, but you'll essentially be a solo embedded software owner inheriting underdocumented legacy code with heavy regulatory compliance obligations that the job listing quietly glosses over.

## 🚩 Red Flags

| Phrase | What it really means |
|--------|---------------------|
| Collaborating with original developers to build a deep understanding of existing embedded software architectures | The original developers may no longer be there, documentation is likely sparse or outdated, and you'll be inheriting legacy code that nobody fully understands anymore. |
| small, multidisciplinary team | You will be the only embedded software person, expected to make all software decisions alone while also interfacing with clinical, mechanical, and electrical people who don't speak your language. |
| extending and enhancing them with new capabilities and algorithms | Medical device software is heavily regulated (IEC 62304, FDA/TGA submissions); adding features to certified devices means significant documentation, validation, and regulatory burden on top of the actual engineering work. |
| clinical trial readiness | Your software will need to meet stringent regulatory standards with extensive traceability and testing documentation — this is not a typical embedded role and the compliance overhead is substantial. |

## ✅ Green Flags

| Phrase | What it signals |
|--------|----------------|
| Bi-annual Profit share | Profit sharing twice a year is genuinely above-average compensation structure and signals the company is financially healthy and willing to share gains with employees. |
| High rates of internal promotion | If true, this suggests genuine career progression without needing to job-hop, which is rare and valuable in engineering roles. |
| Annual Salary Review | Explicitly committing to annual reviews is a positive sign they won't let your salary stagnate quietly. |
| world leader in the design, manufacture, and marketing of medical devices, exporting to over 120 countries | Fisher & Paykel Healthcare is a legitimate, well-established company with real products — this is not a startup with vaporware; your work will actually reach patients. |
| Employee share purchase scheme | Ownership stake in a publicly traded, profitable medical device company is a tangible financial benefit worth real money over time. |

---
*Generated by grapply*

---

Example of score.md:

# Job Fit — Embedded Software Engineer at XXX

## Score: 7/10 — Apply with caveats ⚠️

**Experience gap:** minor  **Overqualified risk:** No  **Generated:** 2026-05-16

## ✅ Matching Skills
- Embedded software/firmware development (20 years experience)
- Embedded systems architecture and design
- Working with mature hardware platforms (Buildroot Linux, Oclea SDK)
- Cross-functional team collaboration
- Technical leadership and mentoring
- CI/CD pipeline development
- C/C++ embedded development (implied through Buildroot/control systems work)
- Sensor systems experience
- Prototyping and testing
- Communication and documentation skills
- New Zealand-based (Auckland)

## ❌ Missing Skills
- Medical device development experience
- Regulatory/clinical trial software development (IEC 62304 or similar)
- Algorithm development for embedded systems (explicitly stated)
- Experience analysing existing undocumented embedded architectures
- Safety-critical or healthcare-specific software standards
- Firmware-level hardware bring-up experience (not clearly evidenced)

## Honest Assessment
With 20 years of embedded systems experience including Buildroot Linux, control systems integration, and cross-functional technical leadership, this candidate brings substantial depth that directly aligns with the core role requirements. The work at Framecad on machine control systems and embedded-to-cloud interfaces is particularly transferable to extending existing embedded architectures. The main caveat is no explicit medical device or clinical/regulatory experience, but the candidate's strong engineering judgement, embedded maturity, and Auckland location make them a genuinely competitive applicant who should absolutely apply.

---
*Generated by grapply*

---

## Requirements

- Python 3.10+
- Chrome 109+
- Ollama or an OpenAI/Claude API key

---

## License

MIT — see [LICENSE](LICENSE)


