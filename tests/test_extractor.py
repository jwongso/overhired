"""
Unit tests for extractor.py

Uses monkeypatching to avoid real LLM calls.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tool_server
import extractor


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_parsers_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_server, "PARSERS_DIR", tmp_path)
    monkeypatch.setattr(extractor, "PARSERS_DIR", tmp_path)
    return tmp_path


GOOD_CODE = """\
def extract(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return {
        'title':       lines[0] if lines else '',
        'company':     lines[1] if len(lines) > 1 else '',
        'description': '\\n'.join(lines[2:]),
        'location':    '',
    }
"""

SAMPLE_TEXT = "Senior C++ Developer\nCompuGroup Medical\nWe are hiring talented engineers."


def _make_ai(tool_responses=None, text_response=""):
    """Build a mock AIClient."""
    ai = MagicMock()
    ai.generate.return_value = text_response
    if tool_responses is not None:
        ai.generate_with_tools.return_value = tool_responses
    return ai


# ── _try_cached ───────────────────────────────────────────────────────────────

class TestTryCached:
    def test_returns_none_when_no_parser(self):
        assert extractor._try_cached("example.com", SAMPLE_TEXT) is None

    def test_returns_dict_when_parser_works(self, tmp_parsers_dir):
        tool_server.save_parser("example.com", GOOD_CODE)
        result = extractor._try_cached("example.com", SAMPLE_TEXT)
        assert result is not None
        assert result["title"] == "Senior C++ Developer"

    def test_deletes_broken_parser_and_returns_none(self, tmp_parsers_dir):
        broken = "def extract(text): raise RuntimeError('broken')"
        tool_server.save_parser("example.com", broken)
        result = extractor._try_cached("example.com", SAMPLE_TEXT)
        assert result is None
        assert not (tmp_parsers_dir / "example.com.py").exists()

    def test_deletes_empty_title_parser_and_returns_none(self, tmp_parsers_dir):
        empty = "def extract(text): return {'title':'','company':'','description':'','location':''}"
        tool_server.save_parser("example.com", empty)
        result = extractor._try_cached("example.com", SAMPLE_TEXT)
        assert result is None
        assert not (tmp_parsers_dir / "example.com.py").exists()

    def test_no_extract_fn_returns_none(self, tmp_parsers_dir):
        tool_server.save_parser("example.com", "x = 1\n")
        result = extractor._try_cached("example.com", SAMPLE_TEXT)
        assert result is None


# ── _phase1_extract (was _oneshot_extract) ────────────────────────────────────

class TestPhase1Extract:
    def test_parses_clean_json(self):
        ai = _make_ai(text_response=json.dumps({
            "title": "Engineer", "company": "Acme",
            "description": "Build stuff", "location": "Remote"
        }))
        result = extractor._phase1_extract("example.com", SAMPLE_TEXT, ai)
        assert result["title"] == "Engineer"
        assert result["company"] == "Acme"

    def test_strips_markdown_fences(self):
        ai = _make_ai(text_response='```json\n{"title":"Dev","company":"X","description":"D","location":""}\n```')
        result = extractor._phase1_extract("example.com", SAMPLE_TEXT, ai)
        assert result["title"] == "Dev"

    def test_returns_empty_on_bad_json(self):
        ai = _make_ai(text_response="not json at all")
        result = extractor._phase1_extract("example.com", SAMPLE_TEXT, ai)
        assert result == {"title": "", "company": "", "description": "", "location": ""}

    def test_missing_keys_default_to_empty(self):
        ai = _make_ai(text_response='{"title": "Only Title"}')
        result = extractor._phase1_extract("example.com", SAMPLE_TEXT, ai)
        assert result["title"] == "Only Title"
        assert result["company"] == ""


# ── extract (integration) ─────────────────────────────────────────────────────

class TestExtract:
    def test_cache_hit_skips_llm(self, tmp_parsers_dir):
        tool_server.save_parser("example.com", GOOD_CODE)
        ai = _make_ai()
        result = extractor.extract("example.com", SAMPLE_TEXT, ai)
        assert result["title"] == "Senior C++ Developer"
        ai.generate.assert_not_called()

    def test_phase1_on_cache_miss(self, tmp_parsers_dir):
        """On cache miss with no programmatic strategy, Phase 1 LLM extraction is used."""
        ai = _make_ai(text_response=json.dumps({
            "title": "Senior C++ Developer", "company": "CompuGroup Medical",
            "description": "We are hiring talented engineers.", "location": ""
        }))
        result = extractor.extract("unknown-domain.com", SAMPLE_TEXT, ai)
        assert result["title"] == "Senior C++ Developer"
        # Phase 1 calls ai.generate at minimum once (background parser gen may add more)
        assert ai.generate.call_count >= 1

    def test_falls_back_to_empty_on_llm_failure(self, tmp_parsers_dir):
        ai = _make_ai()
        ai.generate.side_effect = Exception("model timeout")
        result = extractor.extract("unknown.com", SAMPLE_TEXT, ai)
        assert result == {"title": "", "company": "", "description": "", "location": ""}

    def test_result_has_all_keys(self, tmp_parsers_dir):
        tool_server.save_parser("example.com", GOOD_CODE)
        ai = _make_ai()
        result = extractor.extract("example.com", SAMPLE_TEXT, ai)
        assert set(result.keys()) == {"title", "company", "description", "location"}
