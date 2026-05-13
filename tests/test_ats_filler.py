"""Tests for companion/ats_filler.py and related endpoints."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import ats_filler
import main


SAMPLE_SNAPSHOT = [
    {
        "tag": "input",
        "type": "email",
        "id": "candidate-email",
        "name": "email",
        "placeholder": "Email address",
        "label": "Email address",
        "aria_label": "",
    },
    {
        "tag": "textarea",
        "type": "textarea",
        "id": "cover-letter",
        "name": "coverLetter",
        "placeholder": "Cover letter",
        "label": "Cover letter",
        "aria_label": "",
    },
]

GOOD_FILLER = """function fill(data) {
  const email = document.getElementById('candidate-email') || document.querySelector('[name="email"]');
  const cover = document.getElementById('cover-letter') || document.querySelector('[name="coverLetter"]');
  const errors = [];
  let filled = 0;
  const touch = (el, value) => {
    if (!el) return;
    el.value = value || '';
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    filled += 1;
  };
  touch(email, data.email);
  touch(cover, data.cover_letter);
  return { filled, errors };
}
"""


@pytest.fixture(autouse=True)
def tmp_fillers_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ats_filler, "FILLERS_DIR", tmp_path)
    monkeypatch.setattr(main.ats_filler_module, "FILLERS_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def client():
    main.CFG["auth_token"] = ""
    return TestClient(main.app)


class TestLooksValidFiller:
    def test_valid_code(self):
        assert ats_filler._looks_valid_filler(GOOD_FILLER, SAMPLE_SNAPSHOT) is True

    def test_invalid_without_fill_signature(self):
        assert ats_filler._looks_valid_filler("const x = 1;", SAMPLE_SNAPSHOT) is False

    def test_invalid_without_field_references(self):
        code = "function fill(data) { return { filled: 0, errors: [] }; }"
        assert ats_filler._looks_valid_filler(code, SAMPLE_SNAPSHOT) is False


class TestGetFiller:
    def test_cache_hit_skips_llm(self, tmp_fillers_dir):
        path = tmp_fillers_dir / "example.com.js"
        path.write_text(GOOD_FILLER, encoding="utf-8")
        ai = MagicMock()

        result = ats_filler.get_filler("example.com", SAMPLE_SNAPSHOT, ai)

        assert result == GOOD_FILLER
        assert ats_filler.last_cache_hit() is True
        ai.generate_with_tools.assert_not_called()

    def test_stale_cache_deleted_and_regenerated(self, tmp_fillers_dir, monkeypatch):
        path = tmp_fillers_dir / "example.com.js"
        path.write_text("function fill(data) { return { filled: 0, errors: [] }; }", encoding="utf-8")
        fresh = GOOD_FILLER.replace("candidate-email", "candidate-email")
        agent = MagicMock(return_value=fresh)
        monkeypatch.setattr(ats_filler, "_agentic_fill", agent)

        result = ats_filler.get_filler("example.com", SAMPLE_SNAPSHOT, MagicMock())

        assert result == fresh
        assert ats_filler.last_cache_hit() is False
        assert not path.exists()
        agent.assert_called_once()

    def test_crash_self_heals_and_regenerates(self, tmp_fillers_dir, monkeypatch):
        path = tmp_fillers_dir / "example.com.js"
        path.write_bytes(b"\x80\x81")
        agent = MagicMock(return_value=GOOD_FILLER)
        monkeypatch.setattr(ats_filler, "_agentic_fill", agent)

        result = ats_filler.get_filler("example.com", SAMPLE_SNAPSHOT, MagicMock())

        assert result == GOOD_FILLER
        assert ats_filler.last_cache_hit() is False
        assert not path.exists()
        agent.assert_called_once()


class TestFillEndpoint:
    def test_fill_endpoint_returns_code_and_cached_flag(self, client):
        with patch.object(main.ats_filler_module, "get_filler", return_value=GOOD_FILLER) as get_filler, \
             patch.object(main.ats_filler_module, "last_cache_hit", return_value=True):
            resp = client.post("/fill", json={
                "domain": "example.com",
                "form_snapshot": SAMPLE_SNAPSHOT,
                "fill_data": {"name": "Juni"},
            })

        assert resp.status_code == 200
        assert resp.json() == {"code": GOOD_FILLER, "cached": True}
        get_filler.assert_called_once()

    def test_fill_endpoint_returns_422_when_generation_fails(self, client):
        with patch.object(main.ats_filler_module, "get_filler", return_value=None), \
             patch.object(main.ats_filler_module, "last_cache_hit", return_value=False):
            resp = client.post("/fill", json={"domain": "example.com", "form_snapshot": SAMPLE_SNAPSHOT})

        assert resp.status_code == 422


class TestHealthEndpoint:
    def test_health_includes_profile_and_fillers_cached(self, client, tmp_fillers_dir):
        (tmp_fillers_dir / "a.com.js").write_text(GOOD_FILLER, encoding="utf-8")
        (tmp_fillers_dir / "b.com.js").write_text(GOOD_FILLER, encoding="utf-8")
        original_profile = main.CFG.get("profile")
        main.CFG["profile"] = {
            "name": "Juniarto Wongso Saputra",
            "email": "juniwssaputra@gmail.com",
            "phone": "+64 204 770601",
        }
        try:
            with patch.object(main.AI, "health_check", return_value=(True, "test-model")):
                resp = client.get("/health")
        finally:
            main.CFG["profile"] = original_profile or {}

        assert resp.status_code == 200
        data = resp.json()
        assert data["fillers_cached"] == 2
        assert data["profile"]["email"] == "juniwssaputra@gmail.com"
