"""
overhired — adaptive job extraction orchestrator

Flow:
  1. Cache hit  → run cached parser immediately (no LLM, <10ms)
  2. Cache miss → agentic loop: LLM writes + tests + saves a parser
  3. Fallback   → one-shot LLM extraction (for models without tool support)
  4. Empty      → return empty dict (user fills manually)

HTML cleaning strategies (applied server-side, ranked by quality score):
  - strip_all      : drop script/style/nav/footer/svg, extract all remaining text
  - content_area   : find main content container first, then strip
  - text_density   : keep only high-density text blocks (Readability-style)
  - raw_innertext  : passthrough (pre-cleaned text sent by extension, no HTML)
"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

from tool_server import PARSERS_DIR, TOOLS, TOOL_FUNCTIONS, _restricted_globals

if TYPE_CHECKING:
    from ai_client import AIClient

_EMPTY = {"title": "", "company": "", "description": "", "location": ""}
_log = logging.getLogger(__name__)

# ── HTML cleaning strategies ──────────────────────────────────────────────────

_DROP_TAGS      = frozenset({
    # JS / CSS
    "script", "style",
    # Document metadata
    "head", "link", "meta", "noscript",
    # Embeds / media that add no text
    "svg", "canvas", "video", "audio", "picture", "source", "track",
    # Invisible / template markup
    "iframe", "template", "object", "embed",
})
_DROP_SECTIONAL = frozenset({"nav", "footer", "aside"})

# Job-signal words — presence in extracted text indicates quality
_JOB_SIGNALS = re.compile(
    r"\b(responsibilit|requirement|qualif|experienc|skill|benefit|salary|"
    r"about us|about the role|who we are|what you.ll|engineer|developer|"
    r"manager|analyst|designer|architect|lead|senior|junior)\b",
    re.IGNORECASE,
)

# Content container selectors tried in order for the content_area strategy
_CONTENT_SELECTORS = [
    r'data-automation="jobAdDetails"',         # SEEK
    r'data-automation-id="jobPostingDescription"',  # Workday
    r'class="[^"]*posting[^"]*"',              # Lever
    r'id="job-details"',
    r'class="[^"]*job-description[^"]*"',
    r'class="[^"]*jobs-description[^"]*"',     # LinkedIn
    r'<article',
    r'<main',
]


class _StripParser(HTMLParser):
    """Extracts visible text, skipping noise subtrees."""

    def __init__(self, drop_sectional: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list[str] = []
        self._drop = _DROP_TAGS | (_DROP_SECTIONAL if drop_sectional else frozenset())

    def handle_starttag(self, tag: str, _attrs: list) -> None:
        if tag.lower() in self._drop:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._drop:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            s = data.strip()
            if s:
                self._parts.append(s)

    def result(self) -> str:
        return re.sub(r"\s{2,}", " ", " ".join(self._parts)).strip()


def _strategy_strip_all(html: str) -> str:
    """Drop script/style/nav/footer/svg, extract all remaining text."""
    p = _StripParser(drop_sectional=True)
    try:
        p.feed(html)
        return p.result()
    except Exception:
        return re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _strategy_content_area(html: str) -> str:
    """Find the main job content container, then strip — tightest focus."""
    for sel in _CONTENT_SELECTORS:
        m = re.search(fr"(<[^>]*{sel}[^>]*>)", html, re.IGNORECASE)
        if not m:
            continue
        start = m.start()
        tag = re.match(r"<(\w+)", m.group(1))
        if not tag:
            continue
        close = f"</{tag.group(1)}"
        end = html.lower().rfind(close.lower(), start)
        if end == -1:
            end = min(start + 60_000, len(html))
        chunk = html[start:end]
        if len(chunk) > 300:
            p = _StripParser(drop_sectional=False)
            try:
                p.feed(chunk)
                text = p.result()
            except Exception:
                text = re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", chunk)).strip()
            if len(text) > 200:
                return text
    return _strategy_strip_all(html)


def _strategy_text_density(html: str) -> str:
    """Keep only block-level elements with high text density (Readability-style).

    Heuristic: compute ratio of text chars to tag chars per block. Blocks above
    threshold are kept; the rest discarded. Effective at removing nav menus and
    sidebars that are mostly links/icons.
    """
    # Split into block-level chunks
    blocks = re.split(r"(?=<(?:div|section|article|p|li|h[1-6])[^>]*>)", html, flags=re.IGNORECASE)
    kept: list[str] = []
    for block in blocks:
        tag_chars  = sum(len(t) for t in re.findall(r"<[^>]+>", block))
        total_chars = len(block)
        text_chars  = total_chars - tag_chars
        if total_chars == 0:
            continue
        density = text_chars / total_chars
        # Keep blocks that are mostly text (>35%) and have some substance (>80 text chars)
        if density > 0.35 and text_chars > 80:
            kept.append(block)

    combined = " ".join(kept) if kept else html
    p = _StripParser(drop_sectional=True)
    try:
        p.feed(combined)
        return p.result()
    except Exception:
        return re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", combined)).strip()


def _quality_score(text: str) -> float:
    """Score extracted text quality for job extraction (higher = better).

    Factors:
    - Signal word density (job-related terms per 1000 chars)
    - Length penalty for being too short or too long
    - Penalise obvious nav noise (excessive short tokens)
    """
    if not text:
        return 0.0
    length = len(text)
    # Length sweet spot: 800–8000 chars
    if length < 200:
        length_factor = length / 200
    elif length > 10_000:
        length_factor = max(0.3, 1 - (length - 10_000) / 10_000)
    else:
        length_factor = 1.0

    signal_count = len(_JOB_SIGNALS.findall(text))
    signal_density = signal_count / max(length / 1000, 0.1)

    # Penalise if average word length is very short (lots of nav/menu fragments)
    words = text.split()
    avg_word_len = sum(len(w) for w in words) / max(len(words), 1)
    word_penalty = min(avg_word_len / 5.0, 1.0)  # ideal avg ~5 chars

    return (signal_density * 2 + length_factor + word_penalty) / 4


_STRATEGIES = {
    "strip_all":     _strategy_strip_all,
    "content_area":  _strategy_content_area,
    "text_density":  _strategy_text_density,
}


def _run_all_strategies(html: str, max_chars: int) -> list[dict]:
    """Run every strategy and return scored results list."""
    results = []
    for name, fn in _STRATEGIES.items():
        t0 = time.perf_counter()
        try:
            text  = fn(html)[:max_chars]
            error = ""
        except Exception as exc:
            text  = ""
            error = str(exc)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        score = _quality_score(text)
        _log.info("[clean_html] %-14s  len=%-6d  score=%.3f  time=%.1fms",
                  name, len(text), score, elapsed_ms)
        results.append({
            "strategy": name,
            "text":     text,
            "length":   len(text),
            "score":    round(score, 4),
            "time_ms":  round(elapsed_ms, 2),
            "error":    error,
            "preview":  text[:300],
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def clean_html(html: str, domain: str = "", max_chars: int = 12_000) -> str:
    """Clean HTML using the best strategy for this domain.

    First visit: benchmarks all strategies, picks winner, logs to DB catalog.
    Subsequent visits: uses historically best strategy directly (fast path).
    Re-benchmarks every 10 runs to catch site changes.
    """
    import tracker
    best_strategy = tracker.get_best_strategy(domain) if domain else None

    if best_strategy and best_strategy in _STRATEGIES:
        # Fast path: use catalog winner directly
        t0 = time.perf_counter()
        try:
            text = _STRATEGIES[best_strategy](html)[:max_chars]
        except Exception:
            text = _strategy_strip_all(html)[:max_chars]
        elapsed = (time.perf_counter() - t0) * 1000
        score = _quality_score(text)
        _log.info("[clean_html] domain=%s catalog=%s score=%.3f time=%.1fms",
                  domain, best_strategy, score, elapsed)
        # Log this run so the catalog stays fresh
        if domain:
            tracker.log_strategy_run(
                domain,
                [{"strategy": best_strategy, "score": score,
                  "length": len(text), "time_ms": elapsed}],
                best_strategy,
            )
        return text

    # Benchmark path: try all strategies, pick best, save to catalog
    results = _run_all_strategies(html, max_chars)
    winner  = results[0] if results else {"strategy": "strip_all", "text": ""}
    _log.info("[clean_html] domain=%s benchmark winner=%s score=%.3f",
              domain, winner["strategy"], winner["score"])
    if domain:
        tracker.log_strategy_run(domain, results, winner["strategy"])
    return winner["text"]


def benchmark_html(html: str, domain: str = "", max_chars: int = 12_000) -> list[dict]:
    """Run all strategies, log to DB catalog, return scored results (sans full text)."""
    results = _run_all_strategies(html, max_chars)
    winner  = results[0]["strategy"] if results else "strip_all"
    if domain:
        import tracker
        tracker.log_strategy_run(domain, results, winner)
    # Strip internal text field before returning to caller
    return [{k: v for k, v in r.items() if k != "text"} for r in results]


# ── Page mode detection ───────────────────────────────────────────────────────

# Known ATS domains always mean application form, not job listing
_ATS_DOMAINS = frozenset({
    "ashbyhq.com", "greenhouse.io", "lever.co",
    "myworkdayjobs.com", "smartrecruiters.com",
    "jobvite.com", "icims.com", "taleo.net",
    "successfactors.com", "bamboohr.com", "recruitee.com",
})

# URL path segments that indicate an application page
_ATS_PATH_RE = re.compile(
    r"/(apply|application|jobs/apply|submit|careers/apply)", re.IGNORECASE
)


def detect_mode(html: str, domain: str, url: str = "") -> str:
    """Return 'ats_form' or 'job_posting' by analysing page HTML + domain.

    Signals checked (in priority order):
    1. Domain is a known ATS platform          -> ats_form
    2. URL path contains /apply or /application -> ats_form
    3. HTML has resume upload + email field     -> ats_form  (strong application signal)
    4. HTML has 2+ labeled form fields          -> ats_form
    5. Otherwise                               -> job_posting
    """
    domain_lower = domain.lower()
    if any(ats in domain_lower for ats in _ATS_DOMAINS):
        _log.info("[detect_mode] domain=%s -> ats_form (known ATS domain)", domain)
        return "ats_form"

    if url and _ATS_PATH_RE.search(url):
        _log.info("[detect_mode] domain=%s -> ats_form (ATS URL path)", domain)
        return "ats_form"

    # Resume upload is a near-certain application form signal
    has_file_input = bool(re.search(r'type=["\']file["\']', html, re.IGNORECASE))
    has_email      = bool(re.search(r'type=["\']email["\']', html, re.IGNORECASE))
    if has_file_input and has_email:
        _log.info("[detect_mode] domain=%s -> ats_form (file+email inputs)", domain)
        return "ats_form"

    # Count labeled/named text-like inputs (ignore search/hidden/checkbox/radio)
    form_inputs = re.findall(
        r'<input[^>]+type=["\'](?:text|email|tel|number|url)["\'][^>]*>',
        html, re.IGNORECASE,
    )
    # Also count textareas (cover letter / additional info)
    textareas = re.findall(r'<textarea', html, re.IGNORECASE)
    application_fields = len(form_inputs) + len(textareas)
    if application_fields >= 4:
        _log.info("[detect_mode] domain=%s -> ats_form (%d form fields)", domain, application_fields)
        return "ats_form"

    _log.info("[detect_mode] domain=%s -> job_posting (fields=%d)", domain, application_fields)
    return "job_posting"


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


def extract(domain: str, page_text: str, ai: "AIClient",
            page_html: str = "") -> dict:
    """Return job info dict for the given domain + page content.

    If page_html is provided it is cleaned server-side (multi-strategy benchmark)
    and used in place of page_text -- keeps the extension simple.
    Tries cached parser first. Falls back to MCP agentic loop, then one-shot LLM.
    """
    if page_html:
        page_text = clean_html(page_html, domain=domain)
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
        result = _oneshot_extract(page_text, ai)
        if result.get("title"):
            # Bootstrap a reusable parser from this successful extraction so
            # future requests for this domain skip the LLM entirely.
            try:
                _bootstrap_parser_from_result(domain, page_text, result, ai)
            except Exception as exc:
                _log.warning("[extract] bootstrap failed for %s: %s", domain, exc)
        return result
    except Exception:
        return dict(_EMPTY)


def _looks_valid_title(title: str, domain: str) -> bool:
    """Heuristic sanity check — returns False if title looks like parser output is stale/broken."""
    if not title or len(title) < 5:
        return False
    if len(title) > 150 or "\n" in title:
        return False
    # Title matches bare domain name or its root (e.g. "SEEK", "LinkedIn", "Indeed")
    domain_root = domain.split(".")[0].lower()
    if title.strip().lower() == domain_root:
        return False
    return True


def _try_cached(domain: str, page_text: str) -> dict | None:
    """Run the cached parser for domain. Returns None on miss or error.

    Self-healing: deletes and returns None if the parser crashes, returns
    empty, or returns a title that fails basic sanity checks (too short,
    matches the domain name, etc.) — triggering the agentic loop to
    regenerate a fresh parser.
    """
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
            _log.warning("[cache] parser for %s has no extract() — deleting", domain)
            path.unlink(missing_ok=True)
            return None
        result = fn(page_text)
        if not isinstance(result, dict):
            _log.warning("[cache] parser for %s returned non-dict — deleting", domain)
            path.unlink(missing_ok=True)
            return None
        title = result.get("title", "")
        if not _looks_valid_title(title, domain):
            _log.warning("[cache] parser for %s returned suspicious title %r — deleting for regeneration",
                         domain, title)
            path.unlink(missing_ok=True)
            return None
        _log.info("[cache] hit for %s — title=%r", domain, title)
        return {k: str(result.get(k, "")) for k in _EMPTY}
    except Exception as exc:
        _log.warning("[cache] parser for %s crashed (%s) — deleting for regeneration", domain, exc)
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


def _bootstrap_parser_from_result(domain: str, page_text: str,
                                   known: dict, ai: "AIClient") -> None:
    """After a successful one-shot extraction, ask the LLM to write a reusable
    parser for this domain using the known-good result as the ground truth.

    This is the fallback path for models that cannot do tool-use, so we keep
    the prompt dead simple and validate before saving.
    """
    from tool_server import save_parser as _save_parser, _safe_domain

    title   = known.get("title", "")
    company = known.get("company", "")
    if not title or not _looks_valid_title(title, domain):
        return  # not worth saving a parser for bad data

    system = (
        "You are a Python code generator. Write a function called `extract(page_text: str) -> dict` "
        "that parses job listing pages from the domain and returns a dict with keys: "
        "title, company, description, location.\n\n"
        "Rules:\n"
        "- Use only the Python standard library (re, html, json — no third-party packages).\n"
        "- Look for the specific patterns visible in the sample page text.\n"
        "- Return empty strings for any field not found.\n"
        "- Output ONLY the Python function, no imports outside the function, no explanation.\n"
        "- Start your response with `def extract(page_text: str) -> dict:`\n\n"
        f"Domain: {domain}\n"
        f"Known-good result for validation:\n"
        f"  title   = {title!r}\n"
        f"  company = {company!r}\n"
    )
    user = f"Sample page text:\n\n{page_text[:8000]}"

    try:
        code = ai.generate(system, user).strip()
        if code.startswith("```"):
            code = code.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if "def extract(" not in code:
            _log.warning("[bootstrap] LLM did not produce a valid extract() function for %s", domain)
            return

        # Validate: run the generated parser against the same page_text
        from tool_server import run_parser as _run_parser
        result = _run_parser(code=code, text=page_text)
        parsed_title = result.get("title", "")
        if not _looks_valid_title(parsed_title, domain):
            _log.warning("[bootstrap] generated parser returned bad title %r for %s — not saving",
                         parsed_title, domain)
            return

        save_result = _save_parser(domain=domain, code=code)
        _log.info("[bootstrap] saved parser for %s via one-shot bootstrap: %s", domain, save_result)
    except Exception as exc:
        _log.warning("[bootstrap] parser bootstrap failed for %s: %s", domain, exc)

