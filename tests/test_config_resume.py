"""Tests for companion/config.py resume loading helpers."""

import sys
import types
from unittest.mock import MagicMock

import config


def test_load_resume_text_empty_path():
    assert config.load_resume_text({"resume": {"path": ""}}) == ""


def test_load_resume_text_missing_file(tmp_path):
    missing = tmp_path / "missing.txt"
    assert config.load_resume_text({"resume": {"path": str(missing)}}) == ""


def test_load_resume_text_txt(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("plain text resume\n", encoding="utf-8")
    assert config.load_resume_text({"resume": {"path": str(path)}}) == "plain text resume"


def test_load_resume_text_md(tmp_path):
    path = tmp_path / "resume.md"
    path.write_text("# Resume\n\nBuilt things.\n", encoding="utf-8")
    assert config.load_resume_text({"resume": {"path": str(path)}}) == "# Resume\n\nBuilt things."


def test_load_resume_text_pdf(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4")

    page1 = MagicMock()
    page1.extract_text.return_value = "Page one"
    page2 = MagicMock()
    page2.extract_text.return_value = "Page two"

    class FakePdfReader:
        def __init__(self, pdf_path):
            assert pdf_path == path
            self.pages = [page1, page2]

    fake_module = types.SimpleNamespace(PdfReader=FakePdfReader)
    original = sys.modules.get("pypdf")
    sys.modules["pypdf"] = fake_module
    try:
        assert config.load_resume_text({"resume": {"path": str(path)}}) == "Page one\nPage two"
    finally:
        if original is None:
            sys.modules.pop("pypdf", None)
        else:
            sys.modules["pypdf"] = original
