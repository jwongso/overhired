# overhired findings

I read `PLAN.md` and `README.md` first to understand the intended product: a browser extension plus a localhost companion service that extracts job data, generates cover letters with AI, and auto-fills ATS forms.

Below are the current issues and improvement suggestions, sorted by priority. I focused on problems that are real in the current codebase, not hypothetical style concerns.

---

## Critical

### 1. The localhost companion is exposed to arbitrary websites

**Why this matters**

This is the biggest issue in the project today. Any website the user visits can talk to the companion service on `localhost:7878`, because the companion explicitly enables wildcard CORS and exposes unauthenticated `POST /generate` and `POST /save`.

**Code references**

- `companion/main.py:43-48` — `allow_origins=["*"]`
- `companion/main.py:70-76` — `SaveRequest` accepts `output_dir`
- `companion/main.py:123-137` — `/save` writes files using the caller-supplied `output_dir`

**Verified behavior**

I verified this live:

1. `OPTIONS /save` with `Origin: https://example.com` returns `Access-Control-Allow-Origin: *`
2. `POST /save` with `output_dir: "/tmp/overhired-poc"` successfully wrote:
   - `/tmp/overhired-poc/Acme/Role/cover_letter.md`
   - `/tmp/overhired-poc/Acme/Role/cover_letter.html`

**Impact**

- Any website can use the companion as a local file-write primitive
- Any website can use `/generate` as an unauthenticated proxy to the user’s configured AI backend
- This weakens the privacy guarantees described in `README.md` and `PRIVACY.md`

**Suggested fix**

1. Remove wildcard CORS
2. Require a shared secret / token between the extension and companion
3. Remove `output_dir` from the public request model, or restrict all writes to a configured base directory
4. Consider rejecting requests that do not carry an extension-generated auth header

---

## High

### 2. The AI provider settings in the popup are mostly fake right now

**Why this matters**

The Settings UI lets the user choose provider, endpoint, model, and API key, but those values do not drive generation. The real AI configuration comes from the companion’s own `~/.overhired/config.toml`, loaded at startup.

**Code references**

- `extension/popup/popup.js:18-23` — popup stores provider/endpoint/model/api_key
- `extension/popup/popup.js:279-299` — UI exposes these fields
- `extension/service_worker.js:36-45` — only `global_instructions`, `per_job_instructions`, and `easter_egg` are sent to `/generate`
- `companion/main.py:37-38` — companion builds a global `AIClient` from config at boot

**Impact**

- Changing provider/model/API key in the popup does not affect actual generation
- Users can think they switched to OpenAI or Claude while the companion still points at Ollama
- API keys entered in the extension are currently unused for generation

**Suggested fix**

Pick one design and make it true:

1. **Extension-controlled AI config:** send provider/model/endpoint/api_key to the companion per request and instantiate `AIClient` from the request
2. **Companion-controlled AI config:** remove those fields from the extension UI and tell users to configure the companion via `config.toml`

Right now the UI and backend contract disagree.

### 3. Exported HTML is vulnerable to title/company HTML injection

**Why this matters**

The HTML export interpolates `company` and `role` directly into the `<title>` tag without escaping. A malicious job page can inject HTML or script into the saved export.

**Code references**

- `companion/main.py:191-216` — `_HTML_TEMPLATE`
- `companion/main.py:235-239` — `.format(company=company, role=role, body=full_body)`

**Verified behavior**

I verified this with a quick Python check:

- `company = 'ACME<script>alert(1)</script>'`
- `role = 'Engineer\"><script>alert(2)</script>'`

Both payloads appear verbatim in the generated HTML `<title>`.

**Impact**

- Opening a saved `cover_letter.html` can execute attacker-controlled markup originating from the job page metadata
- This is especially risky because the HTML output is explicitly meant to be opened locally

**Suggested fix**

1. Escape `company` and `role` with `html.escape()`
2. Consider sanitizing or restricting raw HTML in the rendered body as well, since model output is also untrusted

---

## Medium

### 4. Uploading a resume does not enable Generate until the popup is reopened

**Why this matters**

The popup’s top-level `resumeLoaded` state is only set during initial mount. Uploading a PDF in the Settings tab updates local state inside `SettingsTab`, but never updates the parent `App` state that controls the Generate button.

**Code references**

- `extension/popup/popup.js:339` — `const [resumeLoaded, setResumeLoaded] = useState(false)`
- `extension/popup/popup.js:345-347` — parent only initializes it from storage once
- `extension/popup/popup.js:208-218` — `handlePdf()` stores resume text and sets only local `rStatus`
- `extension/popup/popup.js:78` — Generate is gated by `job && resumeLoaded`

**Impact**

- User uploads a resume successfully
- UI still says “No resume loaded” in the Generate tab
- Generate remains disabled until the popup is closed and reopened

