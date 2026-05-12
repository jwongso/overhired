"""
overhired — companion service

Start with:
    pip install -r requirements.txt
    python main.py

Or with custom port:
    python main.py --port 7878

Endpoints:
    GET  /health    — liveness probe used by the extension
    POST /generate  — build prompt, call AI, return cover letter (md + html)
    POST /save      — write cover_letter.md + cover_letter.html to disk
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path
from typing import Optional

import markdown as md_lib
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config as cfg_module
import ai_client as ai_module

# ── Boot ─────────────────────────────────────────────────────────────────────

CFG = cfg_module.load()
AI  = ai_module.AIClient(CFG["ai"])

app = FastAPI(title="overhired companion", version="1.0.0")

# Allow the browser extension (chrome-extension:// origin) to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # Extension origin varies by browser/ID; wildcard is fine for localhost
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    job_title:            str
    company:              str
    job_description:      str
    resume_text:          str
    user_profile:         dict          = Field(default_factory=dict)
    global_instructions:  str           = ""
    per_job_instructions: str           = ""
    easter_egg:           bool          = False
    easter_egg_text:      Optional[str] = None


class GenerateResponse(BaseModel):
    cover_letter_md:   str
    cover_letter_html: str


class SaveRequest(BaseModel):
    company:           str
    role:              str
    cover_letter_md:   str
    cover_letter_html: str
    output_dir:        Optional[str] = None   # overrides config if provided


class SaveResponse(BaseModel):
    md_path:   str
    html_path: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    ai_ok = AI.health_check()
    return {
        "status":     "ok",
        "ai_provider": CFG["ai"]["provider"],
        "ai_model":    CFG["ai"]["model"],
        "ai_endpoint": CFG["ai"]["endpoint"],
        "ai_reachable": ai_ok,
    }


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    system_prompt = _build_system_prompt()
    user_prompt   = _build_user_prompt(req)

    try:
        raw = AI.generate(system_prompt, user_prompt)
    except ai_module.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Normalise — some models wrap output in markdown fences
    cover_md = _unwrap_fences(raw)

    # Append easter egg HTML comment if requested
    if req.easter_egg:
        egg_text = req.easter_egg_text or CFG["cover_letter"]["easter_egg_text"]
        # Inject company/role into the egg text if placeholders exist
        egg_text = egg_text.replace("{company}", req.company).replace("{role}", req.job_title)
        comment_lines = "\n".join(f"  {l}" for l in egg_text.splitlines())
        cover_md += f"\n\n<!--\n{comment_lines}\n-->\n"

    cover_html = _md_to_html(cover_md, req.company, req.job_title)

    return GenerateResponse(cover_letter_md=cover_md, cover_letter_html=cover_html)


@app.post("/save", response_model=SaveResponse)
def save(req: SaveRequest):
    out_root = Path(req.output_dir or CFG["output_dir"]).expanduser()
    company  = _safe_name(req.company)
    role     = _safe_name(req.role)
    dest     = out_root / company / role
    dest.mkdir(parents=True, exist_ok=True)

    md_path   = dest / "cover_letter.md"
    html_path = dest / "cover_letter.html"

    md_path.write_text(req.cover_letter_md,   encoding="utf-8")
    html_path.write_text(req.cover_letter_html, encoding="utf-8")

    return SaveResponse(md_path=str(md_path), html_path=str(html_path))


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    max_words = CFG["cover_letter"].get("max_words", 450)
    language  = CFG["cover_letter"].get("language", "English")
    return textwrap.dedent(f"""\
        You are an expert cover letter writer. Your task is to produce a
        personalized, professional cover letter in Markdown format.

        Guidelines:
        - Language: {language}
        - Target length: {max_words} words (be concise and impactful)
        - Tone: confident, warm, genuine — never sycophantic
        - Start with a salutation (e.g. "Dear Hiring Team,")
        - Highlight 2–3 specific skills or experiences from the resume that
          directly match the job requirements
        - End with a professional closing and the applicant's name
        - Do NOT include a date, subject line, or postal addresses
        - Output ONLY the cover letter — no preamble, no commentary
    """)


def _build_user_prompt(req: GenerateRequest) -> str:
    parts = [
        f"## Role\n{req.job_title} at {req.company}",
        f"## Job Description\n{req.job_description.strip()}",
        f"## My Resume\n{req.resume_text.strip()}",
    ]

    profile = req.user_profile
    if profile:
        profile_lines = "\n".join(
            f"- {k}: {v}" for k, v in profile.items() if v
        )
        parts.append(f"## My Profile\n{profile_lines}")

    if req.global_instructions.strip():
        parts.append(f"## Global Instructions (always apply)\n{req.global_instructions.strip()}")

    if req.per_job_instructions.strip():
        parts.append(
            f"## Specific Instructions for This Application\n"
            f"{req.per_job_instructions.strip()}"
        )

    parts.append("Write the cover letter now:")
    return "\n\n".join(parts)


# ── HTML export ───────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Cover Letter — {role} at {company}</title>
  <style>
    body {{
      font-family: Georgia, 'Times New Roman', serif;
      max-width: 740px;
      margin: 60px auto;
      padding: 0 24px;
      color: #1a1a1a;
      line-height: 1.75;
      font-size: 16px;
    }}
    p {{ margin: 0 0 1em; }}
    strong {{ font-weight: 600; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""

def _md_to_html(cover_md: str, company: str, role: str) -> str:
    # Split off any trailing HTML comment (easter egg) before converting,
    # then re-append it so it stays outside the converted <p> tags.
    egg_comment = ""
    md_body = cover_md

    comment_match = re.search(r"\n*(<!--.*?-->)\s*$", cover_md, re.DOTALL)
    if comment_match:
        egg_comment = "\n" + comment_match.group(1)
        md_body = cover_md[: comment_match.start()]

    body_html = md_lib.markdown(md_body)
    full_body = body_html + egg_comment

    return _HTML_TEMPLATE.format(
        company=company,
        role=role,
        body=full_body,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _unwrap_fences(text: str) -> str:
    """Remove ```markdown … ``` wrappers that some models add."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (``` or ```markdown) and last ``` line
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return text


def _safe_name(name: str) -> str:
    """Convert a company/role name to a safe directory component."""
    name = name.strip()
    name = re.sub(r"[^\w\s-]", "", name)       # remove special chars
    name = re.sub(r"[\s_]+", "-", name)        # spaces → hyphens
    name = name.strip("-")
    return name[:64] or "unknown"              # cap length


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="overhired-companion")
    parser.add_argument("--port", type=int, default=CFG.get("companion_port", 7878))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reload", action="store_true", help="Dev mode auto-reload")
    args = parser.parse_args()

    print(f"overhired companion  |  http://{args.host}:{args.port}")
    print(f"  AI provider : {CFG['ai']['provider']}  ({CFG['ai']['endpoint']})")
    print(f"  AI model    : {CFG['ai']['model']}")
    print(f"  Output dir  : {CFG['output_dir']}")
    print()

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
