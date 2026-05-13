"""
overhired — adaptive job extraction orchestrator

Flow:
  1. Cache hit  → run cached parser immediately (no LLM, <10ms)
  2. Cache miss → agentic loop: LLM writes + tests + saves a parser
  3. Fallback   → one-shot LLM extraction (for models without tool support)
  4. Empty      → return empty dict (user fills manually)
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from tool_server import PARSERS_DIR, TOOLS, TOOL_FUNCTIONS, _restricted_globals

if TYPE_CHECKING:
    from ai_client import AIClient

_EMPTY = {"title": "", "company": "", "description": "", "location": ""}
_log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert web scraper. Your task is to write a Python function that extracts
job information from job-board page text.

The function signature MUST be:
    def extract(text: str) -> dict:

The dict MUST have exactly these keys: title, company, description, location.
All values must be strings. description should be the main job body (up to 4000 chars).

Available tools:
- run_parser(code, text): test your extract() function against the provided text.
- save_parser(domain, code): persist the working parser once run_parser confirms a title.

Workflow:
1. Write an extract() function.
2. Call run_parser to test it. If title is empty or wrong, revise and call run_parser again.
3. As soon as run_parser returns a non-empty title, you MUST call save_parser immediately.

RULES:
- After a successful run_parser (non-empty title), your ONLY next action is to call save_parser.
- Do NOT explain, summarise, or write any text after run_parser succeeds. Call save_parser.
- Do NOT skip save_parser. The task is not complete until save_parser has been called.
"""


def extract(domain: str, page_text: str, ai: "AIClient") -> dict:
    """Return job info dict for the given domain + page text.

    Tries cached parser first. Falls back to MCP agentic loop.
    Falls back further to one-shot LLM extraction on tool-use failure.
    """
    page_text = page_text[:12000]

    # ── 1. Cache hit ─────────────────────────────────────────────────────────
    result = _try_cached(domain, page_text)
    if result:
        return result

    # ── 2. Agentic loop ───────────────────────────────────────────────────────
    try:
        result = _agentic_extract(domain, page_text, ai)
        if result and result.get("title"):
            return result
    except Exception as exc:
        _log.warning("[extract] agentic loop failed for %s: %s", domain, exc)

    # ── 3. One-shot fallback (models without tool support) ────────────────────
    try:
        return _oneshot_extract(page_text, ai)
    except Exception:
        return dict(_EMPTY)


def _try_cached(domain: str, page_text: str) -> dict | None:
    """Run the cached parser for domain. Returns None on miss or error."""
    from tool_server import _safe_domain
    path = PARSERS_DIR / f"{_safe_domain(domain)}.py"
    if not path.exists():
        return None
    try:
        code = path.read_text(encoding="utf-8")
        ns = _restricted_globals()
        exec(compile(code, str(path), "exec"), ns)  # noqa: S102
        fn = ns.get("extract")
        if not callable(fn):
            return None
        result = fn(page_text)
        if isinstance(result, dict) and result.get("title"):
            return {k: str(result.get(k, "")) for k in _EMPTY}
        # Broken — delete so it regenerates
        path.unlink(missing_ok=True)
        return None
    except Exception:
        path.unlink(missing_ok=True)
        return None


def _agentic_extract(domain: str, page_text: str, ai: "AIClient") -> dict | None:
    """Run the tool-use agentic loop. Returns result if a parser was saved.

    Auto-save strategy: if the LLM calls run_parser and gets a valid title but
    then forgets to call save_parser (common with instruction-following models),
    we capture the last successfully-tested code and save it ourselves.
    """
    from tool_server import save_parser as _save_parser
    log = _log

    # Track the last code that run_parser validated successfully.
    _last_good_code: list[str] = []

    original_run_parser = TOOL_FUNCTIONS["run_parser"]

    def _tracked_run_parser(code: str, text: str) -> dict:
        result = original_run_parser(code=code, text=text)
        title = result.get("title", "")
        # Accept only clean titles: non-empty, short, no newlines
        if title and len(title) <= 150 and "\n" not in title:
            _last_good_code.clear()
            _last_good_code.append(code)
            log.info("[extract] run_parser validated — title=%r", title)
        elif title:
            log.info("[extract] run_parser title looks wrong (len=%d, newlines=%s) — skipping",
                     len(title), "\n" in title)
        return result

    custom_fns = {**TOOL_FUNCTIONS, "run_parser": _tracked_run_parser}

    user_prompt = (
        f"Domain: {domain}\n\n"
        f"Page text (first 12000 chars):\n{page_text}"
    )
    try:
        loop_result = ai.generate_with_tools(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tools=TOOLS,
            tool_functions=custom_fns,
            max_iters=10,
        )
    except Exception as exc:
        log.warning("[extract] agentic loop raised: %s", exc)
        loop_result = {"saved": False, "iterations": 0, "result": None}

    log.info("[extract] agentic loop done: saved=%s iterations=%s",
             loop_result.get("saved"), loop_result.get("iterations"))

    # Happy path: LLM called save_parser itself.
    if loop_result.get("saved"):
        return _try_cached(domain, page_text)

    # Recovery: LLM validated code but forgot to call save_parser — do it ourselves.
    if _last_good_code:
        save_result = _save_parser(domain=domain, code=_last_good_code[-1])
        log.info("[extract] auto-saved parser for %s: %s", domain, save_result)
        return _try_cached(domain, page_text)

    return None


def _oneshot_extract(page_text: str, ai: "AIClient") -> dict:
    """One-shot extraction without tools — returns JSON from LLM."""
    system = (
        "You are a job listing parser. Extract job info from the given page text. "
        "Reply with ONLY a JSON object with keys: title, company, description, location. "
        "No markdown, no explanation."
    )
    user = f"Extract job info from this page text:\n\n{page_text}"
    raw = ai.generate(system, user)
    # Strip markdown fences if model wraps with ```json
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(raw)
        return {k: str(data.get(k, "")) for k in _EMPTY}
    except json.JSONDecodeError:
        return dict(_EMPTY)