**Suggested fix**

Lift resume state to `App`, or pass an `onResumeLoaded()` callback from `App` to `SettingsTab`.

### 5. Job-scrape failures can be treated as a valid job and generate an “Unknown Role” letter

**Why this matters**

`extractor.js` now returns `{ error: ... }` when scraping fails, but the popup accepts any truthy response as a job object.

**Code references**

- `extension/content_scripts/extractor.js:189-194` — `GET_JOB_INFO` returns `{ error }` on failure
- `extension/popup/popup.js:352-354` — popup does `if (j) setJob(j)`
- `extension/popup/popup.js:78` — `canGenerate = job && resumeLoaded && ...`

**Impact**

- A scrape error can still enable Generate
- `job.title` / `job.company` are missing, so generation falls back to “Unknown Role” / “Unknown Company”
- The user gets no clear explanation that extraction failed

**Suggested fix**

1. Validate the response shape before calling `setJob`
2. Treat `{ error }` as an error state, not a job
3. Show a user-visible scrape failure message

### 6. Health checks report some broken AI endpoints as “reachable”

**Why this matters**

`AIClient.health_check()` currently returns `True` for any status code below 500. That means 401, 403, and 404 are treated as healthy.

**Code references**

- `companion/ai_client.py:61-74`
- `companion/ai_client.py:72` — `return r.status_code < 500`
- `extension/popup/popup.js:44-47` — popup uses `ai_reachable` to display provider health

**Impact**

- Missing or invalid OpenAI / Claude API keys can still show as “reachable”
- Wrong endpoints that return 404 can still show as “reachable”
- The banner becomes misleading exactly when the user needs it most

**Suggested fix**

Return `True` only for 2xx responses, or return a richer status object such as:

- `ok`
- `auth_error`
- `not_found`
- `timeout`

### 7. The companion claims to support custom `--port` / `--host`, but the extension cannot follow it

**Why this matters**

The companion can start on a custom host/port, but both the popup and the service worker hardcode `http://localhost:7878`.

**Code references**

- `companion/main.py:270-283` — CLI supports `--port` and `--host`
- `extension/popup/popup.js:11` — hardcoded companion URL
- `extension/service_worker.js:10` — hardcoded companion URL

**Impact**

- Running `python main.py --port 8787` makes the companion unreachable from the extension
- The CLI flexibility is effectively developer-only, not user-facing

**Suggested fix**

Add a configurable companion base URL in extension settings, or explicitly document that the extension requires `127.0.0.1:7878`.

### 8. The “user-editable” easter egg is not actually user-editable from the UI

**Why this matters**

The docs say the easter egg text is user-editable, and the backend supports `easter_egg_text`, but the extension only exposes a checkbox.

**Code references**

- `README.md:171` — “The message is user-editable.”
- `PLAN.md:263` — “The injection message is a user-editable template”
- `companion/main.py:62` and `:112` — backend supports `easter_egg_text`
- `extension/popup/popup.js:311-323` — UI only exposes a checkbox, no text input

**Impact**

- The feature is only half-wired
- The docs oversell what the current product can do

**Suggested fix**

Either add a textarea in Settings for the template, or remove the claim from the docs until the UI exists.

---

## Low

### 9. The docs are out of sync with the current implementation

**Why this matters**

The repo documentation no longer matches the code in several places. This makes onboarding harder and lowers trust in the project status.

**Examples**

- `README.md:18` still says ATS auto-fill works on “Greenhouse, Ashby and Workable” only, while the supported-platform table lists 8 handlers
- `PLAN.md:27`, `:60`, `:213`, `:339` still refer to `mupdf.wasm`, but the actual file is `mupdf-wasm.wasm`
- `PLAN.md:295-317` still shows nearly every implementation checkbox unchecked, even though most of v1.0 and several v1.1 items exist
- `PLAN.md` repository structure still shows only three ATS handlers

**Suggested fix**

Refresh `PLAN.md` and the top feature bullets in `README.md` to reflect the current state of the codebase.

### 10. The repo references documentation artifacts that do not exist

**Why this matters**

There are broken references in the repo today.

**Examples**

- `README.md:9` references `docs/screenshots/demo.gif`, but that file does not exist
- `companion/config.py:15` references `docs/SETUP.md`, but that file does not exist

**Suggested fix**

Either add the missing files, or remove the references until they exist.

---

## Suggested next order of work

1. Lock down the companion API (`CORS`, auth token, remove free-form `output_dir`)
2. Decide whether AI configuration lives in the extension or the companion, then make the UI/backend consistent
3. Fix the resume-loaded state and scrape-error handling in the popup
4. Escape HTML export metadata
5. Clean up the docs so the repo matches reality
