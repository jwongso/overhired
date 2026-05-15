"""
grapply — MCP server (real MCP protocol via FastMCP)

This is a proper MCP server that any MCP client can connect to:
  - Claude Desktop
  - Cursor / VS Code with MCP extension
  - MCP Inspector (for development/testing)
  - Any other MCP-compatible client

Transport: stdio (default) — the client spawns this as a subprocess and
communicates over stdin/stdout using JSON-RPC.

Usage:
  # Run directly (stdio, for MCP clients):
  python mcp_server.py

  # Test interactively with MCP Inspector:
  npx @modelcontextprotocol/inspector python mcp_server.py

  # Add to Claude Desktop (see docs/MCP_SERVER.md)

Tools exposed:
  Parser tools (tool_server.py):
    run_parser    - test a Python extract() function against page text
    save_parser   - cache a working parser for a domain
    read_parser   - read an existing cached parser
    list_parsers  - list all cached parsers
    delete_parser - remove a cached parser to force regeneration

  Application tracker (tracker.py):
    log_application     - record a job application
    list_applications   - list/filter applications
    update_application  - update status or notes
    get_stats           - response rate, counts by status
    delete_application  - remove a record

  Job analysis (analyzer.py):
    decode_jargon    - decode corporate BS in a job description
    score_job_fit    - match resume vs JD, get honest recommendation
    research_company - fetch company website → structured research summary
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from any working directory
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server import FastMCP

import tool_server as ts
import tracker     as tk
import analyzer    as az
import config      as cfg_module
import ai_client   as ai_module

# Build an AIClient using the same config as the companion
_CFG = cfg_module.load()
_AI  = ai_module.AIClient(_CFG["ai"])

mcp = FastMCP(
    name="grapply",
    instructions=(
        "grapply tools for job seekers: parse job pages, track applications, "
        "decode corporate jargon, score job fit, and research companies."
    ),
)


# ── Parser tools ──────────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Execute a Python extract(text: str) -> dict function against job page text. "
        "Returns the extracted dict or an error message. "
        "The function MUST return a dict with keys: title, company, description, location. "
        "Use this to test your parser before saving it."
    )
)
def run_parser(code: str, text: str) -> dict:
    """
    Args:
        code: Python source defining `def extract(text: str) -> dict`.
              Must return {title, company, description, location}.
        text: The job page innerText to test against (max 12 000 chars).
    """
    return ts.run_parser(code, text)


@mcp.tool(
    description=(
        "Save a working parser for a domain. "
        "Call this ONLY after run_parser confirms a non-empty title. "
        "The parser is cached in ~/.grapply/parsers/ and reused on future scans."
    )
)
def save_parser(domain: str, code: str) -> dict:
    """
    Args:
        domain: Hostname the parser targets, e.g. 'linkedin.com' or 'seek.co.nz'.
        code:   The same Python code that passed run_parser validation.
    """
    return ts.save_parser(domain, code)


@mcp.tool(
    description="Read the source of an existing cached parser for a domain."
)
def read_parser(domain: str) -> dict:
    """
    Args:
        domain: Hostname to look up, e.g. 'linkedin.com'.
    """
    return ts.read_parser(domain)


@mcp.tool(
    description=(
        "List all cached parsers in ~/.grapply/parsers/ "
        "with domain, file size, and last-modified date."
    )
)
def list_parsers() -> dict:
    return ts.list_parsers()


@mcp.tool(
    description=(
        "Delete a cached parser to force regeneration on the next scan. "
        "Use this when a site has changed its layout and the parser is broken."
    )
)
def delete_parser(domain: str) -> dict:
    """
    Args:
        domain: Hostname whose parser should be removed.
    """
    return ts.delete_parser(domain)


# ── Application tracker tools ─────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Record a new job application in your local tracker. "
        "Call this every time you apply somewhere so you can track progress."
    )
)
def log_application(
    domain: str,
    title: str,
    company: str,
    date_applied: str = "",
    notes: str = "",
) -> dict:
    """
    Args:
        domain:       Job board or company domain, e.g. 'seek.co.nz'.
        title:        Job title you applied for.
        company:      Company name.
        date_applied: ISO date YYYY-MM-DD. Leave empty to use today.
        notes:        Optional notes (why you applied, referral, etc.).
    """
    return tk.log_application(domain, title, company, date_applied, notes)


@mcp.tool(
    description=(
        "List tracked job applications. Filter by status or recency. "
        "Use this to review your pipeline."
    )
)
def list_applications(status: str = "", days: int = 0, limit: int = 50) -> dict:
    """
    Args:
        status: Filter by status: applied, interviewing, offered, accepted, rejected, ghosted, withdrawn.
                Leave empty for all.
        days:   Only show applications from the last N days. 0 = no limit.
        limit:  Maximum number of results (default 50).
    """
    return tk.list_applications(status, days, limit)


@mcp.tool(
    description="Update the status or notes of an existing application."
)
def update_application(id: int, status: str = "", notes: str = "") -> dict:
    """
    Args:
        id:     Application ID (from log_application or list_applications).
        status: New status. One of: applied, interviewing, offered, accepted, rejected, ghosted, withdrawn.
        notes:  Text to append to existing notes.
    """
    return tk.update_application(id, status, notes)


@mcp.tool(
    description=(
        "Return aggregate stats across all tracked applications: "
        "total count, breakdown by status, response rate, average days to first reply."
    )
)
def get_stats() -> dict:
    return tk.get_stats()


@mcp.tool(description="Permanently delete an application record.")
def delete_application(id: int) -> dict:
    """Args: id: Application ID to remove."""
    return tk.delete_application(id)


# ── Job analysis tools ────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Decode corporate jargon and buzzwords in a job description. "
        "Returns red flags (with plain-truth translations), green flags, "
        "an overall vibe summary, and a verdict: Apply / Apply with caution / Skip."
    )
)
def decode_jargon(job_description: str) -> dict:
    """
    Args:
        job_description: The full job description text.
    """
    return az.decode_jargon(job_description, _AI)


@mcp.tool(
    description=(
        "Score how well your resume matches a job description (0–10). "
        "Returns matching skills, gaps, overqualification risk, and an honest "
        "recommendation: Apply / Apply with caveats / Stretch role / Skip."
    )
)
def score_job_fit(job_description: str, resume_text: str) -> dict:
    """
    Args:
        job_description: Full job description text.
        resume_text:     Your resume as plain text.
    """
    return az.score_job_fit(job_description, resume_text, _AI)


@mcp.tool(
    description=(
        "Research a company by fetching their website and generating a structured summary: "
        "overview, products/services, culture signals, size/stage, tech stack hints, "
        "and red/green flags from their public messaging."
    )
)
def research_company(domain: str, company_name: str) -> dict:
    """
    Args:
        domain:       Company website domain, e.g. 'stripe.com'.
        company_name: Human-readable name, e.g. 'Stripe'.
    """
    return az.research_company(domain, company_name, _AI)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # stdio transport: the MCP client spawns this process and talks via stdin/stdout.
    # This is the standard transport for local MCP servers (Claude Desktop, Cursor).
    mcp.run(transport="stdio")
