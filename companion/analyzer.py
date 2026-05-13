"""
overhired — job description analyzer

Three tools:
  1. decode_jargon   — corporate BS → plain truth (curated dict + LLM context)
  2. score_job_fit   — resume vs JD → match score + honest recommendation
  3. research_company — fetch website → LLM summary (culture, products, red flags)
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ai_client import AIClient

# ── 1. Jargon decoder ─────────────────────────────────────────────────────────

# Curated phrase → plain-truth translation.
# Keys are lowercase regex patterns for flexible matching.
_JARGON: dict[str, str] = {
    r"dynamic\s+(?:and\s+)?(?:fast[- ]?paced\s+)?environment":
        "Constant chaos, no processes, frequent firefighting",
    r"fast[- ]?paced\s+(?:and\s+)?(?:dynamic\s+)?environment":
        "High stress, expect overtime, WLB is an afterthought",
    r"wear\s+(?:many|multiple)\s+hats?":
        "Understaffed — you'll do 3 people's jobs for 1 salary",
    r"self[- ]?starter":
        "No mentorship, no onboarding, figure it out yourself",
    r"strong\s+(?:and\s+)?(?:solid\s+)?(?:clear\s+)?(?:organizational\s+)?structure":
        "Heavy bureaucracy, slow decisions, lots of approval chains",
    r"like\s+a\s+(?:family|close[- ]?knit\s+team)":
        "No professional boundaries, guilt-tripped for taking leave",
    r"unlimited\s+(?:pto|vacation|annual\s+leave)":
        "People feel guilty taking leave; actual usage is below average",
    r"(?:rockstar|ninja|guru|wizard|unicorn)\s+developer":
        "Toxic culture, overworked expectations, probably bro-heavy",
    r"competitive\s+(?:salary|compensation|pay)":
        "Below-market; if it were good they'd state the number",
    r"passionate\s+(?:about\s+what\s+we\s+do|team|culture)":
        "Unpaid overtime expected; 'passion' covers for poor pay",
    r"results[- ]?(?:oriented|driven|focused)":
        "Micromanagement or blame culture — output is everything",
    r"agile\s+(?:environment|team|culture)":
        "Often means chaos with stand-ups, not real Scrum",
    r"move\s+fast(?:\s+and\s+break\s+things)?":
        "Technical debt everywhere, no tests, constant fire drills",
    r"disrupt(?:ive|ing)":
        "May have questionable legal or ethical practices",
    r"startup\s+(?:culture|mentality|environment)":
        "Low pay, long hours, 'equity' that may never materialise",
    r"growth\s+(?:opportunity|mindset|culture)":
        "No budget for training; you grow on your own time",
    r"detail[- ]?oriented":
        "Tedious, repetitive work or heavy review/sign-off culture",
    r"team\s+player":
        "You'll be blamed for others' failures; expected to cover for them",
    r"must\s+(?:thrive|excel|perform)\s+under\s+pressure":
        "The job regularly involves unrealistic deadlines",
    r"collaborative\s+(?:and\s+)?(?:open[- ]?plan|open)\s+office":
        "Noisy open-plan — no focus time",
    r"(?:fun|vibrant)\s+(?:office|culture|team)":
        "Ping-pong table to distract from low pay",
    r"entrepreneurial\s+(?:spirit|mindset)":
        "Expects founder-level commitment with employee-level pay",
    r"(?:looking\s+for|seeking)\s+(?:a\s+)?(?:self[- ]?motivated|proactive)":
        "Low support environment — you're largely on your own",
}

_GREEN_FLAGS: dict[str, str] = {
    r"salary\s+(?:range|band|of)\s+\$[\d,]+":
        "Transparent salary — good sign of respect for candidates",
    r"4[- ]day\s+(?:work\s+)?week":
        "Genuine WLB investment",
    r"remote[- ]?first":
        "Culture built around distributed work (not just 'allowed')",
    r"parental\s+leave\s+(?:of\s+)?\d+\s+(?:weeks?|months?)":
        "Quantified parental leave — shows genuine commitment",
    r"annual\s+(?:learning|training|education)\s+(?:budget|allowance)":
        "Invests in your growth",
    r"employee\s+ownership|esop":
        "Real equity stake, not just options that expire",
    r"no\s+(?:on[- ]?call|after[- ]?hours)":
        "Genuine boundaries on working hours",
    r"diverse\s+(?:and\s+)?inclusive":
        "Potentially meaningful (look for specifics, not just words)",
}


def decode_jargon(job_description: str, ai: "AIClient") -> dict:
    """Decode corporate jargon in a job description.

    Returns red flags, green flags, and an LLM plain-English rewrite of
    key phrases — so you know what you're actually signing up for.

    Args:
        job_description: The full job description text.
    """
    text_lower = job_description.lower()
    red_flags:   list[dict] = []
    green_flags: list[dict] = []

    for pattern, meaning in _JARGON.items():
        match = re.search(pattern, text_lower)
        if match:
            red_flags.append({"phrase": match.group(0), "reality": meaning})

    for pattern, meaning in _GREEN_FLAGS.items():
        match = re.search(pattern, text_lower)
        if match:
            green_flags.append({"phrase": match.group(0), "signal": meaning})

    # LLM pass: catch anything the dict missed, give overall vibe
    system = (
        "You are a brutally honest career advisor who decodes corporate job-listing BS. "
        "Identify any additional red flags or green flags not already caught by a pattern match. "
        "Reply with ONLY a JSON object:\n"
        '{"additional_red_flags": [{"phrase": "...", "reality": "..."}], '
        '"additional_green_flags": [{"phrase": "...", "signal": "..."}], '
        '"overall_vibe": "one sentence plain-English summary of what this job is really like", '
        '"verdict": "Apply" | "Apply with caution" | "Skip"}'
    )
    user = (
        f"Job description:\n{job_description[:4000]}\n\n"
        f"Already detected red flags: {[f['phrase'] for f in red_flags]}\n"
        f"Already detected green flags: {[f['phrase'] for f in green_flags]}"
    )
    try:
        raw = ai.generate(system, user).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        llm_out = json.loads(raw)
    except Exception:
        llm_out = {}

    red_flags.extend(llm_out.get("additional_red_flags", []))
    green_flags.extend(llm_out.get("additional_green_flags", []))

    return {
        "red_flags":    red_flags,
        "green_flags":  green_flags,
        "overall_vibe": llm_out.get("overall_vibe", ""),
        "verdict":      llm_out.get("verdict", ""),
    }


# ── 2. Job fit scorer ─────────────────────────────────────────────────────────

def score_job_fit(job_description: str, resume_text: str, ai: "AIClient") -> dict:
    """Score how well your resume matches a job description.

    Returns a 0–10 score, matching skills, gaps, and an honest recommendation
    on whether you should apply, apply with caveats, or skip.

    Args:
        job_description: Full job description text.
        resume_text:     Your resume text (plain text, not PDF binary).
    """
    system = (
        "You are a brutally honest technical recruiter. "
        "Evaluate how well the candidate's resume matches the job description. "
        "Be specific — name actual skills, tools, and requirements. "
        "Reply with ONLY a JSON object:\n"
        "{\n"
        '  "score": <0-10 integer>,\n'
        '  "matching_skills": ["skill1", "skill2", ...],\n'
        '  "missing_skills": ["skill1", "skill2", ...],\n'
        '  "overqualified_risk": true | false,\n'
        '  "experience_gap": "none" | "minor" | "significant",\n'
        '  "recommendation": "Apply" | "Apply with caveats" | "Stretch role" | "Skip",\n'
        '  "reasoning": "2-3 sentence honest assessment"\n'
        "}"
    )
    user = (
        f"Job Description:\n{job_description[:3000]}\n\n"
        f"Resume:\n{resume_text[:3000]}"
    )
    try:
        raw = ai.generate(system, user).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        # Validate score is int in range
        result["score"] = max(0, min(10, int(result.get("score", 0))))
        return result
    except Exception as exc:
        return {"error": str(exc), "score": 0, "recommendation": "Unknown"}


# ── 3. Company research ───────────────────────────────────────────────────────

_FETCH_PATHS = ["/", "/about", "/about-us", "/company", "/careers", "/culture"]
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; overhired-research/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}

# Private / loopback IP ranges that must never be fetched (SSRF protection)
_PRIVATE_NETS = re.compile(
    r"^(?:"
    r"127\."                        # loopback
    r"|10\."                        # RFC-1918
    r"|192\.168\."                  # RFC-1918
    r"|172\.(?:1[6-9]|2\d|3[01])\."# RFC-1918
    r"|169\.254\."                  # link-local
    r"|::1"                         # IPv6 loopback
    r"|fc00:"                       # IPv6 ULA
    r"|fd"                          # IPv6 ULA
    r")"
)


def _safe_domain(domain: str) -> str:
    """Validate domain for outbound fetch: must be plain hostname over http/https."""
    # Strip any scheme/path the caller may have included
    domain = re.sub(r"^https?://", "", domain).split("/")[0].lower()
    if not re.match(r"^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$", domain):
        raise ValueError(f"Invalid domain: {domain!r}")
    if _PRIVATE_NETS.match(domain):
        raise ValueError(f"Private/loopback domain not allowed: {domain!r}")
    return domain


def _fetch_text(domain: str) -> str:
    """Fetch text content from a company's key pages (HTTPS only, no private IPs)."""
    domain = _safe_domain(domain)   # raises ValueError on bad/private domains
    parts: list[str] = []
    base = f"https://{domain}"
    with httpx.Client(timeout=8, follow_redirects=True, headers=_HEADERS) as client:
        for path in _FETCH_PATHS:
            try:
                resp = client.get(base + path)
                if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                    # Strip tags crudely — no BS4 dependency
                    text = re.sub(r"<[^>]+>", " ", resp.text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(text) > 200:
                        parts.append(f"[{path}]\n{text[:2000]}")
            except Exception:
                continue
    return "\n\n".join(parts)[:8000]


def research_company(domain: str, company_name: str, ai: "AIClient") -> dict:
    """Fetch a company's website and generate a structured research summary.

    Covers: what they do, products/services, culture signals, size/stage,
    tech stack hints, and red/green flags from their public messaging.

    Args:
        domain:       Company website domain, e.g. 'stripe.com'.
        company_name: Human-readable company name, e.g. 'Stripe'.
    """
    try:
        raw_text = _fetch_text(domain)
    except ValueError as exc:
        return {"error": str(exc)}

    if not raw_text:
        raw_text = f"No web content available for {domain}."

    system = (
        "You are a thorough company analyst. Based on the scraped website text, "
        "produce a structured JSON research summary for a job seeker deciding whether to apply. "
        "Be specific — pull real product names, real culture words they use. "
        "If information is missing from the text, say 'not stated'. "
        "Reply with ONLY a JSON object:\n"
        "{\n"
        '  "overview": "2-3 sentences on what the company does",\n'
        '  "products_services": ["product1", "product2"],\n'
        '  "industry": "...",\n'
        '  "size_stage": "startup (<50) | scaleup (50-500) | mid-market | enterprise | unknown",\n'
        '  "tech_stack_hints": ["tech1", "tech2"],\n'
        '  "culture_signals": ["signal1", "signal2"],\n'
        '  "mission_statement": "their stated mission or empty string",\n'
        '  "red_flags": ["flag1", "flag2"],\n'
        '  "green_flags": ["flag1", "flag2"],\n'
        '  "notable": "anything unusual or worth knowing"\n'
        "}"
    )
    user = (
        f"Company: {company_name} ({domain})\n\n"
        f"Scraped website content:\n{raw_text}"
    )

    try:
        raw = ai.generate(system, user).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        return {"error": str(exc), "overview": "", "domain": domain}
