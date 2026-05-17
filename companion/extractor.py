"""
grapply — adaptive job extraction orchestrator

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

import ast
import json
import logging
import re
import threading as _threading
import time
import traceback
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

from tool_server import PARSERS_DIR, _restricted_globals

if TYPE_CHECKING:
    from ai_client import AIClient

_EMPTY = {"title": "", "company": "", "description": "", "location": ""}

# Per-domain lock to prevent duplicate parser generation and cache race conditions.
_domain_locks: dict[str, _threading.Lock] = {}
_domain_locks_guard = _threading.Lock()
_log = logging.getLogger(__name__)


def _get_domain_lock(domain: str) -> _threading.Lock:
    """Return a per-domain lock, creating one atomically if needed."""
    with _domain_locks_guard:
        if domain not in _domain_locks:
            _domain_locks[domain] = _threading.Lock()
        return _domain_locks[domain]

# ── HTML cleaning strategies ──────────────────────────────────────────────────

_DROP_TAGS      = frozenset({
    # JS / CSS
    "script", "style",
    # Document metadata (head itself is dropped; meta/link are void - see _VOID_TAGS)
    "head", "noscript",
    # Embeds / media that add no text
    "svg", "canvas", "video", "audio", "picture",
    # Invisible / template markup
    "iframe", "template", "object", "embed",
})
_DROP_SECTIONAL = frozenset({"nav", "footer", "aside"})

# Void elements never have a closing tag. If they appear in _DROP_TAGS they would
# increment _skip without ever decrementing it, causing the entire rest of the
# document to be silently dropped. Keep them out of _DROP_TAGS and handle them
# here: skip their content by simply ignoring them in handle_starttag (they have
# no text content anyway).
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# Job-signal words — presence in extracted text indicates quality
_JOB_SIGNALS = re.compile(
    r"\b(responsibilit|requirement|qualif|experienc|skill|benefit|salary|"
    r"about us|about the role|who we are|what you.ll|engineer|developer|"
    r"manager|analyst|designer|architect|lead|senior|junior)\b",
    re.IGNORECASE,
)

# Content container selectors tried in order for the content_area strategy
_CONTENT_SELECTORS = [
    r'data-automation="jobDetailsPage"',        # SEEK (full job page: title + company + description)
    r'data-automation="jobAdDetails"',          # SEEK (description-only fallback)
    r'data-automation-id="jobPostingDescription"',  # Workday
    r'class="[^"]*posting[^"]*"',              # Lever
    r'id="job-details"',
    r'class="[^"]*job-description[^"]*"',
    r'class="[^"]*jobs-description[^"]*"',     # LinkedIn
    r'<article',
    r'<main',
]


_BLOCK_TAGS = frozenset({
    "div", "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "tr", "td", "section", "article", "header", "footer",
    "main", "aside", "blockquote", "pre", "br", "hr",
    "dl", "dt", "dd", "ol", "ul", "table", "thead", "tbody",
})


class _StripParser(HTMLParser):
    """Extracts visible text, skipping noise subtrees."""

    def __init__(self, drop_sectional: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list[str] = []
        self._drop = _DROP_TAGS | (_DROP_SECTIONAL if drop_sectional else frozenset())

    def handle_starttag(self, tag: str, _attrs: list) -> None:
        t = tag.lower()
        if t in _VOID_TAGS:
            return  # void elements have no closing tag - must not touch _skip
        if t in self._drop:
            self._skip += 1
        elif t in _BLOCK_TAGS and not self._skip:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._drop:
            self._skip = max(0, self._skip - 1)
        elif t in _BLOCK_TAGS and not self._skip:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            s = data.strip()
            if s:
                self._parts.append(s)

    def result(self) -> str:
        text = " ".join(self._parts)
        # Collapse spaces around newlines, then collapse runs of blank lines
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return re.sub(r" {2,}", " ", text).strip()


_NOISE_TAGS = re.compile(
    r"<(script|style|svg|noscript|iframe|link|meta|head)[^>]*>.*?</\1>|"
    r"<(script|style|svg|noscript|iframe|link|meta)[^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_html_for_llm(html: str, max_chars: int = 15_000) -> str:
    """Remove script/style/svg noise but keep HTML structure so the LLM
    can see tags like <h1>, data-automation attributes, <p>, etc.
    Truncated to max_chars to stay within LLM context limits.
    """
    stripped = _NOISE_TAGS.sub("", html)
    # Collapse excessive whitespace between tags
    stripped = re.sub(r">\s{2,}<", ">\n<", stripped)
    stripped = re.sub(r"\s{3,}", " ", stripped)
    return stripped[:max_chars]


# ── Semantic HTML skeleton ────────────────────────────────────────────────────

# HTML5 void elements — no closing tag, must never affect _skip counter
_HTML_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
    # SVG path elements that are commonly self-closing in practice
    "path", "circle", "ellipse", "line", "polygon", "polyline", "rect", "use",
})

# Tags whose attributes/classes are useful for parser writers
_SKELETON_KEEP_TAGS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6",
    "title", "article", "section", "main", "header",
    "div", "span", "p", "li", "td", "th",
    "a", "time", "address", "label",
})
# Tags we drop entirely (no useful text or structure)
_SKELETON_DROP_TAGS = frozenset({
    "script", "style", "svg", "noscript", "iframe",
    "link", "meta",  # keep <head> and <title> so page title is visible
    "picture", "source",
    "video", "audio", "canvas", "object", "embed", "template",
})
# Attrs to keep on skeleton elements (everything else stripped)
_SKELETON_KEEP_ATTRS = frozenset({
    "id", "class", "data-automation", "data-automation-id",
    "data-job-id", "itemprop", "itemtype",
    "href",  # keep on <a> so domain is visible
})

_TEXT_SNIPPET_LEN = 120   # chars of text to preserve per element


class _SkeletonBuilder(HTMLParser):
    """Produce a compact, human-readable DOM skeleton.

    Each element that contains meaningful text is rendered as:
        <tag class="..." id="...">text snippet...</tag>

    - Script/style/noise subtrees are dropped entirely.
    - Deeply nested empty elements are suppressed.
    - Output is ASCII-safe (non-ASCII chars kept as-is) and stays under
      max_chars characters.

    Example output for a LinkedIn job page (~40 lines, ~2 KB):
        <title>Senior C++ Engineer | EA SPORTS | LinkedIn</title>
        <h1 class="top-card-layout__title">Senior C++ Generalist Software Engineer</h1>
        <span class="topcard__org-name-link">EA SPORTS</span>
        <span class="topcard__flavor--bullet">Vancouver, BC</span>
        <div class="show-more-less-html__markup">Description & Requirements...</div>
    """

    def __init__(self, max_chars: int = 25_000) -> None:
        super().__init__(convert_charrefs=True)
        self._skip     = 0          # nesting depth inside dropped subtrees
        self._buf: list[str] = []
        self._max      = max_chars
        self._total    = 0
        self._tag_stack: list[tuple[str, str]] = []   # (tag, rendered_open_tag)
        self._cur_text : list[str] = []               # text accumulator for current element
        self._done     = False

    def _filtered_attrs(self, attrs: list) -> str:
        kept = [(k, v) for k, v in attrs
                if k in _SKELETON_KEEP_ATTRS and v and v.strip()]
        if not kept:
            return ""
        return " " + " ".join(f'{k}="{v}"' for k, v in kept[:4])  # cap at 4 attrs

    def _flush_element(self) -> None:
        """Write accumulated text for the current open element."""
        if not self._tag_stack:
            return
        tag, open_tag = self._tag_stack[-1]
        if tag not in _SKELETON_KEEP_TAGS:
            return
        text = " ".join(self._cur_text).strip()
        if not text:
            return
        snippet = text[:_TEXT_SNIPPET_LEN]
        if len(text) > _TEXT_SNIPPET_LEN:
            snippet += "…"
        line = f"{open_tag}{snippet}</{tag}>\n"
        if self._total + len(line) > self._max:
            self._done = True
            return
        self._buf.append(line)
        self._total += len(line)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._done:
            return
        t = tag.lower()
        # Void elements never have a closing tag — skip them entirely to avoid
        # corrupting the _skip counter (they'd increment without a matching decrement)
        if t in _HTML_VOID:
            return
        if t in _SKELETON_DROP_TAGS or self._skip > 0:
            self._skip += 1
            return
        attr_str = self._filtered_attrs(attrs)
        open_tag = f"<{t}{attr_str}>"
        self._tag_stack.append((t, open_tag))
        self._cur_text = []

    def handle_endtag(self, tag: str) -> None:
        if self._done:
            return
        t = tag.lower()
        if t in _HTML_VOID:
            return  # void elements have no closing tag — ignore stray ones
        if self._skip > 0:
            # Decrement for ANY non-void closing tag (not just DROP tags)
            # so we correctly exit nested drop subtrees
            self._skip -= 1
            return
        if self._tag_stack and self._tag_stack[-1][0] == t:
            self._flush_element()
            self._tag_stack.pop()
            self._cur_text = []

    def handle_data(self, data: str) -> None:
        if self._done or self._skip > 0:
            return
        s = data.strip()
        if s and len(s) > 1:  # skip single chars like punctuation noise
            self._cur_text.append(s)

    def result(self) -> str:
        return "".join(self._buf)


def html_skeleton(html: str, max_chars: int = 25_000) -> str:
    """Return a compact semantic skeleton of the HTML page.

    The skeleton preserves tag names, meaningful CSS classes/IDs, and short
    text snippets.  Noise (scripts, styles, SVG, empty elements) is removed.

    This is used as input for LLM-based parser generation:
      - Smaller than raw HTML (2.7MB LinkedIn → ~15–30KB)
      - Preserves enough structure to write CSS-selector-style parsers
      - Gives the LLM class names it can use in regex patterns

    Returns empty string if parsing fails (caller falls back to clean text).
    """
    try:
        builder = _SkeletonBuilder(max_chars=max_chars)
        builder.feed(html)
        result = builder.result()
        _log.debug("[skeleton] %d raw chars → %d skeleton chars", len(html), len(result))
        return result
    except Exception as exc:
        _log.warning("[skeleton] failed: %s", exc)
        return ""

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


def _format_job_text(title: str, company: str, location: str, desc: str) -> str:
    """Produce a consistent plain-text layout used by every structured strategy.

    Format (parsers can rely on this):
        {title}
        {company}
        {location}          ← omitted if empty

        Job description:
        {description}
    """
    parts: list[str] = []
    if title:    parts.append(title)
    if company:  parts.append(company)
    if location: parts.append(location)
    parts.append("")
    parts.append("Job description:")
    if desc:     parts.append(desc)
    return "\n".join(parts)


def _strategy_seek(html: str) -> str:
    """Extract job data from SEEK's inline JavaScript data blob.

    SEEK embeds structured job data as a JSON object in a <script> tag
    containing both 'jobTitle' and 'advertiserName'.
    """
    import json as _json
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for s in scripts:
        if '"jobTitle"' not in s or '"advertiserName"' not in s:
            continue

        title_m   = re.search(r'"jobTitle"\s*:\s*"([^"]+)"', s)
        company_m = re.search(r'"advertiserName"\s*:\s*"([^"]+)"', s)
        if not (title_m and company_m):
            continue

        title   = title_m.group(1).strip()
        company = company_m.group(1).strip()

        loc_m    = re.search(r'"location"\s*:\s*"([^"]+)"', s)
        location = loc_m.group(1).strip() if loc_m else ""

        # Description: pick the longest decodable content/content2 field
        desc = ""
        for key_pat in (r'"content2?"', r'"content"', r'"content2"'):
            for raw_val in re.findall(
                key_pat + r'\s*:\s*("(?:[^"\\]|\\.)*")', s
            ):
                try:
                    val = _json.loads(raw_val)
                except _json.JSONDecodeError:
                    continue
                # SEEK sometimes uses Unicode escapes for HTML tags
                if "\\u003C" in raw_val:
                    val = val.encode().decode("unicode_escape",
                                              errors="replace")
                if ("<p>" in val or "<br>" in val or "\\u003C" in val
                        or "Job description" in val):
                    cleaned = re.sub(r"<[^>]+>", " ", val)
                    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
                    if len(cleaned) > len(desc):
                        desc = cleaned

        if not title:
            continue
        return _format_job_text(title, company, location, desc)
    return ""


def _strategy_jsonld(html: str) -> str:
    """Extract job data from JSON-LD <script type="application/ld+json"> blocks.

    Works for Indeed and any site that embeds a JobPosting schema.
    """
    import json as _json
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for raw in blocks:
        try:
            d = _json.loads(raw.strip())
        except _json.JSONDecodeError:
            continue
        if d.get("@type") != "JobPosting":
            continue

        title   = d.get("title", "").strip()
        company = (d.get("hiringOrganization") or {}).get("name", "").strip()
        loc_obj = d.get("jobLocation") or {}
        addr    = loc_obj.get("address") or {} if isinstance(loc_obj, dict) else {}
        location = (addr.get("addressLocality") or addr.get("addressRegion") or
                    addr.get("addressCountry") or "").strip()
        desc_html = d.get("description", "")
        desc = re.sub(r"<[^>]+>", " ", desc_html)
        desc = re.sub(r"\s{2,}", " ", desc).strip()

        if not title:
            continue
        return _format_job_text(title, company, location, desc)
    return ""


def _strategy_linkedin(html: str) -> str:
    """Extract job data from a LinkedIn job page saved as HTML.

    LinkedIn stores title and company in the <title> tag as
    'Title | Company | LinkedIn', and the description after
    'About the job'.
    """
    import html as _html_mod

    # Title and company from <title> tag (use the first job-specific one)
    titles = re.findall(r'<title>([^<]+)</title>', html)
    job_title_tag = next(
        (t for t in titles
         if "LinkedIn" in t and t.count("|") >= 2),
        None,
    )
    if not job_title_tag:
        return ""
    parts = [p.strip() for p in job_title_tag.split("|")]
    # LinkedIn title format: "Title - DivisionName | Company | LinkedIn"
    # Strip trailing "- DivisionGroup" suffix from the title part if present
    raw_title = parts[0]
    title_parts = raw_title.split(" - ")
    title   = title_parts[0].strip()
    company = parts[1] if len(parts) > 1 else ""

    # Location: may be in a JSON state blob
    loc_m = re.search(r'"formattedLocation"\s*:\s*"([^"]+)"', html)
    location = loc_m.group(1).strip() if loc_m else ""

    # Description: everything from "About the job" onward, HTML entities decoded
    for marker in ("About the job", "About this role", "Job description"):
        pos = html.find(marker)
        if pos >= 0:
            chunk = html[pos: pos + 12_000]
            desc = re.sub(r"<[^>]+>", " ", chunk)
            desc = _html_mod.unescape(desc)          # decode &amp; &lt; etc.
            desc = re.sub(r"\s{2,}", " ", desc).strip()
            desc = re.sub(rf"^{re.escape(marker)}\s*", "", desc).strip()
            break
    else:
        desc = ""

    if not title:
        return ""
    return _format_job_text(title, company, location, desc)


_STRATEGIES = {
    "seek":          _strategy_seek,
    "jsonld":        _strategy_jsonld,
    "linkedin":      _strategy_linkedin,
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


# Domains with a dedicated structured extraction strategy.
# These always produce a stable _format_job_text 4-line output so parsers
# trained on any page from that domain reliably generalise to all others.
# Never let the catalog override these with a content_area / text_density winner.
_DOMAIN_PINNED_STRATEGY: dict[str, str] = {
    "nz.seek.com":     "seek",
    "seek.com.au":     "seek",
    "nz.indeed.com":   "jsonld",
    "au.indeed.com":   "jsonld",
    "indeed.com":      "jsonld",
    "www.linkedin.com":"linkedin",
    "linkedin.com":    "linkedin",
}


def clean_html(html: str, domain: str = "", max_chars: int = 12_000) -> str:
    """Clean HTML using the best strategy for this domain.

    Priority:
      1. Pinned strategy  — domains with a dedicated structured extractor always
                            use it so the output format is stable across pages.
      2. Catalog winner   — historically best strategy from tracker DB (fast path).
      3. Benchmark        — run all strategies, pick winner, save to catalog.
    """
    if len(html) < 100:
        return html.strip()

    import tracker

    # ── 1. Pinned strategy (structured domains) ───────────────────────────────
    pinned = _DOMAIN_PINNED_STRATEGY.get(domain.lower())
    if pinned and pinned in _STRATEGIES:
        t0 = time.perf_counter()
        try:
            text = _STRATEGIES[pinned](html)[:max_chars]
        except Exception:
            text = ""
        elapsed = (time.perf_counter() - t0) * 1000
        if text:
            score = _quality_score(text)
            _log.info("[clean_html] domain=%s pinned=%s score=%.3f time=%.1fms",
                      domain, pinned, score, elapsed)
            if domain:
                tracker.log_strategy_run(
                    domain,
                    [{"strategy": pinned, "score": score,
                      "length": len(text), "time_ms": elapsed}],
                    pinned,
                )
            return text
        _log.warning("[clean_html] pinned strategy %s returned empty for %s — falling through",
                     pinned, domain)

    # ── 2. Catalog winner (fast path) ─────────────────────────────────────────
    best_strategy = tracker.get_best_strategy(domain) if domain else None
    if best_strategy and best_strategy in _STRATEGIES:
        t0 = time.perf_counter()
        try:
            text = _STRATEGIES[best_strategy](html)[:max_chars]
        except Exception:
            text = _strategy_strip_all(html)[:max_chars]
        elapsed = (time.perf_counter() - t0) * 1000
        score = _quality_score(text)
        _log.info("[clean_html] domain=%s catalog=%s score=%.3f time=%.1fms",
                  domain, best_strategy, score, elapsed)
        if domain:
            tracker.log_strategy_run(
                domain,
                [{"strategy": best_strategy, "score": score,
                  "length": len(text), "time_ms": elapsed}],
                best_strategy,
            )
        return text

    # ── 3. Benchmark (first visit / unknown domain) ───────────────────────────
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

# Known job listing boards - these always show job postings; only switch to ATS
# if the URL explicitly contains an apply path segment.
_JOB_BOARD_DOMAINS = frozenset({
    "amazon.jobs", "linkedin.com", "indeed.com", "seek.com",
    "nz.seek.com", "au.seek.com", "glassdoor.com", "glassdoor.co.nz",
    "seek.co.nz", "jobs.apple.com", "careers.google.com",
    "microsoft.com", "careers.microsoft.com",
})

# URL path segments that indicate an application page
_ATS_PATH_RE = re.compile(
    r"/(apply|application|jobs/apply|submit|careers/apply)", re.IGNORECASE
)

# File inputs that accept resume/document types (strong application signal)
_RESUME_ACCEPT_RE = re.compile(
    r'type=["\']file["\'][^>]*accept=["\'][^"\']*\.(pdf|doc|docx|rtf)',
    re.IGNORECASE,
)
_RESUME_ACCEPT_RE2 = re.compile(
    r'accept=["\'][^"\']*\.(pdf|doc|docx|rtf)[^"\']*["\'][^>]*type=["\']file["\']',
    re.IGNORECASE,
)


def detect_mode(html: str, domain: str, url: str = "") -> str:
    """Return 'ats_form' or 'job_posting' by analysing page HTML + domain.

    Signals checked (in priority order):
    1. Domain is a known ATS platform              -> ats_form
    2. URL path contains /apply or /application    -> ats_form
    3. Known job board domain without apply URL    -> job_posting (skip heuristics)
    4. HTML has resume upload + email field        -> ats_form  (strong application signal)
    5. HTML has 6+ labeled form fields             -> ats_form
    6. Otherwise                                   -> job_posting
    """
    domain_lower = domain.lower()
    if any(ats in domain_lower for ats in _ATS_DOMAINS):
        _log.info("[detect_mode] domain=%s -> ats_form (known ATS domain)", domain)
        return "ats_form"

    if url and _ATS_PATH_RE.search(url):
        _log.info("[detect_mode] domain=%s -> ats_form (ATS URL path)", domain)
        return "ats_form"

    # Known job boards: listing pages have search/alert forms - skip HTML heuristics
    if any(board in domain_lower for board in _JOB_BOARD_DOMAINS):
        _log.info("[detect_mode] domain=%s -> job_posting (known job board)", domain)
        return "job_posting"

    # Resume-specific file upload + email = application form (not just any file input)
    has_resume_upload = bool(
        _RESUME_ACCEPT_RE.search(html) or _RESUME_ACCEPT_RE2.search(html)
    )
    has_email = bool(re.search(r'type=["\']email["\']', html, re.IGNORECASE))
    if has_resume_upload and has_email:
        _log.info("[detect_mode] domain=%s -> ats_form (resume+email inputs)", domain)
        return "ats_form"

    # Count labeled/named text-like inputs (ignore search/hidden/checkbox/radio)
    form_inputs = re.findall(
        r'<input[^>]+type=["\'](?:text|email|tel|number|url)["\'][^>]*>',
        html, re.IGNORECASE,
    )
    # Also count textareas (cover letter / additional info)
    textareas = re.findall(r'<textarea', html, re.IGNORECASE)
    application_fields = len(form_inputs) + len(textareas)
    if application_fields >= 6:
        _log.info("[detect_mode] domain=%s -> ats_form (%d form fields)", domain, application_fields)
        return "ats_form"

    _log.info("[detect_mode] domain=%s -> job_posting (fields=%d)", domain, application_fields)
    return "job_posting"


# ── Phase 1: Extraction-only prompt ──────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are extracting structured data from a job listing page. No explanation. No code.

Respond with EXACTLY one JSON object and nothing else:

{{"title": "<job title>", "company": "<company name>", "location": "<city/region or empty string>", "description": "<full job description, up to 4000 chars>"}}

Rules:
- Output ONLY the JSON object — no markdown fences, no preamble, no trailing text
- All four keys are required; use "" for any field not found
- Do NOT use placeholder text like "Job Title" or "Company Name"
- description should be the actual job requirements/responsibilities text
"""

