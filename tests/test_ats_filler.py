"""Tests for companion/ats_filler.py and related endpoints."""

import json
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

GOOD_OPERATIONS = [
    {"selector": "#candidate-email", "value_key": "email"},
    {"selector": "#cover-letter", "value_key": "cover_letter"},
]


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
    def test_valid_operations(self):
        assert ats_filler._looks_valid_filler(GOOD_OPERATIONS, SAMPLE_SNAPSHOT) is True

    def test_invalid_without_operations(self):
        assert ats_filler._looks_valid_filler([], SAMPLE_SNAPSHOT) is False

    def test_invalid_without_field_references(self):
        ops = [{"selector": ".totally-random", "value_key": "email"}]
        assert ats_filler._looks_valid_filler(ops, SAMPLE_SNAPSHOT) is False

    def test_invalid_value_key(self):
        ops = [{"selector": "#candidate-email", "value_key": "resume"}]
        assert ats_filler._looks_valid_filler(ops, SAMPLE_SNAPSHOT) is False


class TestTryCached:
    def test_cache_miss_returns_none(self):
        assert ats_filler._try_cached("example.com", SAMPLE_SNAPSHOT) is None

    def test_cache_hit_returns_operations(self, tmp_fillers_dir):
        path = tmp_fillers_dir / "example.com.json"
        path.write_text(json.dumps(GOOD_OPERATIONS), encoding="utf-8")

        assert ats_filler._try_cached("example.com", SAMPLE_SNAPSHOT) == GOOD_OPERATIONS

    def test_invalid_cache_deleted(self, tmp_fillers_dir):
        path = tmp_fillers_dir / "example.com.json"
        path.write_text('{"oops": true}', encoding="utf-8")

        assert ats_filler._try_cached("example.com", SAMPLE_SNAPSHOT) is None
        assert not path.exists()


class TestGetFiller:
    def test_cache_hit_skips_llm(self, tmp_fillers_dir):
        path = tmp_fillers_dir / "example.com.json"
        path.write_text(json.dumps(GOOD_OPERATIONS), encoding="utf-8")
        ai = MagicMock()

        result = ats_filler.get_filler("example.com", SAMPLE_SNAPSHOT, ai)

        assert result == GOOD_OPERATIONS
        assert ats_filler.last_cache_hit() is True
        ai.generate.assert_not_called()

    def test_stale_cache_deleted_and_regenerated(self, tmp_fillers_dir, monkeypatch):
        path = tmp_fillers_dir / "example.com.json"
        path.write_text(json.dumps([{"selector": ".wrong", "value_key": "email"}]), encoding="utf-8")
        generator = MagicMock(return_value=GOOD_OPERATIONS)
        monkeypatch.setattr(ats_filler, "_one_shot_fill", generator)

        result = ats_filler.get_filler("example.com", SAMPLE_SNAPSHOT, MagicMock())

        assert result == GOOD_OPERATIONS
        assert ats_filler.last_cache_hit() is False
        assert json.loads(path.read_text(encoding="utf-8")) == GOOD_OPERATIONS
        generator.assert_called_once()

    def test_crash_self_heals_and_regenerates(self, tmp_fillers_dir, monkeypatch):
        path = tmp_fillers_dir / "example.com.json"
        path.write_bytes(b"\x80\x81")
        generator = MagicMock(return_value=GOOD_OPERATIONS)
        monkeypatch.setattr(ats_filler, "_one_shot_fill", generator)

        result = ats_filler.get_filler("example.com", SAMPLE_SNAPSHOT, MagicMock())

        assert result == GOOD_OPERATIONS
        assert ats_filler.last_cache_hit() is False
        assert json.loads(path.read_text(encoding="utf-8")) == GOOD_OPERATIONS
        generator.assert_called_once()


class TestFillEndpoint:
    def test_fill_endpoint_returns_operations_and_cached_flag(self, client):
        with patch.object(main.ats_filler_module, "get_filler", return_value=GOOD_OPERATIONS) as get_filler, \
             patch.object(main.ats_filler_module, "last_cache_hit", return_value=True):
            resp = client.post("/fill", json={
                "domain": "example.com",
                "form_snapshot": SAMPLE_SNAPSHOT,
                "fill_data": {"name": "Juni"},
            })

        assert resp.status_code == 200
        assert resp.json() == {"operations": GOOD_OPERATIONS, "cached": True}
        get_filler.assert_called_once()

    def test_fill_endpoint_returns_422_when_generation_fails(self, client):
        with patch.object(main.ats_filler_module, "get_filler", return_value=None), \
             patch.object(main.ats_filler_module, "last_cache_hit", return_value=False):
            resp = client.post("/fill", json={"domain": "example.com", "form_snapshot": SAMPLE_SNAPSHOT})

        assert resp.status_code == 422

    def test_delete_filler_endpoint_removes_cached_json(self, client, tmp_fillers_dir):
        path = tmp_fillers_dir / "example.com.json"
        path.write_text(json.dumps(GOOD_OPERATIONS), encoding="utf-8")

        resp = client.delete("/fillers/example.com")

        assert resp.status_code == 200
        assert resp.json() == {"deleted": "example.com"}
        assert not path.exists()


class TestHealthEndpoint:
    def test_health_includes_profile_and_fillers_cached(self, client, tmp_fillers_dir):
        (tmp_fillers_dir / "a.com.json").write_text(json.dumps(GOOD_OPERATIONS), encoding="utf-8")
        (tmp_fillers_dir / "b.com.json").write_text(json.dumps(GOOD_OPERATIONS), encoding="utf-8")
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
