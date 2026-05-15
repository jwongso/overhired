# grapply — Setup Guide

## Requirements

| Dependency | Min version | Notes |
|-----------|-------------|-------|
| Python    | 3.10        | 3.11+ preferred (built-in `tomllib`) |
| Node.js   | 18          | Only needed for MuPDF WASM setup |
| Browser   | Chrome 109+ / Firefox 120+ | MV3 support |
| Ollama    | any         | Or another OpenAI-compatible server |

---

## 1 — Companion service

```bash
cd companion
pip install -r requirements.txt
python main.py
# → http://127.0.0.1:7878
```

### Optional config at `~/.grapply/config.toml`

```toml
[ai]
provider = "ollama"            # ollama | openai | claude
endpoint = "http://localhost:11434"
model    = "llama3.2"
api_key  = ""                  # required for openai / claude

[cover_letter]
max_words          = 450
language           = "English"

output_dir         = "~/Documents/job-applications"
companion_port     = 7878

# Shared secret — copy this value to extension Settings → Companion Token.
# Leave empty to skip authentication (suitable for trusted local setups only).
auth_token         = ""
```

Python < 3.11 requires `tomli`:

```bash
pip install tomli
```

---

## 2 — MuPDF WASM (one-time)

```bash
cd extension/wasm
node setup.js
```

This downloads and copies the MuPDF WASM files. Run once after cloning. Requires an internet connection.

---

## 3 — Load the extension

### Chrome / Chromium

1. Navigate to `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder
4. Pin the grapply icon to your toolbar

### Firefox

1. Navigate to `about:debugging#/runtime/this-firefox`
2. Click **Load Temporary Add-on** → select `extension/manifest.json`

---

## 4 — First run

1. Click the grapply icon → **Settings**
2. Drop your resume PDF — parsed locally via MuPDF WASM; never sent anywhere
3. Fill in name, email, phone, LinkedIn, address etc.
4. Choose AI provider (Ollama is the default — free and private)
5. Click **Save Settings**

---

## 5 — Security hardening (optional)

If you're on a shared machine or want to prevent other local processes from
calling the companion, set a shared secret:

**companion config.toml:**
```toml
auth_token = "your-random-secret"
```

**Extension Settings → Companion Token:** paste the same value.

The companion will then reject any request that doesn't carry the
`X-Grapply-Token` header.

---

## 6 — AI providers

### Ollama (default, recommended)

```bash
ollama pull llama3.2
ollama serve          # starts on http://localhost:11434
```

### llama.cpp server

```bash
./llama-server -m model.gguf --port 8080
```

Set **Endpoint** to `http://localhost:8080` in extension Settings.

### OpenAI / Anthropic

Set **Provider** to `openai` or `claude`, enter your API key in extension
Settings. The extension settings take precedence over `config.toml` values.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Banner shows "⚠ not reachable" despite Ollama running | Check that Ollama is listening on the configured endpoint; try `curl http://localhost:11434/v1/models` |
| "No resume found" | Upload your PDF in Settings and wait for "✓ Resume loaded" |
| PDF parse fails | Run `node setup.js` in `extension/wasm/` to download MuPDF WASM |
| Fill Form does nothing | Refresh the job page — content scripts inject at `document_idle` |
| 401 Unauthorized from companion | Token mismatch — check `auth_token` in config.toml matches extension Settings → Companion Token |
