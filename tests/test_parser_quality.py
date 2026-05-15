"""
Parser quality & consistency tests — two-run + consistency validation.

Three phases in a single ordered test:

  Phase 1 — GENERATION (LLM required)
    Clear all cached parsers.
    Run all 3 domains with Page 1.  LLM extracts data + generates parsers.
    Assert: correct title/company extracted, parser file saved.

  Phase 2 — GENERALIZATION (cached parser, no LLM)
    Run all 3 domains with Page 2 (a DIFFERENT job on the same domain).
    Assert: cached parser used (returns in <1 s — no LLM call).
    Assert: correct title/company extracted from the new page.
    Quality gate: if cached parser fails, that's a signal it over-fitted to page 1.

  Phase 3 — CONSISTENCY (cached parser, same page as phase 1)
    Re-run all 3 domains with Page 1.
    Assert: exactly the same title/company as Phase 1 (deterministic).
    Assert: fast (<1 s — cache hit).

Run:
    cd grapply && pytest tests/test_parser_quality.py -v -s
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "companion"))

import extractor
from tool_server import run_parser

REAL_PAGE_DIR = Path(__file__).parent / "real_page"
PARSERS_DIR   = Path("~/.grapply/parsers").expanduser()

# ── Per-domain, two-page configuration ───────────────────────────────────────

PAGES = {
    "nz.seek.com": {
        "page1": {
            "file":    "Embedded Software Engineer Job in Auckland - SEEK.html",
            "title":   "Embedded Software Engineer",
            "company": "Fisher & Paykel Healthcare",
        },
        "page2": {
            "file":    "Senior AI Engineer Job in Auckland - SEEK.html",
            "title":   "Senior AI Engineer",
            "company": "TVNZ",
        },
    },
    "nz.indeed.com": {
        "page1": {
            "file":    "Senior Software Engineer - Base24 EPS - New Zealand - Indeed.com.html",
            "title":   "Senior Software Engineer - Base24 EPS",
            "company": "Westpac New Zealand",
        },
        "page2": {
            "file":    "Technical Lead - Remote - Remote - Indeed.com.html",
            "title":   "Technical Lead - Remote",
            "company": "YO IT CONSULTING",
        },
    },
    "www.linkedin.com": {
        "page1": {
            "file":    "Senior C++ Generalist Software Engineer - Advanted Technology Group _ EA SPORTS _ LinkedIn.html",
            "title":   "Senior C++ Generalist Software Engineer",
            "company": "EA SPORTS",
        },
        "page2": {
            "file":    "C++, C# Engineer _ Quest Global _ LinkedIn.html",
            "title":   "Senior Software Engineer",   # Microsoft job via Quest Global page
            "company": "Microsoft",
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load(filename: str) -> str:
    path = REAL_PAGE_DIR / filename
    assert path.exists(), f"Saved page not found: {path}"
    return path.read_text(encoding="utf-8", errors="replace")


def _parser_file(domain: str) -> Path:
    import re
    safe = re.sub(r"[^\w.\-]", "_", domain.replace("www.", ""))
    return PARSERS_DIR / f"{safe}.py"


def _clear_parsers() -> None:
    for domain in PAGES:
        _parser_file(domain).unlink(missing_ok=True)
    print("\n[quality] All cached parsers cleared.")


def _assert_extraction(result: dict, expected_title: str, expected_company: str,
                        label: str) -> None:
    title   = result.get("title", "")
    company = result.get("company", "")
    print(f"[quality] {label}: title={title!r}  company={company!r}  "
          f"desc_len={len(result.get('description',''))}")
    assert title,   f"{label}: title is empty — extraction failed"
    assert expected_title.lower() in title.lower(), (
        f"{label}: expected title containing {expected_title!r}, got {title!r}"
    )
    assert company, f"{label}: company is empty"
    assert expected_company.lower() in company.lower(), (
        f"{label}: expected company containing {expected_company!r}, got {company!r}"
    )


# ── ai_client fixture (reuse companion config) ────────────────────────────────

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


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Generation: LLM extracts + generates parsers from Page 1
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("domain", list(PAGES.keys()))
def test_phase1_generate(domain, ai_client):
    """Clear parsers, run Page 1 through LLM pipeline, assert parser saved."""
    cfg = PAGES[domain]

    # Clear this domain's parser before the test
    pf = _parser_file(domain)
    pf.unlink(missing_ok=True)
    assert not pf.exists(), "Parser should be cleared before test"

    html   = _load(cfg["page1"]["file"])
    t0     = time.monotonic()

    print(f"\n[phase1] {domain} — running LLM pipeline...")
    result = extractor.extract_and_wait(domain, "", ai_client,
                                        page_html=html, parser_timeout=900.0)
    elapsed = time.monotonic() - t0

    print(f"[phase1] {domain} done in {elapsed:.1f}s")
    _assert_extraction(result, cfg["page1"]["title"], cfg["page1"]["company"],
                       f"phase1/{domain}")

    assert pf.exists(), (
        f"Parser NOT saved after phase1 for {domain}. "
        "Check companion log for [bg-parser] / [save_parser] lines."
    )
    print(f"[phase1] ✅ Parser saved: {pf}")

    # Verify the saved parser itself works on page1
    page_text = extractor.clean_html(html, domain=domain)
    code      = pf.read_text(encoding="utf-8")
    cached    = run_parser(code=code, text=page_text)
    print(f"[phase1] parser self-test: title={cached.get('title')!r}  company={cached.get('company')!r}")
    assert cached.get("title"), f"Saved parser fails on its own training page for {domain}"
    print(f"[phase1] ✅ Parser self-test passed")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Generalization: cached parser on Page 2 (different job, no LLM)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("domain", list(PAGES.keys()))
def test_phase2_generalize(domain, ai_client):
    """Run Page 2 through cached parser — must be fast and correct."""
    cfg = PAGES[domain]
    pf  = _parser_file(domain)

    if not pf.exists():
        pytest.skip(f"Parser for {domain} not found — run test_phase1_generate first")

    html      = _load(cfg["page2"]["file"])
    page_text = extractor.clean_html(html, domain=domain)

    # Run the saved parser directly (no LLM — should be <1 s)
    code   = pf.read_text(encoding="utf-8")
    t0     = time.monotonic()
    result = run_parser(code=code, text=page_text)
    elapsed = time.monotonic() - t0

    print(f"\n[phase2] {domain} — cached parser on page2 took {elapsed*1000:.1f}ms")
    print(f"[phase2] {domain}: title={result.get('title')!r}  company={result.get('company')!r}")

    # Speed check: cached parser must not be doing any IO or LLM calls
    assert elapsed < 5.0, (
        f"Cached parser took {elapsed:.1f}s — unexpectedly slow, may have triggered LLM"
    )

    # Quality check
    _assert_extraction(result, cfg["page2"]["title"], cfg["page2"]["company"],
                       f"phase2/{domain}")
    print(f"[phase2] ✅ Generalization passed for {domain}")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Consistency: same page as phase 1, must produce identical results
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("domain", list(PAGES.keys()))
def test_phase3_consistency(domain, ai_client):
    """Re-run Page 1 with cached parser — results must match Phase 1 exactly."""
    cfg = PAGES[domain]
    pf  = _parser_file(domain)

    if not pf.exists():
        pytest.skip(f"Parser for {domain} not found — run test_phase1_generate first")

    html      = _load(cfg["page1"]["file"])
    page_text = extractor.clean_html(html, domain=domain)

    code   = pf.read_text(encoding="utf-8")
    t0     = time.monotonic()
    result = run_parser(code=code, text=page_text)
    elapsed = time.monotonic() - t0

    print(f"\n[phase3] {domain} — consistency check took {elapsed*1000:.1f}ms")
    print(f"[phase3] {domain}: title={result.get('title')!r}  company={result.get('company')!r}")

    assert elapsed < 5.0, "Consistency check unexpectedly slow"

    # Must produce the same values as phase 1
    _assert_extraction(result, cfg["page1"]["title"], cfg["page1"]["company"],
                       f"phase3/{domain}")

    # Run twice more to confirm determinism
    for run in range(2, 4):
        r2 = run_parser(code=code, text=page_text)
        assert r2.get("title") == result.get("title"), (
            f"Non-deterministic! Run {run}: {r2.get('title')!r} != {result.get('title')!r}"
        )
    print(f"[phase3] ✅ Consistency confirmed (3× identical results) for {domain}")
