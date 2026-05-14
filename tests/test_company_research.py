"""Tests for company domain resolution and research_company().

Unit tests (no network) verify the resolution logic with mocked HTTP.
Integration tests (marked 'integration') make real network calls — run with:
    pytest -m integration tests/test_company_research.py -v
"""
import json
import re
import urllib.parse
from unittest.mock import MagicMock, patch

import httpx
import pytest

from analyzer import (
    _JOB_BOARDS,
    _resolve_company_domain,
    _safe_domain,
    research_company,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ai(domain_answer: str = "unknown", extract_response: str = "{}") -> MagicMock:
    ai = MagicMock()
    def _gen(system, user):
        if "company domain lookup tool" in system:
            return domain_answer
        return extract_response
    ai.generate.side_effect = _gen
    return ai


def _mock_site(html: str, url: str) -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = 200
    m.headers = {"content-type": "text/html"}
    m.text = html
    m.url = url
    return m


def _mock_404() -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = 404
    m.text = ""
    m.url = ""
    return m


def _wiki_opensearch(urls=()) -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = 200
    m.json.return_value = ["q", [], [], list(urls)]
    return m


def _wiki_extlinks(*links: str) -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = 200
    m.json.return_value = {
        "query": {"pages": {"1": {"extlinks": [{"*": l} for l in links]}}}
    }
    return m


def _patch_http(site_map: dict):
    """Patch httpx.get (Wikipedia) and httpx.Client (site fetches).
    site_map: { url_substring: (html, final_url) }  — 404 if not matched.
    """
    def _get(url, **kw):
        url = str(url)
        if "wikipedia" in url and "opensearch" in url:
            return _wiki_opensearch()  # no Wikipedia results by default
        if "wikipedia" in url:
            return _wiki_extlinks()
        for key, (html, final_url) in site_map.items():
            if key in url:
                return _mock_site(html, final_url)
        return _mock_404()

    client = MagicMock()
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = _get

    return (
        patch("analyzer.httpx.get", side_effect=_get),
        patch("analyzer.httpx.Client", return_value=client),
    )


# ── Unit: Strategy 1 — LLM knowledge base ────────────────────────────────────

class TestLLMStrategy:
    def test_llm_returns_correct_domain(self):
        """LLM says halterhq.com → verified → returned."""
        site_map = {"halterhq.com": ("<html><title>Halter cattle</title></html>", "https://www.halterhq.com/")}
        ai = _make_ai("halterhq.com")
        p1, p2 = _patch_http(site_map)
        with p1, p2:
            result = _resolve_company_domain("Halter", ai=ai)
        assert result == "www.halterhq.com"

    def test_llm_unknown_falls_to_wikipedia(self):
        """LLM says 'unknown' → Wikipedia strategy runs next."""
        ai = _make_ai("unknown")
        wiki_os = _wiki_opensearch(["https://en.wikipedia.org/wiki/Stripe_(company)"])
        wiki_el = _wiki_extlinks("https://stripe.com/")
        site_resp = _mock_site("<html><title>Stripe payments</title></html>", "https://stripe.com/")

        wiki_calls = iter([wiki_os, wiki_el])

        def fake_get(url, **kw):
            url = str(url)
            if "wikipedia" in url:
                return next(wiki_calls)
            return site_resp

        client = MagicMock(); client.__enter__ = lambda s: s; client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = site_resp

        with patch("analyzer.httpx.get", side_effect=fake_get), \
             patch("analyzer.httpx.Client", return_value=client):
            result = _resolve_company_domain("Stripe", ai=ai)
        assert result == "stripe.com"

    def test_llm_wrong_domain_falls_to_tld(self):
        """LLM says domain that doesn't mention the company → content check fails → TLD runs."""
        # halter.com returns "HFG Capital" — wrong content; halter.co.nz works
        site_map = {
            "halterhq.com": ("<html>HFG Capital</html>", "https://halterhq.com/"),  # content mismatch
            "halter.com":   ("<html>HFG Capital</html>", "https://halter.com/"),
            "halter.co.nz": ("<html><title>Halter smart collar</title></html>", "https://www.halterhq.com/"),
        }
        ai = _make_ai("halterhq.com")  # LLM gives wrong domain (content won't match)
        p1, p2 = _patch_http(site_map)
        with p1, p2:
            result = _resolve_company_domain("Halter", ai=ai)
        assert result == "www.halterhq.com"

    def test_llm_not_provided_skips_to_wikipedia(self):
        """When ai=None, Wikipedia is tried first."""
        ai = None
        wiki_os = _wiki_opensearch(["https://en.wikipedia.org/wiki/Stripe"])
        wiki_el = _wiki_extlinks("https://stripe.com/")
        site_resp = _mock_site("<html><title>Stripe payments</title></html>", "https://stripe.com/")

        wiki_calls = iter([wiki_os, wiki_el])

        def fake_get(url, **kw):
            if "wikipedia" in str(url):
                return next(wiki_calls)
            return site_resp

        client = MagicMock(); client.__enter__ = lambda s: s; client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = site_resp

        with patch("analyzer.httpx.get", side_effect=fake_get), \
             patch("analyzer.httpx.Client", return_value=client):
            result = _resolve_company_domain("Stripe", ai=ai)
        assert result == "stripe.com"

    def test_empty_company_name(self):
        with patch("analyzer.httpx.get"), patch("analyzer.httpx.Client"):
            assert _resolve_company_domain("", ai=None) is None
            assert _resolve_company_domain("!!!", ai=None) is None


# ── Unit: Content verification guards against wrong company ──────────────────

class TestContentVerification:
    def test_wrong_content_rejected_correct_accepted(self):
        """halter.com says HFG Capital (no keyword match) → rejected.
        halter.co.nz says Halter → accepted, final URL is halterhq.com."""
        site_map = {
            "halter.com":   ("<html>HFG Capital Investments</html>", "https://halter.com/"),
            "halter.co.nz": ("<html><title>Halter — cattle management</title></html>", "https://www.halterhq.com/"),
        }
        ai = _make_ai("unknown")  # LLM doesn't know
        p1, p2 = _patch_http(site_map)
        with p1, p2:
            result = _resolve_company_domain("Halter", ai=ai)
        assert result == "www.halterhq.com", f"Expected www.halterhq.com, got {result}"

    def test_redirect_final_url_used(self):
        """Returned hostname is from the final redirect, not the guessed domain."""
        site_map = {
            "halter.co.nz": ("<html>Halter cattle</html>", "https://www.halterhq.com/"),
        }
        ai = _make_ai("unknown")
        p1, p2 = _patch_http(site_map)
        with p1, p2:
            result = _resolve_company_domain("Halter", ai=ai)
        assert result == "www.halterhq.com"

    def test_all_wrong_returns_none(self):
        site_map = {d: ("<html>Unrelated Inc</html>", f"https://{d}/") for d in [
            "widget.com", "widget.io", "widget.co", "widget.co.nz",
            "widget.com.au", "widget.ai", "widget.tech",
        ]}
        ai = _make_ai("widget.com")  # LLM also wrong
        p1, p2 = _patch_http(site_map)
        with p1, p2:
            result = _resolve_company_domain("Widget", ai=ai)
        assert result is None


# ── Unit: research_company passes ai to resolver ─────────────────────────────

class TestResearchCompanyAI:
    _AI_RESPONSE = json.dumps({
        "overview": "Halter makes smart cattle collars.",
        "products_services": ["Smart collar"], "industry": "Agri-tech",
        "size_stage": "scaleup (50-500)", "tech_stack_hints": [],
        "culture_signals": [], "mission_statement": "",
        "red_flags": [], "green_flags": [], "notable": "",
    })

    def test_seek_uses_llm_to_resolve_domain(self):
        """research_company from seek.co.nz: AI resolves domain, site is fetched, not seek."""
        site_resp = _mock_site(
            "<html><body>" + "<p>Halter smart cattle collars.</p>" * 20 + "</body></html>",
            "https://www.halterhq.com/"
        )

        def fake_gen(system, user):
            if "company domain lookup tool" in system:
                return "halterhq.com"
            return self._AI_RESPONSE  # company research response

        ai = MagicMock(); ai.generate.side_effect = fake_gen

        def fake_get(url, **kw):
            if "wikipedia" in str(url):
                return _wiki_opensearch()
            return site_resp

        client = MagicMock(); client.__enter__ = lambda s: s; client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = site_resp

        with patch("analyzer.httpx.get", side_effect=fake_get), \
             patch("analyzer.httpx.Client", return_value=client):
            result = research_company("seek.co.nz", "Halter", ai)

        assert "error" not in result
        assert result["overview"] == "Halter makes smart cattle collars."
        fetched = [str(c.args[0]) for c in client.get.call_args_list]
        assert all("seek.co.nz" not in u for u in fetched)
        assert any("halterhq.com" in u for u in fetched)

    def test_direct_domain_skips_resolution(self):
        """Direct company domain → no LLM domain lookup, no Wikipedia."""
        site_resp = _mock_site("<html>" + "Halter " * 50 + "</html>", "https://halterhq.com/")

        def fake_gen(system, user):
            return self._AI_RESPONSE

        ai = MagicMock(); ai.generate.side_effect = fake_gen

        with patch("analyzer.httpx.get") as mock_get, \
             patch("analyzer.httpx.Client") as cc:
            c = MagicMock(); c.__enter__ = lambda s: s; c.__exit__ = MagicMock(return_value=False)
            c.get.return_value = site_resp; cc.return_value = c
            research_company("halterhq.com", "Halter", ai)

        # Domain lookup prompt must NOT have been called
        domain_lookup_calls = [
            c for c in ai.generate.call_args_list
            if "official website domain" in c[0][0]
        ]
        assert not domain_lookup_calls, "LLM was asked for domain even on a direct URL"


# ── Integration: real network calls ──────────────────────────────────────────

@pytest.mark.integration
class TestIntegration:
    """Real network + real LLM tests. Run with: pytest -m integration"""

    def test_halter_resolves_to_halterhq_not_halter_com(self):
        """Regression: Halter must NOT resolve to halter.com (HFG Capital)."""
        from ai_client import AIClient
        import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../companion"))
        import config as cfg_module
        cfg = cfg_module.load()
        ai = AIClient(cfg["ai"])

        result = _resolve_company_domain("Halter", ai=ai)
        assert result is not None, "Could not resolve domain for Halter"
        assert result != "halter.com", f"Got wrong domain halter.com (HFG Capital): {result}"
        assert "halter" in result.lower(), f"Domain doesn't contain 'halter': {result}"

    def test_stripe_resolves(self):
        from ai_client import AIClient
        import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../companion"))
        import config as cfg_module
        cfg = cfg_module.load()
        ai = AIClient(cfg["ai"])
        result = _resolve_company_domain("Stripe", ai=ai)
        assert result and "stripe.com" in result

    def test_research_halter_from_seek_no_hfg_capital(self):
        """End-to-end: LLM must NOT receive HFG Capital content."""
        from ai_client import AIClient
        import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../companion"))
        import config as cfg_module
        cfg = cfg_module.load()
        ai = AIClient(cfg["ai"])

        result = research_company("seek.co.nz", "Halter", ai)
        assert "error" not in result, f"research_company failed: {result}"

        call_args = ai.generate.call_args_list
        all_prompts = " ".join(str(c) for c in call_args)
        assert "HFG Capital" not in all_prompts, (
            "LLM was fed HFG Capital content — domain resolution failed!"
        )
