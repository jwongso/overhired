"""
Tests for the direct side-panel → companion communication pattern.

Architecture context
--------------------
The Chrome MV3 service worker is killed after ~30 s of inactivity.  Routing
long LLM calls through it caused "message channel closed before a response was
received".  The fix: the side panel (popup.js) calls companion endpoints
directly via fetch().  The service worker is now only used for PARSE_PDF
(MuPDF WASM — completes in < 1 s, no idle-timeout risk).

These tests verify every companion endpoint that popup.js calls directly:
  GET  /health
  POST /scan
  POST /extract
  POST /generate
  POST /save
  GET  /jobs/{job_id}/files
  POST /fill
  DELETE /parsers/{domain}
  DELETE /fillers/{domain}

All tests use FastAPI's TestClient (in-process, no real HTTP).  External
I/O (LLM calls, filesystem writes, DB) is mocked so the suite is fast and
hermetic.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "companion"))


# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    import main
    main.CFG["auth_token"] = ""   # disable auth for all tests in this module
    return TestClient(main.app)


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:
    """popup.js polls /health every 10 s to keep the status pill accurate."""

    def test_returns_200(self, client):
        import main
        with patch.object(main.AI, "health_check", return_value=(True, "qwen3:8b")):
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_response_has_required_fields(self, client):
        import main
        with patch.object(main.AI, "health_check", return_value=(True, "qwen3:8b")):
            data = client.get("/health").json()
        assert "status" in data

    def test_companion_down_still_returns_200(self, client):
        """Even when AI is unreachable, /health returns 200 with ai_reachable=false."""
        import main
        with patch.object(main.AI, "health_check", return_value=(False, "unknown")):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["ai_reachable"] is False


# ── /scan ─────────────────────────────────────────────────────────────────────

class TestScan:
    """popup.js calls /scan to decide whether the current tab is a job posting
    or an ATS form before calling /extract."""

    def test_job_posting_detected(self, client):
        with patch("extractor.detect_mode", return_value="job_posting"):
            resp = client.post("/scan", json={"domain": "seek.co.nz", "page_html": "<html/>", "url": ""})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "job_posting"

    def test_ats_form_detected(self, client):
        with patch("extractor.detect_mode", return_value="ats_form"):
            resp = client.post("/scan", json={"domain": "boards.greenhouse.io", "page_html": "<form/>", "url": ""})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "ats_form"

    def test_missing_domain_returns_422(self, client):
        resp = client.post("/scan", json={"page_html": "<html/>"})
        assert resp.status_code == 422

    def test_page_html_and_url_are_optional(self, client):
        with patch("extractor.detect_mode", return_value="job_posting"):
            resp = client.post("/scan", json={"domain": "example.com"})
        assert resp.status_code == 200


# ── /extract ──────────────────────────────────────────────────────────────────

class TestExtract:
    """popup.js calls /extract after /scan returns job_posting.
    The LLM agentic loop runs inside the companion — this can take minutes,
    which is why it must NOT route through the service worker."""

    MOCK = {"title": "Backend Engineer", "company": "Acme", "description": "Build APIs", "location": "Remote"}

    def test_returns_job_fields(self, client):
        with patch("extractor.extract", return_value=self.MOCK), \
             patch("extractor.detect_mode", return_value="job_posting"):
            resp = client.post("/extract", json={"domain": "example.com", "page_text": "Backend Engineer..."})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Backend Engineer"
        assert data["company"] == "Acme"

    def test_mode_field_included_in_response(self, client):
        """popup.js reads mode from the /extract response to update UI state."""
        with patch("extractor.extract", return_value=self.MOCK), \
             patch("extractor.detect_mode", return_value="job_posting"):
            data = client.post("/extract", json={"domain": "x.com", "page_text": "y"}).json()
        assert "mode" in data

    def test_missing_domain_returns_422(self, client):
        resp = client.post("/extract", json={"page_text": "no domain"})
        assert resp.status_code == 422

    def test_page_text_is_optional_defaults_to_empty(self, client):
        """page_text defaults to '' — omitting it is valid and must not hang."""
        empty = {"title": "", "company": "", "description": "", "location": ""}
        with patch("extractor.extract", return_value=empty), \
             patch("extractor.detect_mode", return_value="job_posting"):
            resp = client.post("/extract", json={"domain": "x.com"})
        assert resp.status_code == 200

    def test_empty_extraction_returns_empty_strings_not_500(self, client):
        empty = {"title": "", "company": "", "description": "", "location": ""}
        with patch("extractor.extract", return_value=empty), \
             patch("extractor.detect_mode", return_value="job_posting"):
            resp = client.post("/extract", json={"domain": "x.com", "page_text": "gibberish"})
        assert resp.status_code == 200


# ── /generate ─────────────────────────────────────────────────────────────────

class TestGenerate:
    """popup.js calls /generate after the user clicks 'Generate Cover Letter'.
    LLM call can take 7–8 min on slow hardware — cannot route through SW."""

    def test_returns_cover_letter(self, client):
        import main
        with patch("main.cfg_module.load_resume_text", return_value="my resume"), \
             patch.object(main.AI, "generate", return_value="Dear Hiring Team, ..."):
            resp = client.post("/generate", json={
                "job_title": "Engineer",
                "company": "Acme",
                "job_description": "Build stuff",
                "resume_text": "",
            })
        assert resp.status_code == 200
        assert "cover_letter_md" in resp.json()

    def test_resume_text_fallback_to_companion_cache(self, client):
        """When popup sends resume_text='', companion loads it from ~/.overhired/resume.txt."""
        import main
        with patch("main.cfg_module.load_resume_text", return_value="cached resume") as load_resume, \
             patch.object(main.AI, "generate", return_value="cover letter"):
            client.post("/generate", json={
                "job_title": "SWE",
                "company": "Corp",
                "job_description": "desc",
                "resume_text": "",
            })
        load_resume.assert_called_once_with(main.CFG)

    def test_missing_required_fields_returns_422(self, client):
        resp = client.post("/generate", json={"company": "Acme"})
        assert resp.status_code == 422


# ── /save ─────────────────────────────────────────────────────────────────────

class TestSave:
    """popup.js fire-and-forgets /save immediately after /generate succeeds."""

    def test_save_returns_paths_and_job_id(self, client, tmp_path):
        import main
        original_dir = main.CFG.get("output_dir", "~/overhired-output")
        main.CFG["output_dir"] = str(tmp_path)
        try:
            with patch("main.cfg_module.load_resume_text", return_value=""):
                resp = client.post("/save", json={
                    "company": "Acme",
                    "role": "Engineer",
                    "cover_letter_md": "# Cover Letter",
                    "cover_letter_html": "<h1>Cover Letter</h1>",
                    "job_description": "",
                    "resume_text": "",
                    "domain": "",
                })
        finally:
            main.CFG["output_dir"] = original_dir
        assert resp.status_code == 200
        data = resp.json()
        assert "md_path" in data
        assert "job_id" in data

    def test_missing_cover_letter_returns_422(self, client):
        resp = client.post("/save", json={"company": "Acme", "role": "Eng"})
        assert resp.status_code == 422


# ── /jobs/{job_id}/files ──────────────────────────────────────────────────────

class TestPollFiles:
    """popup.js polls /jobs/{id}/files every 5 s to detect when background
    tasks (summary, score, insight) finish writing their files."""

    def test_returns_file_status_dict(self, client, tmp_path):
        import main, hashlib
        # Create a fake job directory matching a known job_id
        dest = tmp_path / "Acme" / "Engineer"
        dest.mkdir(parents=True)
        (dest / "cover_letter.md").write_text("# Cover")
        job_id = hashlib.md5(str(dest).encode()).hexdigest()[:12]

        original_dir = main.CFG.get("output_dir", "~/overhired-output")
        main.CFG["output_dir"] = str(tmp_path)
        try:
            resp = client.get(f"/jobs/{job_id}/files")
        finally:
            main.CFG["output_dir"] = original_dir

        assert resp.status_code == 200
        data = resp.json()
        assert data["cover_letter"] is True
        assert data["summary"] is False

    def test_unknown_job_id_returns_404(self, client, tmp_path):
        import main
        original_dir = main.CFG.get("output_dir", "~/overhired-output")
        main.CFG["output_dir"] = str(tmp_path)
        try:
            resp = client.get("/jobs/nonexistent-000/files")
        finally:
            main.CFG["output_dir"] = original_dir
        assert resp.status_code == 404


# ── /fill ─────────────────────────────────────────────────────────────────────

class TestFill:
    """popup.js calls /fill to get field-fill operations for an ATS form.
    LLM filler generation can be slow — must not route through SW."""

    def test_returns_operations_list(self, client):
        import main
        with patch("main.ats_filler_module.get_filler", return_value=[
            {"selector": "#name", "value_key": "name"},
        ]), patch("main.ats_filler_module.last_cache_hit", return_value=False):
            resp = client.post("/fill", json={
                "domain": "boards.greenhouse.io",
                "form_snapshot": [{"id": "name", "type": "text"}],
                "fill_data": {"name": "Jun"},
            })
        assert resp.status_code == 200
        data = resp.json()
        assert "operations" in data
        assert isinstance(data["operations"], list)

    def test_no_filler_returns_422(self, client):
        import main
        with patch("main.ats_filler_module.get_filler", return_value=None):
            resp = client.post("/fill", json={
                "domain": "weird.ats.io",
                "form_snapshot": [],
                "fill_data": {},
            })
        assert resp.status_code == 422

    def test_missing_domain_returns_422(self, client):
        resp = client.post("/fill", json={"form_snapshot": [], "fill_data": {}})
        assert resp.status_code == 422


# ── /parsers/{domain} DELETE ──────────────────────────────────────────────────

class TestDeleteParser:
    """popup.js calls DELETE /parsers/{domain} when the user forces
    parser regeneration from the parser cache UI."""

    def test_delete_existing_parser(self, client, tmp_path):
        parsers_dir = tmp_path / "parsers"
        parsers_dir.mkdir()
        (parsers_dir / "seek.co.nz.py").write_text("def extract(t): return {}")

        with patch("tool_server.PARSERS_DIR", parsers_dir):
            resp = client.delete("/parsers/seek.co.nz")
        assert resp.status_code == 200
        assert "deleted" in resp.json()

    def test_delete_nonexistent_parser_returns_404(self, client, tmp_path):
        with patch("tool_server.PARSERS_DIR", tmp_path / "parsers"):
            resp = client.delete("/parsers/no-such-domain.com")
        assert resp.status_code == 404


# ── /fillers/{domain} DELETE ──────────────────────────────────────────────────

class TestDeleteFiller:
    """popup.js calls DELETE /fillers/{domain} when a filler fills 0 fields,
    forcing regeneration on the next attempt."""

    def test_delete_existing_filler(self, client, tmp_path):
        fillers_dir = tmp_path / "fillers"
        fillers_dir.mkdir()
        (fillers_dir / "greenhouse.io.json").write_text("{}")

        with patch("ats_filler.FILLERS_DIR", fillers_dir):
            resp = client.delete("/fillers/greenhouse.io")
        assert resp.status_code == 200

    def test_delete_nonexistent_filler_returns_404(self, client, tmp_path):
        with patch("ats_filler.FILLERS_DIR", tmp_path / "fillers"):
            resp = client.delete("/fillers/no-such.io")
        assert resp.status_code == 404


# ── Auth enforcement ──────────────────────────────────────────────────────────

class TestAuthEnforcement:
    """Confirm all direct-fetch endpoints enforce the companion token when
    companion_token is configured — popup.js sends it via X-Overhired-Token."""

    @pytest.mark.parametrize("method,path,body", [
        ("GET",  "/health",          None),
        ("POST", "/scan",            {"domain": "x.com"}),
        ("POST", "/extract",         {"domain": "x.com", "page_text": "y"}),
        ("POST", "/generate",        {"job_title": "E", "company": "A", "job_description": "d", "resume_text": ""}),
        ("POST", "/fill",            {"domain": "x.com", "form_snapshot": [], "fill_data": {}}),
    ])
    def test_token_required_when_configured(self, method, path, body):
        import main
        main.CFG["auth_token"] = "test-secret"
        client = TestClient(main.app, raise_server_exceptions=False)
        try:
            if method == "GET":
                resp = client.get(path)
            else:
                resp = client.post(path, json=body)
            # /health is intentionally exempt from auth (liveness check)
            if path == "/health":
                assert resp.status_code == 200
            else:
                assert resp.status_code == 401

            # With correct token — should not be 401
            headers = {"X-Overhired-Token": "test-secret"}
            with patch("extractor.detect_mode", return_value="job_posting"), \
                 patch("extractor.extract", return_value={"title": "", "company": "", "description": "", "location": ""}), \
                 patch("main.cfg_module.load_resume_text", return_value="r"), \
                 patch.object(main.AI, "generate", return_value="cover"), \
                 patch("main.ats_filler_module.get_filler", return_value=[]), \
                 patch("main.ats_filler_module.last_cache_hit", return_value=False):
                if method == "GET":
                    resp2 = client.get(path, headers=headers)
                else:
                    resp2 = client.post(path, json=body, headers=headers)
            assert resp2.status_code != 401
        finally:
            main.CFG["auth_token"] = ""
