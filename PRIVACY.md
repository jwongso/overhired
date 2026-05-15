# Privacy Policy

grapply is designed to keep your personal data on your device.

## What stays local (never leaves your machine)

| Data | Storage |
|------|---------|
| Resume PDF | Parsed in-browser by MuPDF WASM; the PDF itself is never transmitted |
| Extracted resume text | `chrome.storage.local` |
| Your profile (name, email, address…) | `chrome.storage.local` |
| Generated cover letters | Local filesystem via the companion service |
| API keys | `chrome.storage.local` |

## What is sent to external services

| Data | Destination | When |
|------|-------------|------|
| Job description text | Your chosen AI provider | When you click "Extract & Generate" |
| Resume text | Your chosen AI provider | When you click "Extract & Generate" |
| Per-job and global instructions | Your chosen AI provider | When you click "Extract & Generate" |

If you use a **local AI** (Ollama / llama.cpp), nothing leaves your machine at all.

If you use **OpenAI or Claude**, their respective privacy policies apply to the
job description and resume text sent during generation.

## The companion service

The companion runs at `localhost:7878`. It only accepts connections from `localhost`
(bound to `127.0.0.1` by default). It writes files to your local filesystem.
It does not phone home, collect analytics, or transmit data anywhere other than
the AI endpoint you configure.

## No telemetry

grapply collects zero usage data, crash reports, or analytics of any kind.
