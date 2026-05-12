# Contributing to overhired

Thank you for wanting to help! Contributions of all kinds are welcome.

## The easiest contribution: add an ATS handler

The most impactful thing you can do is add support for an ATS that is not yet covered.
See [docs/ATS_HANDLER_GUIDE.md](docs/ATS_HANDLER_GUIDE.md) for a complete step-by-step guide.

## Other contributions

- **Bug fixes** — open an issue first if the fix is non-trivial
- **AI provider adapters** — add to `companion/ai_client.py`
- **Job description extractors** — improve `extension/content_scripts/extractor.js`
- **Companion features** — new endpoints, CLI mode, packaging
- **Documentation** — corrections, translations, better screenshots

## Workflow

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Test manually (load unpacked extension + run companion locally)
4. Open a pull request with a clear description of what and why
5. Keep PRs focused — one feature or fix per PR

## Code style

- Python: PEP 8, no external formatter required
- JavaScript: 2-space indent, single quotes, no semicolons preference
- No build step — the extension must remain loadable with zero build tooling

## Reporting bugs

Use the issue template. Include your browser version, OS, ATS URL (if applicable),
and the error from the browser console or companion logs.