# ── Phase 2: Parser-generation prompt ────────────────────────────────────────

_PARSER_SYSTEM = """\
You are a Python code generator writing a reusable job-listing parser for {domain}.

The CORRECT extraction result for the SAMPLE page is:
  title       = {title!r}
  company     = {company!r}
  location    = {location!r}

These values are shown so you know WHAT to find, NOT so you copy them into the code.
{format_hint}
Your task: write a Python function that finds the values BY POSITION/STRUCTURE
in the text — NEVER by searching for the specific strings shown above.

Output ONLY a ```python code block containing:
  import re  (and any other stdlib imports you need: html, json, string)
  def extract(text: str) -> dict:
      ...
      return {{"title": title, "company": company, "location": location, "description": description}}

CRITICAL RULES (violations will cause test failure):
1. Use ONLY Python standard library (re, html, json, string) — NO pip packages
2. Every field defaults to "" — never raise exceptions
3. NEVER use string literals from title/company/location as search targets in your code
   BAD:  company = next(l for l in lines if "Fisher & Paykel" in l, "")
   GOOD: company = lines[1] if len(lines) > 1 else ""
4. Generalise to ANY {domain} job listing in the same format, not just this page
5. Return ONLY the ```python block — no explanation before or after
"""

# ── Phase 3: Parser fix prompt ────────────────────────────────────────────────

