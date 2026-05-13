"""ATS form filler — same "LLM-compiled cache" pattern as extractor.py

Flow:
  1. Cache hit  → run cached JS filler immediately (no LLM, <10ms)
  2. Cache miss → agentic loop: LLM writes + saves a JS fill(data) function
  3. Self-heal  → if cached filler fails validation, delete + regenerate
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from tool_server import _safe_domain

if TYPE_CHECKING:
    from ai_client import AIClient

FILLERS_DIR = Path("~/.overhired/fillers").expanduser()
_log = logging.getLogger(__name__)
_LAST_CACHE_HIT = False

_SYSTEM_PROMPT = """You are an expert web automation engineer. Write a JavaScript function that fills an ATS job application form.

Function signature MUST be:
  function fill(data) { ... }

Where data = {name, email, phone, cover_letter} (all strings).

The function must:
- Fill the correct form fields based on label/id/name/placeholder
- Use document.querySelector or getElementById to find fields
- Set .value and dispatch 'input' + 'change' events (for React/Angular reactivity)
- Return {filled: number, errors: string[]}

Available tools:
- test_filler(code): validates your function. Call this first.
- save_filler(domain, code): saves the working filler. Call this after test_filler succeeds.

RULES:
- After test_filler succeeds, your ONLY next action is save_filler.
- Do NOT skip save_filler.
"""

_TEST_FILLER_TOOL = {
    "type": "function",
    "function": {
        "name": "test_filler",
        "description": (
            "Validate a JavaScript ATS filler. Checks the code contains "
            "function fill(data), references current form fields, and stays under 8000 chars."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
            },
            "required": ["code"],
        },
    },
}

_SAVE_FILLER_TOOL = {
    "type": "function",
    "function": {
        "name": "save_filler",
        "description": "Save a validated ATS filler for the domain.",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "code": {"type": "string"},
            },
            "required": ["domain", "code"],
        },
    },
}


def get_filler(domain: str, form_snapshot: list[dict], ai: "AIClient") -> str | None:
    """Return a cached or freshly-generated JS fill(data) function."""
    global _LAST_CACHE_HIT

    try:
        cached = _try_cached(domain, form_snapshot)
    except Exception as exc:
        _log.warning("[ats_cache] stale/invalid for %s — deleting (%s)", domain, exc)
        _delete_cached(domain)
        cached = None

    if cached:
        _LAST_CACHE_HIT = True
        return cached

    _LAST_CACHE_HIT = False
    try:
        code = _agentic_fill(domain, form_snapshot, ai)
    except Exception as exc:
        _log.warning("[fill] agentic loop failed for %s: %s", domain, exc)
        return None

    if code and _looks_valid_filler(code, form_snapshot):
        return code
    return None


def last_cache_hit() -> bool:
    return _LAST_CACHE_HIT


def _try_cached(domain: str, form_snapshot: list[dict]) -> str | None:
    """Return cached filler code if it still matches the current form."""
    path = FILLERS_DIR / f"{_safe_domain(domain)}.js"
    if not path.exists():
        return None

    try:
        code = path.read_text(encoding="utf-8")
        if "function fill(" not in code:
            _log.warning("[ats_cache] stale/invalid for %s — deleting", domain)
            path.unlink(missing_ok=True)
            return None
        if not _has_field_reference(code, form_snapshot, include_labels=False):
            _log.warning("[ats_cache] stale/invalid for %s — deleting", domain)
            path.unlink(missing_ok=True)
            return None
        _log.info("[ats_cache] hit for %s", domain)
        return code
    except Exception:
        _log.warning("[ats_cache] stale/invalid for %s — deleting", domain)
        path.unlink(missing_ok=True)
        return None


def _agentic_fill(domain: str, form_snapshot: list[dict], ai: "AIClient") -> str | None:
    """Run the agentic tool loop and persist a working JS filler."""
    last_good_code: list[str] = []
    saved_paths: list[str] = []

    def test_filler(code: str) -> dict:
        result = _validate_candidate(code, form_snapshot)
        if result["valid"]:
            last_good_code.clear()
            last_good_code.append(code)
            _log.info("[fill] test_filler validated for %s", domain)
        return result

    def save_filler(domain: str, code: str) -> dict:
        FILLERS_DIR.mkdir(parents=True, exist_ok=True)
        safe_domain = _safe_domain(domain)
        path = FILLERS_DIR / f"{safe_domain}.js"
        header = (
            f"// Generated: {date.today()}  Domain: {safe_domain}\n"
            f"// To regenerate: delete this file and refill a {safe_domain} ATS form.\n\n"
        )
        path.write_text(header + code, encoding="utf-8")
        saved_paths.append(str(path))
        return {"saved": str(path)}

    user_prompt = (
        f"Domain: {domain}\n\n"
        f"Form fields:\n{json.dumps(form_snapshot, indent=2)}"
    )

    try:
        loop_result = ai.generate_with_tools(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tools=[_TEST_FILLER_TOOL, _SAVE_FILLER_TOOL],
            tool_functions={
                "test_filler": test_filler,
                "save_filler": save_filler,
            },
            max_iters=10,
        )
    except Exception as exc:
        _log.warning("[fill] agentic loop raised for %s: %s", domain, exc)
        loop_result = {"iterations": 0, "result": None}

    _log.info("[fill] agentic loop done for %s: saved=%s iterations=%s",
              domain, bool(saved_paths), loop_result.get("iterations"))

    if saved_paths:
        return _try_cached(domain, form_snapshot)

    if last_good_code:
        save_result = save_filler(domain=domain, code=last_good_code[-1])
        _log.info("[fill] auto-saved filler for %s: %s", domain, save_result)
        return _try_cached(domain, form_snapshot)

    return None


def _looks_valid_filler(code: str, form_snapshot: list[dict]) -> bool:
    """Basic sanity check for generated filler code."""
    if "function fill(" not in code:
        return False
    if len(code) <= 50:
        return False
    return _has_field_reference(code, form_snapshot, include_labels=True)


def _validate_candidate(code: str, form_snapshot: list[dict]) -> dict:
    if "function fill(" not in code:
        return {"valid": False, "error": "Missing function fill(data) signature."}
    if len(code) > 8000:
        return {"valid": False, "error": "Code exceeds 8000 characters."}
    if not _has_field_reference(code, form_snapshot, include_labels=True):
        return {"valid": False, "error": "Code does not reference any current form fields."}
    return {"valid": True, "error": ""}


def _has_field_reference(code: str, form_snapshot: list[dict], *, include_labels: bool) -> bool:
    code_lc = code.lower()
    return any(token in code_lc for token in _reference_tokens(form_snapshot, include_labels=include_labels))


def _reference_tokens(form_snapshot: list[dict], *, include_labels: bool) -> set[str]:
    tokens: set[str] = set()
    for field in form_snapshot or []:
        for key in ("id", "name"):
            value = str(field.get(key, "")).strip().lower()
            if value:
                tokens.add(value)
        if include_labels:
            for key in ("label", "placeholder", "aria_label"):
                value = str(field.get(key, "")).strip().lower()
                if not value:
                    continue
                tokens.add(value)
                for part in re.split(r"[^a-z0-9_]+", value):
                    if len(part) >= 3:
                        tokens.add(part)
    return tokens


def _delete_cached(domain: str) -> None:
    (FILLERS_DIR / f"{_safe_domain(domain)}.js").unlink(missing_ok=True)
