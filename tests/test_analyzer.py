"""Tests for companion/analyzer.py — decode_jargon, score_job_fit, research_company."""
import json
from unittest.mock import MagicMock, patch

import httpx

from analyzer import decode_jargon, score_job_fit, research_company


def _make_ai(response=None, side_effect=None):
    ai = MagicMock()
    if side_effect is not None:
        ai.generate.side_effect = side_effect
    else:
        ai.generate.return_value = response if response is not None else "{}"
    return ai


class TestDecodeJargon:
    def test_decode_jargon_catches_dict_red_flag(self):
        ai = _make_ai("{}")
        result = decode_jargon("We need someone who thrives in a fast-paced environment.", ai)
        assert any("fast-paced environment" in flag["phrase"] for flag in result["red_flags"])

    def test_decode_jargon_catches_green_flag(self):
        ai = _make_ai("{}")
        result = decode_jargon("We are a remote-first team with async-first communication.", ai)
        assert result["green_flags"]

    def test_decode_jargon_llm_augments_flags(self):
        ai = _make_ai(json.dumps({
            "additional_red_flags": [{"phrase": "wear many hats", "reality": "understaffed"}],
            "additional_green_flags": [],
            "overall_vibe": "chaotic",
            "verdict": "Apply with caution",
        }))
        result = decode_jargon("Nice sounding job.", ai)
        assert {flag["phrase"] for flag in result["red_flags"]} >= {"wear many hats"}

    def test_decode_jargon_llm_error_graceful(self):
        ai = _make_ai(side_effect=RuntimeError("llm unavailable"))
        result = decode_jargon("Just a normal role.", ai)
        assert "red_flags" in result
        assert "green_flags" in result
        assert result["red_flags"] == []
        assert result["green_flags"] == []

    def test_decode_jargon_verdict_propagated(self):
        ai = _make_ai(json.dumps({
            "additional_red_flags": [],
            "additional_green_flags": [],
            "overall_vibe": "rough",
            "verdict": "Skip",
        }))
        result = decode_jargon("Some role text", ai)
        assert result["verdict"] == "Skip"


class TestScoreJobFit:
    def test_score_job_fit_returns_valid_shape(self):
        ai = _make_ai(json.dumps({
            "score": 8,
            "matching_skills": ["Python", "FastAPI"],
            "missing_skills": ["Kubernetes"],
            "overqualified_risk": False,
            "experience_gap": "minor",
            "recommendation": "Apply",
            "reasoning": "Strong overlap.",
        }))
        result = score_job_fit("Python and FastAPI", "Python, FastAPI", ai)
        assert {"score", "recommendation", "matching_skills", "missing_skills", "reasoning"}.issubset(result)

    def test_score_job_fit_clamps_score_high(self):
        ai = _make_ai(json.dumps({
            "score": 15,
            "matching_skills": [],
            "missing_skills": [],
            "overqualified_risk": False,
            "experience_gap": "none",
            "recommendation": "Apply",
            "reasoning": "Too high originally.",
        }))
        result = score_job_fit("JD", "Resume", ai)
        assert result["score"] == 10

    def test_score_job_fit_clamps_score_low(self):
        ai = _make_ai(json.dumps({
            "score": -3,
            "matching_skills": [],
            "missing_skills": [],
            "overqualified_risk": False,
            "experience_gap": "significant",
            "recommendation": "Skip",
            "reasoning": "Too low originally.",
        }))
        result = score_job_fit("JD", "Resume", ai)
        assert result["score"] == 0

    def test_score_job_fit_llm_error_returns_gracefully(self):
        ai = _make_ai(side_effect=RuntimeError("boom"))
        result = score_job_fit("JD", "Resume", ai)
        assert result["score"] == 0
        assert "error" in result


class TestResearchCompany:
    def test_research_company_happy_path(self):
        html = "<html><body>" + ("<p>Acme builds developer tools for distributed teams.</p>" * 20) + "</body></html>"
        response = MagicMock(status_code=200, headers={"content-type": "text/html"}, text=html)
        ai = _make_ai(json.dumps({
            "overview": "Acme builds developer tools.",
            "products_services": ["Tooling"],
            "industry": "tech",
            "size_stage": "scaleup (50-500)",
            "tech_stack_hints": ["Python"],
            "culture_signals": ["remote-first"],
            "mission_statement": "Help teams ship faster",
            "red_flags": [],
            "green_flags": ["salary transparency"],
            "notable": "Fast growth",
        }))
        with patch("analyzer.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            result = research_company("acme.com", "Acme", ai)
        assert result["overview"] == "Acme builds developer tools."

    def test_research_company_ssrf_blocked(self):
        ai = _make_ai("{}")
        result = research_company("192.168.1.1", "Acme", ai)
        assert "error" in result
        assert "Private/loopback domain" in result["error"]

    def test_research_company_fetch_failure_graceful(self):
        ai = _make_ai(json.dumps({
            "overview": "Fallback overview.",
            "products_services": [],
            "industry": "unknown",
            "size_stage": "unknown",
            "tech_stack_hints": [],
            "culture_signals": [],
            "mission_statement": "",
            "red_flags": [],
            "green_flags": [],
            "notable": "",
        }))
        with patch("analyzer.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("offline")
            result = research_company("acme.com", "Acme", ai)
        assert result["overview"] == "Fallback overview."

    def test_research_company_llm_error(self):
        html = "<html><body>" + ("<p>Acme builds developer tools for distributed teams.</p>" * 20) + "</body></html>"
        response = MagicMock(status_code=200, headers={"content-type": "text/html"}, text=html)
        ai = _make_ai(side_effect=RuntimeError("model down"))
        with patch("analyzer.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            result = research_company("acme.com", "Acme", ai)
        assert "error" in result
