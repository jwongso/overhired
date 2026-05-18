"""
Integration tests for the /extract endpoint.

Uses FastAPI's TestClient (in-process, no real HTTP) and mocks
the extractor so no LLM or file I/O happens.
"""
import json
from unittest.mock import patch

import main
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """Boot the companion app and return a TestClient."""
    import main
    # Disable auth for tests
    main.CFG["auth_token"] = ""
    return TestClient(main.app)


MOCK_RESULT = {
    "title":       "Senior C++ Developer",
    "company":     "CompuGroup Medical",
    "description": "We build healthcare software.",
    "location":    "Remote",
}


# ── /extract ──────────────────────────────────────────────────────────────────

class TestExtractEndpoint:
    def test_returns_200_with_valid_payload(self, client):
        with patch("extractor.extract", return_value=MOCK_RESULT):
            resp = client.post("/extract", json={
                "domain": "example.com",
                "page_text": "Senior C++ Developer\nCompuGroup Medical\nWe build healthcare software.",
            })
        assert resp.status_code == 200

    def test_response_shape(self, client):
        with patch("extractor.extract", return_value=MOCK_RESULT):
            resp = client.post("/extract", json={"domain": "example.com", "page_text": "..."})
        data = resp.json()
        assert {"title", "company", "description", "location", "mode"} == set(data.keys())
        assert data["title"] == "Senior C++ Developer"

    def test_extractor_called_with_correct_args(self, client):
        with patch("extractor.extract", return_value=MOCK_RESULT) as mock_ex:
            client.post("/extract", json={"domain": "seek.co.nz", "page_text": "hello"})
        mock_ex.assert_called_once()
        args = mock_ex.call_args
        assert args[0][0] == "seek.co.nz"
        assert args[0][1] == "hello"

    def test_empty_result_returns_empty_strings(self, client):
        empty = {"title": "", "company": "", "description": "", "location": ""}
        with patch("extractor.extract", return_value=empty):
            resp = client.post("/extract", json={"domain": "x.com", "page_text": "y"})
        assert resp.status_code == 200
        assert resp.json()["title"] == ""

    def test_missing_domain_returns_422(self, client):
        resp = client.post("/extract", json={"page_text": "no domain"})
        assert resp.status_code == 422

    def test_missing_page_text_defaults_to_empty(self, client):
        """page_text now defaults to empty string (not required) -- still returns 200."""
        with patch("extractor.extract", return_value=MOCK_RESULT):
            resp = client.post("/extract", json={"domain": "x.com"})
        assert resp.status_code == 200

    def test_auth_token_enforced_when_configured(self):
        import main
        main.CFG["auth_token"] = "secret"
        test_client = TestClient(main.app, raise_server_exceptions=False)
        try:
            with patch("extractor.extract", return_value=MOCK_RESULT):
                resp = test_client.post("/extract", json={"domain": "x.com", "page_text": "y"})
                assert resp.status_code == 401
                # With correct token
                resp2 = test_client.post(
                    "/extract",
                    json={"domain": "x.com", "page_text": "y"},
                    headers={"X-Grapply-Token": "secret"},
                )
            assert resp2.status_code == 200
        finally:
            main.CFG["auth_token"] = ""


# ── /health ───────────────────────────────────────────────────────────────────



class TestGenerateEndpoint:
    def test_generate_uses_companion_resume_when_extension_sends_empty(self, client):
        with patch("main.cfg_module.load_resume_text", return_value="mocked resume text") as load_resume, \
             patch.object(main.AI, "generate", return_value="Dear Hiring Team,") as generate_mock:
            resp = client.post("/generate", json={
                "job_title": "Engineer",
                "company": "Acme",
                "job_description": "Build APIs",
                "resume_text": "",
            })

        assert resp.status_code == 200
        load_resume.assert_called_once_with(main.CFG)
        _, user_prompt = generate_mock.call_args[0]
        assert "mocked resume text" in user_prompt


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        with patch.object(main.AI, "health_check", return_value=(False, "test-model")):
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_has_status_field(self, client):
        with patch.object(main.AI, "health_check", return_value=(False, "test-model")):
            resp = client.get("/health")
        assert "status" in resp.json()
