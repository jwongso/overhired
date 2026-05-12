# overhired findings

All issues from the original audit have been resolved. Summary below.

## Resolved

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

## Current known limitations

- Claude `max_tokens` was 1024 - raised to 2048 to safely cover a 450-word letter with formatting overhead.
- Calendar data (auspice integration) covers May-December 2026 only. Update `fs.json` in the auspice repo when 2027 data is available.
