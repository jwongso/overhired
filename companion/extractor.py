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
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from tool_server import PARSERS_DIR, TOOLS, TOOL_FUNCTIONS, _restricted_globals

if TYPE_CHECKING:
    from ai_client import AIClient

_EMPTY = {"title": "", "company": "", "description": "", "location": ""}

_SYSTEM_PROMPT = """\
You are an expert web scraper. Your task is to write a Python function that extracts
job information from job-board page text.

The function signature MUST be:
    def extract(text: str) -> dict:

The dict MUST have exactly these keys: title, company, description, location.
All values must be strings. description should be the main job body (up to 4000 chars).

Available tools:
- run_parser(code, text): test your extract() function. Check the output carefully.
- save_parser(domain, code): save the working parser once run_parser returns a non-empty title.

Workflow:
1. Write an extract() function.
2. Call run_parser to test it against the provided page_text.
3. If title is empty or wrong, revise and test again.
4. Once title is correct, call save_parser with the domain and final code.

IMPORTANT: Only call save_parser when you have confirmed a non-empty, correct title.
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
    except Exception:
        pass  # fall through to one-shot

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
    """Run the tool-use agentic loop. Returns result if save_parser was called."""
    user_prompt = (
        f"Domain: {domain}\n\n"
        f"Page text (first 12000 chars):\n{page_text}"
    )
    loop_result = ai.generate_with_tools(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tools=TOOLS,
        tool_functions=TOOL_FUNCTIONS,
        max_iters=6,
    )
    if loop_result.get("saved") and isinstance(loop_result.get("result"), dict):
        r = loop_result["result"]
        # result here is from save_parser ({"saved": path}), not run_parser
        # Re-run the freshly saved parser to get the actual data
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
