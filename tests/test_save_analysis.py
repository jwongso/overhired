"""Tests for /save analysis generation and markdown formatters."""
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main
from main import (
    _bg_write_insight,
    _bg_write_score,
    _bg_write_summary,
    _format_insight,
    _format_score,
    _format_summary,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    main.CFG["auth_token"] = ""
    monkeypatch.setitem(main.CFG, "output_dir", str(tmp_path))
    return TestClient(main.app)


class TestSaveEndpoint:
    def test_save_without_analysis_fields(self, client, tmp_path):
        resp = client.post("/save", json={
            "company": "Acme",
            "role": "Engineer",
            "cover_letter_md": "# Hello",
            "cover_letter_html": "<p>Hello</p>",
        })
        assert resp.status_code == 200
        data = resp.json()
        md_path = Path(data["md_path"])
        html_path = Path(data["html_path"])
        assert data["job_id"]
        assert len(data["job_id"]) == 12
        assert md_path.exists()
        assert html_path.exists()
        assert not (md_path.parent / "summary.md").exists()
        assert not (md_path.parent / "score.md").exists()
        assert not (md_path.parent / "insight.md").exists()

    def test_save_with_all_analysis_fields(self, client):
        with patch("main._bg_write_insight") as insight, patch("main._bg_write_score") as score, patch("main._bg_write_summary") as summary:
            resp = client.post("/save", json={
                "company": "Acme",
                "role": "Engineer",
                "cover_letter_md": "# Hello",
                "cover_letter_html": "<p>Hello</p>",
                "job_description": "Dynamic environment with Python",
                "resume_text": "Python developer",
                "domain": "acme.com",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["md_path"].endswith("cover_letter.md")
        assert data["html_path"].endswith("cover_letter.html")
        assert data["job_id"]
        insight.assert_called_once()
        score.assert_called_once()
        summary.assert_called_once()

    def test_job_files_found(self, client):
        resp = client.post("/save", json={
            "company": "Acme",
            "role": "Engineer",
            "cover_letter_md": "# Hello",
            "cover_letter_html": "<p>Hello</p>",
        })
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        files_resp = client.get(f"/jobs/{job_id}/files")
        assert files_resp.status_code == 200
        assert files_resp.json() == {
            "job_id": job_id,
            "cover_letter": True,
            "summary": False,
            "score": False,
            "insight": False,
        }

    def test_job_files_not_found(self, client):
        resp = client.get("/jobs/doesnotexist/files")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "job_id 'doesnotexist' not found"


class TestBackgroundWriters:
    def test_bg_write_insight_creates_file(self, tmp_path):
        with patch("main.analyzer_module.decode_jargon", return_value={
            "verdict": "Apply",
            "overall_vibe": "Looks good.",
            "red_flags": [{"phrase": "fast-paced", "reality": "overtime"}],
            "green_flags": [],
        }):
            _bg_write_insight(tmp_path, "Dynamic environment...", "Engineer", "Acme")
        content = (tmp_path / "insight.md").read_text(encoding="utf-8")
        assert "# Role Insights" in content
        assert "fast-paced" in content

    def test_bg_write_score_creates_file(self, tmp_path):
        with patch("main.analyzer_module.score_job_fit", return_value={
            "score": 7,
            "recommendation": "Apply",
            "matching_skills": ["Python"],
            "missing_skills": ["Kubernetes"],
            "reasoning": "Good fit.",
            "experience_gap": "minor",
            "overqualified_risk": False,
        }):
            _bg_write_score(tmp_path, "5+ years Python", "Resume text", "Engineer", "Acme")
        content = (tmp_path / "score.md").read_text(encoding="utf-8")
        assert "# Job Fit" in content
        assert "7/10" in content

    def test_bg_write_summary_creates_file(self, tmp_path):
        with patch("main.analyzer_module.research_company", return_value={
            "overview": "We do stuff.",
            "industry": "tech",
            "products_services": ["API"],
        }):
            _bg_write_summary(tmp_path, "acme.com", "Acme")
        content = (tmp_path / "summary.md").read_text(encoding="utf-8")
        assert "# Company Research" in content
        assert "We do stuff." in content

    def test_bg_write_insight_handles_ai_error(self, tmp_path):
        with patch("main.analyzer_module.decode_jargon", side_effect=RuntimeError("boom")):
            _bg_write_insight(tmp_path, "Dynamic environment...", "Engineer", "Acme")
        assert not (tmp_path / "insight.md").exists()


class TestFormatters:
    def test_format_summary_basic(self):
        text = _format_summary({"overview": "We do stuff", "industry": "tech"}, "Acme", "acme.com")
        assert text.startswith("# Company Research")
        assert "We do stuff" in text

    def test_format_summary_error(self):
        text = _format_summary({"error": "blocked"}, "Acme", "acme.com")
        assert "⚠️" in text

    def test_format_score_apply(self):
        text = _format_score({"score": 8, "recommendation": "Apply"}, "Engineer", "Acme")
        assert "8/10" in text
        assert "✅" in text

    def test_format_score_skip(self):
        text = _format_score({"score": 2, "recommendation": "Skip"}, "Engineer", "Acme")
        assert "❌" in text

    def test_format_insight_with_red_flags(self):
        text = _format_insight({
            "red_flags": [{"phrase": "fast-paced", "reality": "no work-life balance"}],
            "green_flags": [],
            "overall_vibe": "rough",
            "verdict": "Skip",
        }, "Engineer", "Acme")
        assert "| fast-paced | no work-life balance |" in text

    def test_format_insight_empty(self):
        text = _format_insight({}, "Engineer", "Acme")
        assert text.startswith("# Role Insights")
