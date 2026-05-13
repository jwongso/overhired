"""
Unit tests for tool_server.py

Tests the five parser tool functions and their sandbox behaviour.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import tool_server


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_parsers_dir(tmp_path, monkeypatch):
    """Redirect PARSERS_DIR to a temp directory for every test."""
    monkeypatch.setattr(tool_server, "PARSERS_DIR", tmp_path)
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

SAMPLE_TEXT = "Senior C++ Developer\nCompuGroup Medical\nWe are hiring..."


# ── run_parser ────────────────────────────────────────────────────────────────

class TestRunParser:
    def test_valid_code_returns_correct_dict(self):
        result = tool_server.run_parser(GOOD_CODE, SAMPLE_TEXT)
        assert result["title"] == "Senior C++ Developer"
        assert result["company"] == "CompuGroup Medical"
        assert "hiring" in result["description"]
        assert result["location"] == ""

    def test_all_keys_present(self):
        result = tool_server.run_parser(GOOD_CODE, SAMPLE_TEXT)
        assert set(result.keys()) == {"title", "company", "description", "location"}

    def test_no_extract_function_returns_error(self):
        result = tool_server.run_parser("x = 1\n", SAMPLE_TEXT)
        assert "error" in result
        assert "extract" in result["error"]

    def test_import_is_blocked(self):
        code = "import os\ndef extract(text): return {'title': os.getcwd(), 'company':'','description':'','location':''}"
        result = tool_server.run_parser(code, SAMPLE_TEXT)
        assert "error" in result
        assert "not allowed" in result["error"]

    def test_open_is_blocked(self):
        code = "def extract(text):\n    open('/etc/passwd')\n    return {'title':'','company':'','description':'','location':''}"
        result = tool_server.run_parser(code, SAMPLE_TEXT)
        assert "error" in result

    def test_non_dict_return_is_error(self):
        code = "def extract(text): return 'just a string'"
        result = tool_server.run_parser(code, SAMPLE_TEXT)
        assert "error" in result
        assert "dict" in result["error"]

    def test_exception_in_extract_returns_error(self):
        code = "def extract(text): raise ValueError('oops')"
        result = tool_server.run_parser(code, SAMPLE_TEXT)
        assert "error" in result
        assert "ValueError" in result["error"]

    def test_empty_text_returns_empty_strings(self):
        code = "def extract(text): return {'title':'','company':'','description':'','location':''}"
        result = tool_server.run_parser(code, "")
        assert result["title"] == ""
        assert "error" not in result

    def test_values_coerced_to_str(self):
        code = "def extract(text): return {'title': 42, 'company': None, 'description': ['a'], 'location': True}"
        result = tool_server.run_parser(code, SAMPLE_TEXT)
        assert result["title"] == "42"
        assert result["company"] == "None"


# ── save_parser ───────────────────────────────────────────────────────────────

class TestSaveParser:
    def test_creates_file(self, tmp_parsers_dir):
        result = tool_server.save_parser("linkedin.com", GOOD_CODE)
        assert "saved" in result
        saved_path = Path(result["saved"])
        assert saved_path.exists()

    def test_file_contains_code(self, tmp_parsers_dir):
        tool_server.save_parser("seek.co.nz", GOOD_CODE)
        content = (tmp_parsers_dir / "seek.co.nz.py").read_text()
        assert "def extract" in content

    def test_file_has_header_comment(self, tmp_parsers_dir):
        tool_server.save_parser("indeed.com", GOOD_CODE)
        content = (tmp_parsers_dir / "indeed.com.py").read_text()
        assert "# Generated:" in content
        assert "indeed.com" in content

    def test_www_stripped_from_domain(self, tmp_parsers_dir):
        tool_server.save_parser("www.example.com", GOOD_CODE)
        assert (tmp_parsers_dir / "example.com.py").exists()

    def test_overwrite_existing(self, tmp_parsers_dir):
        tool_server.save_parser("example.com", "# v1\n" + GOOD_CODE)
        tool_server.save_parser("example.com", "# v2\n" + GOOD_CODE)
        content = (tmp_parsers_dir / "example.com.py").read_text()
        assert "# v2" in content
        assert "# v1" not in content


# ── read_parser ───────────────────────────────────────────────────────────────

class TestReadParser:
    def test_reads_saved_parser(self, tmp_parsers_dir):
        tool_server.save_parser("example.com", GOOD_CODE)
        result = tool_server.read_parser("example.com")
        assert "code" in result
        assert "def extract" in result["code"]

    def test_missing_parser_returns_error(self):
        result = tool_server.read_parser("notexist.com")
        assert "error" in result


# ── list_parsers ──────────────────────────────────────────────────────────────

class TestListParsers:
    def test_empty_dir(self):
        result = tool_server.list_parsers()
        assert result["count"] == 0
        assert result["parsers"] == []

    def test_lists_saved_parsers(self, tmp_parsers_dir):
        tool_server.save_parser("a.com", GOOD_CODE)
        tool_server.save_parser("b.com", GOOD_CODE)
        result = tool_server.list_parsers()
        assert result["count"] == 2
        domains = {p["domain"] for p in result["parsers"]}
        assert "a.com" in domains
        assert "b.com" in domains

    def test_each_entry_has_metadata(self, tmp_parsers_dir):
        tool_server.save_parser("meta.com", GOOD_CODE)
        result = tool_server.list_parsers()
        entry = result["parsers"][0]
        assert "domain" in entry
        assert "bytes" in entry
        assert "modified" in entry


# ── delete_parser ─────────────────────────────────────────────────────────────

class TestDeleteParser:
    def test_deletes_existing(self, tmp_parsers_dir):
        tool_server.save_parser("del.com", GOOD_CODE)
        result = tool_server.delete_parser("del.com")
        assert "deleted" in result
        assert not (tmp_parsers_dir / "del.com.py").exists()

    def test_missing_returns_error(self):
        result = tool_server.delete_parser("ghost.com")
        assert "error" in result


# ── _safe_domain ──────────────────────────────────────────────────────────────

class TestSafeDomain:
    def test_strips_www(self):
        assert tool_server._safe_domain("www.linkedin.com") == "linkedin.com"

    def test_lowercase(self):
        assert tool_server._safe_domain("LinkedIn.COM") == "linkedin.com"

    def test_special_chars_replaced(self):
        result = tool_server._safe_domain("some weird!site.com")
        assert "!" not in result
        assert " " not in result
