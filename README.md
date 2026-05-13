# overhired

> The luxury problem of too many offers.

**overhired** is a browser extension + local companion service that reads any job posting, generates a personalized cover letter using your local AI, and produces a full analysis package — cover letter, job fit score, company summary, and jargon decoder — saved to disk automatically.

---

## Features

- **One-click grab** — scrapes any job page and extracts title, company, description via LLM
- **Adaptive parsers** — companion writes and caches a Python parser per site; instant on repeat visits
- **Self-healing** — stale or broken parsers are auto-detected and regenerated
- **AI cover letter** — personalized to your resume and the specific role
- **Post-save analysis** — `insight.md` (jargon decoder), `score.md` (fit score), `summary.md` (company profile)
- **Local-first AI** — defaults to [Ollama](https://ollama.com); OpenAI and Claude supported
- **Privacy by design** — resume stays on disk; only job text reaches the AI
- **Feng shui panel** — daily lucky day + best interview dates 🈴

---

## Quick Start

### 1 — Configure `~/.overhired/config.toml`

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
cd overhired/companion
pip install -r requirements.txt
python main.py
# optional flags:
#   --port 7878
#   --log-level debug
#   --reload          (dev mode, auto-restart on changes)
```

### 3 — Load the extension

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `extension/` folder
4. Pin the overhired icon to your toolbar

---

## How It Works

1. Navigate to any job posting
2. Click the overhired icon → **Grab Page**
3. Companion extracts job info (instant if site seen before, ~30s first time)
4. Click **Generate Cover Letter** — companion reads your resume from disk and calls the AI
5. Cover letter is saved; analysis files are written in the background

---

## Architecture

```mermaid
graph TB
    subgraph Browser["Browser (Chrome)"]
        EXT["Extension Popup\n(Preact UI)"]
        SW["Service Worker"]
    end

    subgraph Companion["Companion (FastAPI :7878)"]
        API["main.py\n/extract /generate /save"]
        EXT_MOD["extractor.py\nOrchestrator"]
        CACHE["Parser Cache\n~/.overhired/parsers/*.py"]
        AI_CLI["ai_client.py\nAIClient"]
        ANA["analyzer.py\ninsight / score / summary"]
    end

    subgraph LLM["LLM (Ollama :11434)"]
        MODEL["qwen3:8b"]
    end

    subgraph Disk["Disk Output"]
        OUT["~/Documents/job-applications/\nCompany/Role/\n  cover_letter.md\n  insight.md\n  score.md\n  summary.md"]
        RESUME["~/Downloads/js_2026.pdf"]
        CONFIG["~/.overhired/config.toml"]
    end

    EXT -->|"EXTRACT / GENERATE\n/ SAVE / POLL_FILES"| SW
    SW -->|"POST /extract"| API
    SW -->|"POST /generate"| API
    SW -->|"POST /save"| API
    SW -->|"GET /jobs/.../files"| API
    API --> EXT_MOD
    EXT_MOD --> CACHE
    EXT_MOD --> AI_CLI
    AI_CLI --> MODEL
    API --> ANA
    ANA --> AI_CLI
    API --> OUT
    API -.->|reads| RESUME
    API -.->|reads| CONFIG
```

---

## Sequence: First visit (no parser)

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
    Popup->>Popup: scrapeJobFromPage()\n→ {domain, page_text}
    Popup->>SW: EXTRACT {domain, page_text}
    SW->>API: POST /extract
    API->>Ext: extract(domain, page_text)
    Ext->>Cache: seek.co.nz.py exists?
    Cache-->>Ext: ❌ miss

    Ext->>LLM: generate_with_tools(system, user, tools)
    Note over LLM: Writes extract() function

    loop up to 10 iterations
        LLM->>Ext: run_parser(code, text)
        Ext->>Ext: exec code in sandbox
        Ext-->>LLM: {title, company, ...}
        alt title invalid / empty
            LLM->>LLM: revise code
        else title looks good
            LLM->>Ext: save_parser(domain, code)
            Ext->>Cache: write seek.co.nz.py
        end
    end

    Ext->>Cache: run saved parser to confirm
    Cache-->>Ext: {title, company, description, location}
    Ext-->>API: result
    API-->>SW: {title, company, ...}
    SW-->>Popup: job info
    Popup->>User: "Senior Engineer @ Acme"
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
    Popup->>Popup: scrapeJobFromPage()\n→ {domain, page_text}
    Popup->>SW: EXTRACT {domain, page_text}
    SW->>API: POST /extract
    API->>Ext: extract(domain, page_text)
    Ext->>Cache: seek.co.nz.py exists?
    Cache-->>Ext: ✅ hit

    Ext->>Ext: exec parser in sandbox
    alt title passes sanity check
        Ext-->>API: {title, company, description, location}
        API-->>SW: result
        SW-->>Popup: job info
        Popup->>User: "Senior Engineer @ Acme" ⚡ instant
    else title suspicious / crash (self-healing)
        Ext->>Cache: delete seek.co.nz.py
        Note over Ext: falls through to\nagentic loop\n→ regenerates parser
    end
```

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

---

## Requirements

- Python 3.10+
- Chrome 109+
- Ollama (recommended) or an OpenAI/Claude API key

---

## License

MIT — see [LICENSE](LICENSE)