_FIX_SYSTEM = """\
The Python parser below is WRONG. Fix it so it extracts the correct values.

Expected:
  title    = {title!r}
  company  = {company!r}
  location = {location!r}

Got:
  title    = {got_title!r}
  company  = {got_company!r}
  location = {got_location!r}

The parser runs on plain text from {domain} job listing pages.
Output ONLY the corrected ```python code block. No explanation.
"""

_PLACEHOLDERS = frozenset({
    "your title here", "title here", "job title", "<job title>",
    "your company name", "company name", "<company>", "company here",
})


def _strip_think(raw: str) -> str:
    """Remove <think>...</think> blocks, logging them so they're visible in companion.log."""
    def _log_and_drop(m: re.Match) -> str:
        block = m.group(0)
        # Log first 2000 chars of the thinking block so it's visible in logs
        _log.info("[think] %s…" if len(block) > 2000 else "[think] %s", block[:2000])
        return ""
    return re.sub(r"<think>.*?</think>", _log_and_drop, raw, flags=re.DOTALL).strip()


def _parse_json_result(raw: str) -> dict:
    """Try multiple strategies to extract a JSON job-info dict from LLM output."""
    # Strategy 1: fenced ```json block
    m = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            return {k: str(data.get(k, "")) for k in _EMPTY}
        except json.JSONDecodeError:
            pass

    # Strategy 2: bare JSON object anywhere in response
    m = re.search(r"\{[^{}]*?\"title\"\s*:\s*\"[^\"]+?\"[^{}]*?\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return {k: str(data.get(k, "")) for k in _EMPTY}
        except json.JSONDecodeError:
            pass

    # Strategy 3: any JSON object that has all four keys
    for m in re.finditer(r"\{[^{}]{20,}\}", raw, re.DOTALL):
        try:
            data = json.loads(m.group(0))
            if all(k in data for k in _EMPTY):
                return {k: str(data.get(k, "")) for k in _EMPTY}
        except json.JSONDecodeError:
            continue

    return dict(_EMPTY)


def _parse_python_block(raw: str) -> str | None:
    """Extract and normalise the Python parser code from an LLM response."""
    m = re.search(r"```(?:python)?\s*(.*?)```", raw, re.DOTALL)
    if not m:
        return None
    block = m.group(1).strip()
    # Normalise variant function names → def extract(
    block = re.sub(r"\bdef extract_\w+\s*\(", "def extract(", block)
    if "def extract" not in block:
        return None
    return block


# ── Phase 1 — extraction only ─────────────────────────────────────────────────

def _phase1_extract(domain: str, page_text: str, ai: "AIClient") -> dict:
    """LLM call A: extract job JSON only.  Returns _EMPTY on failure."""
    user = f"Job listing text from {domain}:\n\n{page_text}"
    _log.info("[phase1] extracting from %s (%d chars)", domain, len(page_text))
    t0 = time.monotonic()
    try:
        raw = ai.generate(_EXTRACT_SYSTEM, user, timeout=ai.tool_timeout, _endpoint="extract")
    except Exception as exc:
        _log.warning("[phase1] LLM call failed: %s", exc)
        return dict(_EMPTY)
    elapsed = time.monotonic() - t0
    _log.info("[phase1] replied in %.1fs", elapsed)
    raw = _strip_think(raw)
    _log.info("[phase1] raw (first 1000):\n%s", raw[:1000])
    result = _parse_json_result(raw)
    if result.get("title", "").lower() in _PLACEHOLDERS:
        _log.warning("[phase1] placeholder title %r — discarding", result["title"])
        return dict(_EMPTY)
    _log.info("[phase1] title=%r  company=%r  location=%r",
              result["title"], result["company"], result["location"])
    return result


# ── Phase 2 — parser generation ───────────────────────────────────────────────

_PINNED_FORMAT_HINT = """\

IMPORTANT FORMAT for {domain}: the plain text passed to extract() always has this structure:
  line 0: job title
  line 1: company name
  line 2: location (may be empty or on the same line as company)
  ...rest: job description body (after a "Job description:" header)
Use positional extraction (lines[0], lines[1], etc.) — it is always reliable for this domain.
"""


def _phase2_generate(domain: str, page_text: str, confirmed: dict,
                     ai: "AIClient") -> str | None:
    """LLM call B: generate Python parser knowing the confirmed values.

    The confirmed extraction result is given as ground truth so the LLM knows
    exactly what to search for in the text, enabling it to write reliable patterns.
    """
    format_hint = ""
    if domain.lower() in _DOMAIN_PINNED_STRATEGY:
        format_hint = _PINNED_FORMAT_HINT.format(domain=domain)

    system = _PARSER_SYSTEM.format(
        domain=domain,
        title=confirmed.get("title", ""),
        company=confirmed.get("company", ""),
        location=confirmed.get("location", ""),
        format_hint=format_hint,
    )
    user = f"Sample page text for {domain}:\n\n{page_text}"
    _log.info("[phase2] generating parser for %s (title=%r)", domain, confirmed.get("title"))
    t0 = time.monotonic()
    try:
        raw = ai.generate(system, user, timeout=ai.tool_timeout, _endpoint="extract")
    except Exception as exc:
        _log.warning("[phase2] LLM call failed: %s", exc)
        return None
    elapsed = time.monotonic() - t0
    _log.info("[phase2] replied in %.1fs", elapsed)
    raw = _strip_think(raw)
    _log.info("[phase2] raw (first 1500):\n%s", raw[:1500])
    code = _parse_python_block(raw)
    if code:
        _log.info("[phase2] parser ✓  (%d chars)", len(code))
    else:
        _log.warning("[phase2] no valid def extract found in response")
    return code


# ── Phase 3 — validate + retry loop ──────────────────────────────────────────

def _phase3_validate_and_fix(domain: str, page_text: str, confirmed: dict,
                              parser_code: str, ai: "AIClient",
                              max_retries: int = 3) -> tuple[str | None, dict]:
    """Run the parser, compare to confirmed values, ask LLM to fix if wrong.

    Returns (final_parser_code, final_result).
    final_parser_code is None if validation ultimately fails.
    """
    from tool_server import run_parser as _run_parser

    exp_title   = confirmed.get("title", "")
    exp_company = confirmed.get("company", "")
    exp_loc     = confirmed.get("location", "")

    for attempt in range(1, max_retries + 2):  # +1 for the first validate-only pass
        try:
            got = _run_parser(code=parser_code, text=page_text)
        except Exception as exc:
            got = {"title": "", "company": "", "location": "", "description": "",
                   "error": str(exc)}
        got_title   = got.get("title", "")
        got_company = got.get("company", "")
        got_loc     = got.get("location", "")
        _log.info("[phase3] attempt %d — title=%r  company=%r  location=%r",
                  attempt, got_title, got_company, got_loc)

        # Accept if title matches (case-insensitive substring is enough)
        title_ok   = exp_title.lower() in got_title.lower() or got_title.lower() in exp_title.lower()
        company_ok = (not exp_company) or (exp_company.lower() in got_company.lower()
                                           or got_company.lower() in exp_company.lower())
        if title_ok and company_ok:
            _log.info("[phase3] ✓ validated after %d attempt(s)", attempt)
            return parser_code, {k: str(got.get(k, "")) for k in _EMPTY}

        if attempt > max_retries:
            _log.warning("[phase3] exhausted %d retries — keeping last parser anyway (title wrong)",
                         max_retries)
            # Still return it — _try_save_parser will run its own sanity check
            return parser_code, {k: str(got.get(k, "")) for k in _EMPTY}

        # Ask LLM to fix it
        _log.info("[phase3] parser wrong — fix attempt %d/%d", attempt, max_retries)
        fix_system = _FIX_SYSTEM.format(
            domain=domain,
            title=exp_title, company=exp_company, location=exp_loc,
            got_title=got_title, got_company=got_company, got_location=got_loc,
        )
        fix_user = (
            f"Failing parser:\n```python\n{parser_code}\n```\n\n"
            f"Page text:\n{page_text}"
        )
        try:
            t0 = time.monotonic()
            raw = ai.generate(fix_system, fix_user, timeout=ai.tool_timeout, _endpoint="extract")
            _log.info("[phase3] fix LLM replied in %.1fs", time.monotonic() - t0)
            raw = _strip_think(raw)
            fixed = _parse_python_block(raw)
            if fixed:
                parser_code = fixed
                _log.info("[phase3] fix produced new parser (%d chars)", len(parser_code))
            else:
                _log.warning("[phase3] fix LLM produced no valid parser — giving up")
                break
        except Exception as exc:
            _log.warning("[phase3] fix LLM call failed: %s — giving up", exc)
            break

    return None, dict(_EMPTY)


# ── Main 3-phase pipeline ─────────────────────────────────────────────────────

def _three_phase_pipeline(domain: str, page_text: str, page_html: str,
                           ai: "AIClient") -> tuple[dict, str | None]:
    """Quality-first parser generation:

    Phase 1 — extraction only (LLM A): focused JSON extraction, no distractions.
              Falls back to domain-specific strategy (seek/linkedin/jsonld) if LLM fails.
    Phase 2 — parser generation (LLM B): informed by confirmed values from Phase 1.
              Parser runs on PLAIN TEXT, so we only pass plain text here (not HTML).
    Phase 3 — validate + fix loop (LLM C×N): runs the parser, compares to Phase 1
              values, sends failures back to LLM for correction (up to 3 retries).

    The html_skeleton is available to callers for diagnostic/future HTML-parser use
    but is NOT included in Phase 2 since current parsers operate on plain text.

    Returns (result_dict, parser_code_or_None).
    """
    # ── Phase 1: Extract ───────────────────────────────────────────────────────
    result = _phase1_extract(domain, page_text, ai)

    # If Phase 1 failed, try the domain strategies as a fallback source of truth
    if not result.get("title") and page_html:
        _log.info("[pipeline] Phase 1 failed — trying domain strategies as fallback")
        for strat_name, strat_fn in [
            ("seek",     _strategy_seek),
            ("jsonld",   _strategy_jsonld),
            ("linkedin", _strategy_linkedin),
        ]:
            fallback_text = strat_fn(page_html)
            if fallback_text:
                lines = [l for l in fallback_text.splitlines() if l.strip()]
                if lines:
                    result = {
                        "title":       lines[0],
                        "company":     lines[1] if len(lines) > 1 else "",
                        "location":    lines[2] if len(lines) > 2 else "",
                        "description": fallback_text.split("Job description:", 1)[-1].strip()
                                       if "Job description:" in fallback_text else "",
                    }
                    _log.info("[pipeline] strategy %s gave title=%r", strat_name, result["title"])
                    break

    if not result.get("title"):
        _log.warning("[pipeline] Phase 1 produced no title — aborting parser generation")
        return result, None

    # ── Phase 2: Generate parser (plain text only) ────────────────────────────
    # Parsers run on clean plain text produced by clean_html(), so we only pass
    # page_text here — NOT the HTML skeleton (which would mislead the LLM into
    # writing an HTML parser instead of a text-pattern parser).
    parser_code = _phase2_generate(domain, page_text, result, ai)
    if not parser_code:
        _log.warning("[pipeline] Phase 2 produced no parser — returning result only")
        return result, None

    # ── Phase 3: Validate + fix ────────────────────────────────────────────────
    final_code, validated_result = _phase3_validate_and_fix(
        domain, page_text, result, parser_code, ai, max_retries=3
    )

    # If Phase 3 confirmed the result, use the validated dict (may have description)
    if validated_result.get("title"):
        result = validated_result

    return result, final_code


def _programmatic_extract(domain: str, page_text: str,
                           page_html: str) -> dict | None:
    """Try to extract job info without any LLM call.

    Checks (in order):
    1. JSON-LD  (@type: JobPosting — industry standard, 100% reliable)
    2. SEEK inline script JSON blob
    3. LinkedIn title-tag strategy

    Returns a result dict if a strategy succeeds (title non-empty), else None.
    """
    for strat_name, strat_fn in [
        ("jsonld",   _strategy_jsonld),
        ("seek",     _strategy_seek),
        ("linkedin", _strategy_linkedin),
    ]:
        if not page_html:
            break
        try:
            text = strat_fn(page_html)
        except Exception as exc:
            _log.debug("[prog] strategy %s raised: %s", strat_name, exc)
            continue
        if not text:
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        title   = lines[0]
        company = lines[1] if len(lines) > 1 else ""
        location = (lines[2]
                    if len(lines) > 2 and "Job description:" not in lines[2]
                    else "")
        description = (text.split("Job description:", 1)[-1].strip()
                       if "Job description:" in text else "")
        if title:
            _log.info("[prog] strategy=%s → title=%r  company=%r", strat_name, title, company)
            return {"title": title, "company": company,
                    "location": location, "description": description}
    return None


# Positional parser template for all pinned-strategy domains.
# _format_job_text ALWAYS produces: line0=title, line1=company, line2=location,
# then "Job description:" header, then description body.
# This template is injected directly — no LLM required.
_POSITIONAL_PARSER_TEMPLATE = '''\
# Generated: {date}  Domain: {domain}
# Parser for {domain} — uses positional extraction on _format_job_text output.
# Format: line 0 = title, line 1 = company, line 2 = location (optional),
#         "Job description:" header, then description body.

def extract(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title    = lines[0] if len(lines) > 0 else ""
    company  = lines[1] if len(lines) > 1 else ""
    # line 2 is location unless it starts with "Job description:"
    if len(lines) > 2 and not lines[2].startswith("Job description:"):
        location = lines[2]
    else:
        location = ""
    # description starts after the "Job description:" marker
    marker = "Job description:"
    desc_start = text.find(marker)
    description = text[desc_start + len(marker):].strip() if desc_start >= 0 else ""
    return {{
        "title":       title,
        "company":     company,
        "location":    location,
        "description": description,
    }}
'''


def _spawn_parser_generation(domain: str, page_text: str, page_html: str,
                              confirmed: dict, ai: "AIClient") -> _threading.Thread:
    """Spawn a background daemon thread to generate + validate + save parser.

    For pinned domains (SEEK, Indeed, LinkedIn): injects a deterministic positional
    template instantly — no LLM call needed, 100% reliable.
    For unknown domains: runs the full 3-phase LLM pipeline (Phase 2 + Phase 3).

    The user has already received the extraction result.  This runs async so
    the next visit to the same domain is fast (<10ms) via the saved parser.
    Returns the thread so callers can optionally join() it (e.g. in tests).
    """
    def _worker() -> None:
        _log.info("[bg-parser] starting generation for %s (title=%r)",
                  domain, confirmed.get("title"))
        try:
            # ── Fast path: pinned domains get a deterministic template ────────
            if domain.lower() in _DOMAIN_PINNED_STRATEGY:
                import datetime as _dt
                code = _POSITIONAL_PARSER_TEMPLATE.format(
                    date=_dt.date.today().isoformat(),
                    domain=domain,
                )
                saved = _try_save_parser(domain, code, page_text)
                _log.info("[bg-parser] positional template saved=%s for %s", saved, domain)
                return

            # ── LLM path: unknown domains ─────────────────────────────────────
            parser_code = _phase2_generate(domain, page_text, confirmed, ai)
            if not parser_code:
                _log.warning("[bg-parser] Phase 2 returned no code for %s", domain)
                return
            final_code, _ = _phase3_validate_and_fix(
                domain, page_text, confirmed, parser_code, ai, max_retries=3
            )
            target_code = final_code or parser_code
            saved = _try_save_parser(domain, target_code, page_text)
            _log.info("[bg-parser] parser saved=%s for %s", saved, domain)
        except Exception as exc:
            _log.warning("[bg-parser] failed for %s: %s", domain, exc)

    t = _threading.Thread(target=_worker, name=f"parser-gen-{domain}", daemon=True)
    t.start()
    _log.info("[bg-parser] background thread started for %s", domain)
    return t


def extract(domain: str, page_text: str, ai: "AIClient",
            page_html: str = "",
            pre_extracted: dict | None = None) -> dict:
    """Return job info dict for the given domain + page content.

    Design principle: extraction (user-facing) and parser generation (background)
    are completely decoupled.  The user never waits for parser generation.

    Flow:
      0. Pre-extracted hit  → client sent JSON-LD / meta data with title + desc → return
      1. Cache hit          → run cached parser (<10ms)                          → return
      2. Programmatic hit   → JSON-LD / SEEK / LinkedIn strategy                 → return
                              + spawn background: Phase 2 + Phase 3 parser generation
      3. LLM extraction     → Phase 1 focused call (JSON only)                  → return
                              + spawn background: Phase 2 + Phase 3 parser generation
      4. All fail           → return empty dict

    LLM cost model:
      - Known domains (JSON-LD / SEEK / LinkedIn): ZERO LLM calls once parser is cached
      - Unknown domains (first visit): 1 LLM call for extraction (returns to user fast)
      - Parser generation: 2–5 background LLM calls amortized across all future visits
    """
    t0 = time.monotonic()

    # ── 0. Pre-extracted data (JSON-LD / meta from client DOM) ───────────────
    # The extension parses JSON-LD and meta tags in the live browser, which works
    # even for React SPAs where the server-sent HTML contains no job content.
    # If the client found a title + description, trust it and skip HTML cleaning.
    pre = pre_extracted or {}
    if pre.get("title") and pre.get("description"):
        _log.info("[extract] domain=%s using pre-extracted data (JSON-LD/meta) — title=%r",
                  domain, pre["title"])
        return {
            "title":       pre["title"],
            "company":     pre.get("company", ""),
            "location":    pre.get("location", ""),
            "description": pre["description"],
        }

    if page_html:
        _log.debug("[extract] cleaning HTML for %s (%d chars raw)", domain, len(page_html))
        cleaned = clean_html(page_html, domain=domain)
        _log.info("[extract] domain=%s clean_text=%d chars", domain, len(cleaned))
        if cleaned:
            page_text = cleaned
        elif page_text:
            _log.info("[extract] domain=%s HTML cleaning empty — using page_text fallback (%d chars)",
                      domain, len(page_text))
        # else: both empty — will try pre-extracted partial data or LLM
    else:
        _log.info("[extract] domain=%s text_len=%d", domain, len(page_text))

    # ── 1. Cache hit ──────────────────────────────────────────────────────────
    result = _try_cached(domain, page_text)
    if result:
        _log.info("[extract] cache hit for %s in %.1fms", domain, (time.monotonic()-t0)*1000)
        return result

    # ── 2. Programmatic extraction (fast, zero LLM) ───────────────────────────
    prog_result = _programmatic_extract(domain, page_text, page_html)
    if prog_result:
        elapsed = time.monotonic() - t0
        _log.info("[extract] programmatic for %s in %.1fms — title=%r",
                  domain, elapsed * 1000, prog_result.get("title"))
        # Parser not yet cached → generate in background; next visit will be fast
        _safe_domain = _make_safe_domain(domain)
        parser_file  = PARSERS_DIR / f"{_safe_domain}.py"
        lock = _get_domain_lock(domain)
        if lock.acquire(blocking=False):
            try:
                if not parser_file.exists():
                    _spawn_parser_generation(domain, page_text, page_html, prog_result, ai)
            finally:
                lock.release()
        return prog_result

    # ── 3. LLM extraction (Phase 1 only — focused, JSON only) ────────────────
    # Supplement page_text with pre-extracted partial data if page_text is thin.
    llm_text = page_text
    if pre.get("title") and len(page_text) < 500:
        pre_summary = (
            f"Title: {pre['title']}\n"
            f"Company: {pre.get('company', '')}\n"
            f"Location: {pre.get('location', '')}\n"
            f"{page_text}"
        ).strip()
        llm_text = pre_summary
        _log.info("[extract] domain=%s supplementing thin page_text with pre-extracted fields", domain)

    _log.info("[extract] no programmatic strategy for %s — LLM Phase 1 extraction", domain)
    try:
        llm_result = _phase1_extract(domain, llm_text, ai)
    except Exception as exc:
        _log.warning("[extract] LLM extraction failed for %s: %s", domain, exc)
        return dict(_EMPTY)

    if not llm_result.get("title"):
        # Last resort: return whatever the client pre-extracted (title at minimum)
        if pre.get("title"):
            _log.info("[extract] domain=%s LLM empty — returning pre-extracted partial: title=%r",
                      domain, pre["title"])
            return {
                "title":       pre["title"],
                "company":     pre.get("company", ""),
                "location":    pre.get("location", ""),
                "description": pre.get("description", ""),
            }
        _log.warning("[extract] LLM returned no title for %s", domain)
        return dict(_EMPTY)

    elapsed = time.monotonic() - t0
    _log.info("[extract] LLM extraction for %s in %.1fs — title=%r",
              domain, elapsed, llm_result.get("title"))

    # ── 4. Spawn background parser generation ─────────────────────────────────
    lock = _get_domain_lock(domain)
    if lock.acquire(blocking=False):
        try:
            _safe_domain = _make_safe_domain(domain)
            parser_file = PARSERS_DIR / f"{_safe_domain}.py"
            if not parser_file.exists():
                _spawn_parser_generation(domain, page_text, page_html, llm_result, ai)
        finally:
            lock.release()

    return llm_result


def extract_and_wait(domain: str, page_text: str, ai: "AIClient",
                     page_html: str = "",
                     parser_timeout: float = 900.0) -> dict:
    """Like extract(), but blocks until the background parser thread finishes.

    For use in tests and CLI tooling where the caller needs to verify that the
    parser was saved before asserting.  Not for use in the FastAPI request path.
    """
    t0 = time.monotonic()

    if page_html:
        page_text = clean_html(page_html, domain=domain)

    # Cache hit — no parser generation needed
    result = _try_cached(domain, page_text)
    if result:
        return result

    prog_result = _programmatic_extract(domain, page_text, page_html)
    if prog_result:
        _safe = _make_safe_domain(domain)
        parser_file = PARSERS_DIR / f"{_safe}.py"
        if not parser_file.exists():
            thread = _spawn_parser_generation(domain, page_text, page_html, prog_result, ai)
            _log.info("[extract_and_wait] waiting for background parser (timeout=%.0fs)", parser_timeout)
            thread.join(timeout=parser_timeout)
            if thread.is_alive():
                _log.warning("[extract_and_wait] parser generation timed out after %.0fs", parser_timeout)
        return prog_result

    try:
        llm_result = _phase1_extract(domain, page_text, ai)
    except Exception as exc:
        _log.warning("[extract_and_wait] LLM extraction failed: %s", exc)
        return dict(_EMPTY)

    if not llm_result.get("title"):
        return dict(_EMPTY)

    thread = _spawn_parser_generation(domain, page_text, page_html, llm_result, ai)
    _log.info("[extract_and_wait] waiting for background parser (timeout=%.0fs)", parser_timeout)
    thread.join(timeout=parser_timeout)
    if thread.is_alive():
        _log.warning("[extract_and_wait] parser generation timed out after %.0fs", parser_timeout)

    _log.info("[extract_and_wait] total=%.1fs  title=%r", time.monotonic() - t0,
              llm_result.get("title"))
    return llm_result


def _make_safe_domain(domain: str) -> str:
    """Return a filename-safe version of a domain string."""
    import re as _re
    return _re.sub(r"[^\w.\-]", "_", domain.replace("www.", ""))






def _try_save_parser(domain: str, code: str, page_text: str) -> bool:
    """Safety-check, validate, and save an LLM-generated parser.

    Pipeline:
      1. AST safety scan  — block dangerous imports, builtins, and syscalls
      2. Functional test  — run code against the sample page_text
      3. Title sanity     — verify output looks like a real job title
      4. Save             — persist to PARSERS_DIR only if all checks pass
    """
    from tool_server import run_parser as _run_parser, save_parser as _save_parser

    # ── 1. Safety scan ───────────────────────────────────────────────────────
    _log.info("[save_parser] generated parser code for %s:\n%s", domain, code)
    safe, reason = _safety_check(code)
    if not safe:
        _log.error("[save_parser] SAFETY REJECTION for %s — %s\n--- rejected code ---\n%s\n---",
                   domain, reason, code)
        return False

    # ── 2. Functional test ───────────────────────────────────────────────────
    try:
        test = _run_parser(code=code, text=page_text)
    except Exception as exc:
        _log.warning("[save_parser] runtime error for %s: %s — NOT saving", domain, exc)
        return False

    title = test.get("title", "")
    _log.info("[save_parser] validation run: title=%r company=%r location=%r error=%r",
              title, test.get("company", ""), test.get("location", ""), test.get("error", ""))

    # ── 3. Title sanity ──────────────────────────────────────────────────────
    if not _looks_valid_title(title, domain):
        _log.warning("[save_parser] validation failed for %s — title=%r — NOT saving", domain, title)
        return False

    # ── 4. Save ──────────────────────────────────────────────────────────────
    res = _save_parser(domain=domain, code=code)
    _log.info("[save_parser] saved parser for %s ✓ — %s", domain, res)
    return True


# ── LLM-generated code safety checker ────────────────────────────────────────

# Modules the parser must never import
_BLOCKED_IMPORTS = frozenset({
    "os", "sys", "subprocess", "shutil", "pathlib", "glob",
    "socket", "urllib", "http", "requests", "httpx", "aiohttp",
    "pickle", "marshal", "shelve", "dbm",
    "ctypes", "cffi", "importlib", "runpy", "pkgutil",
    "tempfile", "threading", "multiprocessing", "concurrent",
    "signal", "resource", "mmap", "fcntl", "termios",
    "builtins", "inspect", "gc", "weakref", "traceback",
    "ast", "dis", "code", "codeop", "tokenize",
    "pty", "tty", "nis", "pwd", "grp",
})

# Builtin names the parser must never call
_BLOCKED_BUILTINS = frozenset({
    "eval", "exec", "compile", "__import__", "open",
    "breakpoint", "memoryview", "vars", "locals", "globals",
    "exit", "quit",
})

# Object methods that suggest file/process/network access
_BLOCKED_METHODS = frozenset({
    "system", "popen", "run", "call", "Popen", "check_output", "check_call",
    "remove", "unlink", "rmdir", "rmtree", "chmod", "chown", "rename", "replace",
    "write", "writelines", "truncate", "seek",
    "connect", "bind", "send", "sendall", "recv",
})

# Regex patterns that hint at obfuscation / shell injection even without AST
_SUSPICIOUS_PATTERNS = [
    (re.compile(r"\beval\s*\("),               "eval()"),
    (re.compile(r"\bexec\s*\("),               "exec()"),
    (re.compile(r"\bos\s*\.\s*system\s*\("),   "os.system()"),
    (re.compile(r"\bsubprocess\b"),             "subprocess"),
    (re.compile(r"__import__\s*\("),            "__import__()"),
    (re.compile(r"\bopen\s*\("),               "open()"),
    (re.compile(r"\bchr\s*\(\s*\d"),           "chr() literal (possible obfuscation)"),
    (re.compile(r"\\x[0-9a-fA-F]{2}.*\\x"),   "hex-escape sequence (possible obfuscation)"),
    (re.compile(r"base64"),                     "base64 (possible obfuscation)"),
    (re.compile(r"rm\s+-"),                     "shell rm command"),
    (re.compile(r":\s*/dev/"),                  "/dev/ path"),
]


def _safety_check(code: str) -> tuple[bool, str]:
    """AST + regex static safety analysis for LLM-generated parser code.

    Returns (is_safe, rejection_reason).
    Checks are layered: fast regex first, then full AST walk.
    """
    # ── Fast regex pass ───────────────────────────────────────────────────────
    for pattern, label in _SUSPICIOUS_PATTERNS:
        if pattern.search(code):
            return False, f"suspicious pattern detected: {label}"

    # ── AST parse ─────────────────────────────────────────────────────────────
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"

    # ── AST walk ──────────────────────────────────────────────────────────────
    for node in ast.walk(tree):

        # Block any import of a dangerous module
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_IMPORTS:
                    return False, f"blocked import: {alias.name}"

        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BLOCKED_IMPORTS:
                return False, f"blocked import from: {node.module}"

        # Block dangerous builtin calls: eval(), exec(), open(), __import__()
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in _BLOCKED_BUILTINS:
                    return False, f"blocked builtin call: {node.func.id}()"

            # Block dangerous method calls: .system(), .write(), .unlink() etc.
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in _BLOCKED_METHODS:
                    return False, f"blocked method call: .{node.func.attr}()"

        # Block dunder attribute access — potential sandbox escape
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("__") and attr.endswith("__") and attr not in (
                "__name__", "__doc__", "__class__",
            ):
                return False, f"blocked dunder attribute: {attr}"

    _log.info("[safety] parser code for passed all checks (%d chars, %d AST nodes)",
              len(code), sum(1 for _ in ast.walk(tree)))
    return True, ""




def create_parser_background(domain: str, page_text: str, ai: "AIClient",
                              known_result: dict | None = None) -> None:
    """Kept for API compatibility — no longer used; combined extract handles this inline."""
    _log.info("[parser-bg] create_parser_background called for %s — now handled inline by extract()", domain)



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





