# overhired findings

## New Issues

| # | Issue | Severity | Notes |
|---|-------|----------|-------|
| 1 | `mcp_server.py` delegates to `tool_server.py` but the internal agentic loop in `extractor.py` uses `tool_server.py` directly - the MCP server is wired for external clients (Claude Desktop etc.) but not used by the companion's own `/extract` flow | Low | Not a bug - two separate paths that both work. But means MCP server and internal loop can diverge if tool implementations change in `tool_server.py`. Consider making `extractor.py` use the MCP server too, or at least add a test that both paths return identical results. |
| 2 | Extension Phase 5 only partially done - `scrapeJobFromPage()` still contains the full hand-written LinkedIn/Seek/JSON-LD scraper and only falls back to `/extract` when JS scrape has no title. The LLM-generated parser is never tried first. | Low | By design for reliability, but contradicts PLAN2 intent of making `/extract` the primary path. Document this decision or flip the priority order. |
| 3 | `analyzer.py` `research_company` fetches external URLs (homepage + about page) from the companion process with no timeout, no sandbox, no SSRF protection. A crafted company name or URL could cause the companion to make arbitrary outbound requests. | Medium | Add a timeout (e.g. `httpx.get(..., timeout=10)`) and restrict to `http`/`https` schemes only. Consider allowlisting or at least blocking private IP ranges (`127.x`, `10.x`, `192.168.x`, `172.16-31.x`). |
| 4 | `tracker.py` SQLite DB at `~/.overhired/applications.db` - no schema migration logic. If the schema changes in a future version, existing installs will break silently or crash. | Low | Add a simple `PRAGMA user_version` check and migration path before any schema changes are made. |

## Previously Resolved

| # | Issue | Fix |
|---|-------|-----|
| 1 | Wildcard CORS exposed companion to any website | `allow_origin_regex` restricted to `chrome-extension://` and `moz-extension://` |
| 2 | Arbitrary file write via caller-supplied `output_dir` | `output_dir` removed from `SaveRequest`; writes always go under configured base dir |
| 3 | Extension AI settings not forwarded to generation | `service_worker.js` sends `ai_provider/endpoint/model/key` per request; companion creates per-request `AIClient` |
| 4 | Resume upload did not enable Generate until popup reopen | `onResumeLoaded` callback lifted to `App`; `SettingsTab` calls it on successful PDF parse |
| 5 | Scrape errors enabled Generate with "Unknown Role" | Popup checks `j.error` before calling `setJob`; scrape errors shown separately |
| 6 | Health check returned `true` for 4xx responses | `health_check()` now returns `True` only for `2xx` |
| 7 | Extension hardcoded companion URL | `companion_url` is now a user-configurable Settings field |
| 8 | Easter egg text not user-editable from UI | Settings tab shows a textarea for custom egg text when easter egg is enabled |
| 9 | HTML injection in exported cover letter title | `html.escape()` applied to `company` and `role` in `_md_to_html()` |
| 10 | Docs referenced `demo.gif` and missing `docs/SETUP.md` | Both references cleaned up; `docs/SETUP.md` exists |

## Known Limitations

- Claude `max_tokens` was 1024 - raised to 2048 to safely cover a 450-word letter with formatting overhead.
- Calendar data (auspice integration) covers May-December 2026 only. Update `fs.json` in the auspice repo when 2027 data is available.
