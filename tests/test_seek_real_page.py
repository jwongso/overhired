"""
Integration test: extract job info from a real live SEEK page.

Fetches https://nz.seek.com/job/91936975 directly, then runs the full
extract pipeline (LLM call included).

On success, saves nz.seek.com.py parser to ~/.grapply/parsers/.

Run:
    cd grapply && pytest tests/test_seek_real_page.py -v -s
"""
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "companion"))

import extractor
import tool_server

SEEK_URL = "https://nz.seek.com/job/91936975?type=standard&ref=search-standalone&origin=cardTitle"
DOMAIN = "nz.seek.com"
PARSERS_DIR = Path("~/.grapply/parsers").expanduser()
EXPECTED_TITLE = "Embedded Software Engineer"
EXPECTED_COMPANY = "Fisher & Paykel Healthcare"

# Fallback: use saved real page if network is unavailable
_SAVED_HTML = (
    Path(__file__).parent
    / "real_page"
    / "Embedded Software Engineer Job in Auckland - SEEK.html"
)


def _fetch_html() -> str:
    """Fetch live page, fall back to saved file."""
    try:
        req = urllib.request.Request(
            SEEK_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-NZ,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        print(f"\n[test] fetched live page: {len(html):,} chars")
        return html
    except Exception as exc:
        print(f"\n[test] live fetch failed ({exc}), using saved file")
        assert _SAVED_HTML.exists(), f"No saved fallback at {_SAVED_HTML}"
        return _SAVED_HTML.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def ai_client():
    from ai_client import AIClient
    return AIClient({
        "provider":     "ollama",
        "model":        "qwen3:8b",
        "endpoint":     "http://192.168.1.99:11434",
        "timeout":      300,
        "tool_timeout": 600,
    })


@pytest.fixture(autouse=True)
def clean_parser():
    """Remove cached parser before test so LLM is always invoked."""
    parser_file = PARSERS_DIR / f"{DOMAIN}.py"
    parser_file.unlink(missing_ok=True)
    yield
    # leave parser in place — that's the goal


def test_real_seek_page_extract_and_parser(ai_client):
    """
    Full pipeline on real SEEK page:
      1. Fetch live HTML (or saved fallback)
      2. clean_html strips to plain text
      3. LLM extracts JSON (title / company / location / description)
      4. LLM generates Python parser
      5. Parser passes safety + validation
      6. Parser saved to ~/.grapply/parsers/nz.seek.com.py
      7. Saved parser re-run against same page — must return correct title
    """
    html = _fetch_html()

    result = extractor.extract(DOMAIN, "", ai_client, page_html=html)

    print(f"[test] title    = {result.get('title')!r}")
    print(f"[test] company  = {result.get('company')!r}")
    print(f"[test] location = {result.get('location')!r}")
    print(f"[test] desc len = {len(result.get('description', ''))}")

    # ── Assert extraction ──────────────────────────────────────────────────
    assert result.get("title"), "title must not be empty"
    assert EXPECTED_TITLE.lower() in result["title"].lower(), \
        f"Expected title containing {EXPECTED_TITLE!r}, got {result['title']!r}"

    assert result.get("company"), "company must not be empty"
    assert EXPECTED_COMPANY.lower() in result["company"].lower(), \
        f"Expected company containing {EXPECTED_COMPANY!r}, got {result['company']!r}"

    # ── Assert parser was saved ────────────────────────────────────────────
    parser_file = PARSERS_DIR / f"{DOMAIN}.py"
    assert parser_file.exists(), (
        f"Parser NOT saved to {parser_file}. "
        "Check ~/.grapply/companion.log for [save_parser] lines."
    )
    print(f"[test] ✅ Parser saved: {parser_file}")
    print(f"[test] Parser content:\n{parser_file.read_text()}")

    # ── Assert saved parser works on same page ─────────────────────────────
    code = parser_file.read_text(encoding="utf-8")
    page_text = extractor.clean_html(html, domain=DOMAIN)
    from tool_server import run_parser
    cached = run_parser(code=code, text=page_text)
    print(f"[test] cached parser output: {cached}")
    assert cached.get("title"), "Saved parser must extract title from the same page"

