"""
overhired — MCP server (real MCP protocol via FastMCP)

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
  run_parser    - test a Python extract() function against page text
  save_parser   - cache a working parser for a domain
  read_parser   - read an existing cached parser
  list_parsers  - list all cached parsers
  delete_parser - remove a cached parser to force regeneration

All tool implementations are in tool_server.py (shared with the companion's
internal agentic loop so behaviour is identical in both code paths).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running from any working directory
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server import FastMCP

# Reuse the same tool implementations used by the internal agentic loop.
# This keeps behaviour identical whether you trigger extraction via the
# companion's /extract endpoint or via an external MCP client.
import tool_server as ts

mcp = FastMCP(
    name="overhired-parser-tools",
    instructions=(
        "Tools for writing, testing, and caching job-page parser scripts. "
        "Write a Python extract(text) function, test it with run_parser, "
        "then save it with save_parser once it returns a non-empty title."
    ),
)


# ── Tool registrations ────────────────────────────────────────────────────────

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
        "The parser is cached in ~/.overhired/parsers/ and reused on future scans."
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
        "List all cached parsers in ~/.overhired/parsers/ "
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # stdio transport: the MCP client spawns this process and talks via stdin/stdout.
    # This is the standard transport for local MCP servers (Claude Desktop, Cursor).
    mcp.run(transport="stdio")
