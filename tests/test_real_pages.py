"""
Integration tests: extract job info from real saved pages (LinkedIn, Indeed, SEEK).

Each test runs the full pipeline:
  1. Load saved HTML (live fetch attempted first for SEEK; saved fallback for all)
  2. clean_html → structured plain text
  3. LLM extracts JSON + generates Python parser
  4. Parser passes safety + validation
  5. Parser saved to ~/.overhired/parsers/<domain>.py
  6. Saved parser re-run against same page — must return correct title

Run:
    cd overhired && pytest tests/test_real_pages.py -v -s
    cd overhired && pytest tests/test_real_pages.py -v -s -k seek
    cd overhired && pytest tests/test_real_pages.py -v -s -k indeed
    cd overhired && pytest tests/test_real_pages.py -v -s -k linkedin
"""
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "companion"))

import extractor
from tool_server import run_parser

REAL_PAGE_DIR = Path(__file__).parent / "real_page"
PARSERS_DIR   = Path("~/.overhired/parsers").expanduser()

# ── Domain configurations ─────────────────────────────────────────────────────

DOMAINS = {
    "nz.seek.com": {
        "live_url": "https://nz.seek.com/job/91936975?type=standard&ref=search-standalone&origin=cardTitle",
        "saved_file": "Embedded Software Engineer Job in Auckland - SEEK.html",
        "expected_title":   "Embedded Software Engineer",
        "expected_company": "Fisher & Paykel Healthcare",
    },
    "nz.indeed.com": {
        "live_url":   None,   # Indeed blocks bot fetches — saved file only
        "saved_file": "Senior Software Engineer - Base24 EPS - New Zealand - Indeed.com.html",
        "expected_title":   "Senior Software Engineer - Base24 EPS",
        "expected_company": "Westpac New Zealand",
    },
    "www.linkedin.com": {
        "live_url":   None,   # LinkedIn requires login — saved file only
        "saved_file": "Senior C++ Generalist Software Engineer - Advanted Technology Group _ EA SPORTS _ LinkedIn.html",
        "expected_title":   "Senior C++ Generalist Software Engineer",
        "expected_company": "EA SPORTS",
    },
}


# ── Shared fixtures ───────────────────────────────────────────────────────────

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


def _load_html(domain: str) -> str:
    """Attempt live fetch (SEEK only); fall back to saved file."""
    cfg        = DOMAINS[domain]
    saved_path = REAL_PAGE_DIR / cfg["saved_file"]

    if cfg["live_url"]:
        try:
            req = urllib.request.Request(
                cfg["live_url"],
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
            print(f"\n[test] fetched live page for {domain}: {len(html):,} chars")
            return html
        except Exception as exc:
            print(f"\n[test] live fetch failed ({exc}), using saved file")

    assert saved_path.exists(), f"Saved page not found: {saved_path}"
    html = saved_path.read_text(encoding="utf-8", errors="replace")
    print(f"\n[test] using saved page for {domain}: {len(html):,} chars")
    return html


def _run_domain_test(domain: str, ai_client) -> None:
    """Full pipeline test for a single domain."""
    cfg = DOMAINS[domain]

    # ── Clean parser cache so LLM is always invoked ───────────────────────────
    import re as _re
    safe = _re.sub(r"[^\w.\-]", "_", domain.replace("www.", ""))
    parser_file = PARSERS_DIR / f"{safe}.py"
    parser_file.unlink(missing_ok=True)

    html = _load_html(domain)

    # ── Show what clean_html produces ─────────────────────────────────────────
    page_text = extractor.clean_html(html, domain=domain)
    lines = [l for l in page_text.splitlines() if l.strip()]
    print(f"[test] clean_html: {len(page_text)} chars, {len(lines)} lines")
    for i, l in enumerate(lines[:5]):
        print(f"[test]   [{i}] {l[:80]!r}")

    # ── LLM extraction + wait for background parser ───────────────────────────
    result = extractor.extract_and_wait(domain, "", ai_client, page_html=html,
                                        parser_timeout=900.0)

    print(f"[test] title    = {result.get('title')!r}")
    print(f"[test] company  = {result.get('company')!r}")
    print(f"[test] location = {result.get('location')!r}")
    print(f"[test] desc len = {len(result.get('description', ''))}")

    # ── Assert extraction ─────────────────────────────────────────────────────
    assert result.get("title"), "title must not be empty"
    assert cfg["expected_title"].lower() in result["title"].lower(), (
        f"Expected title containing {cfg['expected_title']!r}, "
        f"got {result['title']!r}"
    )
    assert result.get("company"), "company must not be empty"
    assert cfg["expected_company"].lower() in result["company"].lower(), (
        f"Expected company containing {cfg['expected_company']!r}, "
        f"got {result['company']!r}"
    )

    # ── Assert parser was saved ───────────────────────────────────────────────
    assert parser_file.exists(), (
        f"Parser NOT saved to {parser_file}. "
        "Check companion log for [save_parser] lines."
    )
    print(f"[test] ✅ Parser saved: {parser_file}")

    # ── Assert saved parser works on the same page ────────────────────────────
    code   = parser_file.read_text(encoding="utf-8")
    cached = run_parser(code=code, text=page_text)
    print(f"[test] cached parser: title={cached.get('title')!r}  "
          f"company={cached.get('company')!r}")
    assert cached.get("title"), (
        f"Saved parser must extract title from the same page. "
        f"Parser output: {cached}"
    )
    print(f"[test] ✅ Cached parser works")


# ── Individual test functions (named for -k filtering) ────────────────────────

def test_seek_extract_and_parser(ai_client):
    """Full pipeline test for nz.seek.com."""
    _run_domain_test("nz.seek.com", ai_client)


def test_indeed_extract_and_parser(ai_client):
    """Full pipeline test for nz.indeed.com (saved page only)."""
    _run_domain_test("nz.indeed.com", ai_client)


def test_linkedin_extract_and_parser(ai_client):
    """Full pipeline test for www.linkedin.com (saved page only)."""
    _run_domain_test("www.linkedin.com", ai_client)
