"""
overhired — parser tool server

Plain Python tool functions exposed to the LLM via OpenAI-compatible tool calling.
No MCP SDK required — tools are called directly by the agentic loop in ai_client.py.

Tools:
  run_parser    - exec a Python extract(text) function against page text
  save_parser   - write a parser to ~/.overhired/parsers/{domain}.py
  read_parser   - read an existing cached parser
  list_parsers  - list all cached parsers with metadata
  delete_parser - force regeneration by removing a cached parser
"""

from __future__ import annotations

import json
import traceback
from datetime import date
from pathlib import Path
from typing import Any

PARSERS_DIR = Path("~/.overhired/parsers").expanduser()

# ── OpenAI-format tool definitions sent to the LLM ───────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_parser",
            "description": (
                "Execute a Python function extract(text: str) -> dict against page text. "
                "Returns the extracted dict or an error string. "
                "Use this to test your parser before saving it. "
                "The function MUST return a dict with keys: title, company, description, location."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python source code defining a function: "
                            "def extract(text: str) -> dict. "
                            "Must return {'title': ..., 'company': ..., 'description': ..., 'location': ...}."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "The job page innerText to test the parser against.",
                    },
                },
                "required": ["code", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_parser",
            "description": (
                "Save a working parser for a domain. Call this ONLY after run_parser "
                "confirms the parser returns a non-empty title. "
                "The parser is cached and will be reused for all future scans of this domain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain the parser is for, e.g. 'linkedin.com' or 'seek.co.nz'.",
                    },
                    "code": {
                        "type": "string",
                        "description": "The same Python code passed to run_parser that produced a valid result.",
                    },
                },
                "required": ["domain", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_parser",
            "description": "Read the source of an existing cached parser for a domain.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_parsers",
            "description": "List all cached parsers with their domain, file size, and last-modified date.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_parser",
            "description": "Delete a cached parser to force regeneration on the next scan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                },
                "required": ["domain"],
            },
        },
    },
]

# ── Tool implementations ───────────────────────────────────────────────────────

_SAFE_BUILTINS = {
    # Built-in functions
    "abs", "all", "any", "bool", "chr", "dict", "enumerate", "filter",
    "float", "frozenset", "int", "isinstance", "issubclass", "iter", "len",
    "list", "map", "max", "min", "next", "ord", "print", "range", "repr",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
    "type", "zip",
    # Exception types — needed for normal Python code (raise/except)
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "StopIteration", "NotImplementedError",
    "AssertionError",
}


def _restricted_globals() -> dict:
    """Minimal __builtins__ — no file I/O, no subprocess, no imports."""
    import builtins
    safe = {name: getattr(builtins, name) for name in _SAFE_BUILTINS if hasattr(builtins, name)}
    safe["__import__"] = _blocked_import
    return {"__builtins__": safe}


def _blocked_import(name: str, *args: Any, **kwargs: Any) -> None:
    raise ImportError(f"import '{name}' is not allowed in parser code")


def run_parser(code: str, text: str) -> dict:
    """Execute parser code in a restricted namespace and return the result."""
    try:
        ns = _restricted_globals()
        exec(compile(code, "<parser>", "exec"), ns)  # noqa: S102
        extract_fn = ns.get("extract")
        if not callable(extract_fn):
            return {"error": "No callable 'extract' function found in code."}
        result = extract_fn(text)
        if not isinstance(result, dict):
            return {"error": f"extract() returned {type(result).__name__}, expected dict."}
        return {
            "title":       str(result.get("title", "")),
            "company":     str(result.get("company", "")),
            "description": str(result.get("description", "")),
            "location":    str(result.get("location", "")),
        }
    except Exception:
        return {"error": traceback.format_exc(limit=5)}


def save_parser(domain: str, code: str) -> dict:
    """Write a parser to ~/.overhired/parsers/{domain}.py."""
    PARSERS_DIR.mkdir(parents=True, exist_ok=True)
    safe_domain = _safe_domain(domain)
    path = PARSERS_DIR / f"{safe_domain}.py"
    header = (
        f"# Generated: {date.today()}  Domain: {safe_domain}\n"
        f"# To regenerate: delete this file and scan a {safe_domain} job page.\n\n"
    )
    path.write_text(header + code, encoding="utf-8")
    return {"saved": str(path)}


def read_parser(domain: str) -> dict:
    path = PARSERS_DIR / f"{_safe_domain(domain)}.py"
    if not path.exists():
        return {"error": f"No cached parser for {domain}."}
    return {"code": path.read_text(encoding="utf-8")}


def list_parsers() -> dict:
    PARSERS_DIR.mkdir(parents=True, exist_ok=True)
    parsers = []
    for p in sorted(PARSERS_DIR.glob("*.py")):
        stat = p.stat()
        parsers.append({
            "domain":    p.stem,
            "bytes":     stat.st_size,
            "modified":  date.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return {"parsers": parsers, "count": len(parsers)}


def delete_parser(domain: str) -> dict:
    path = PARSERS_DIR / f"{_safe_domain(domain)}.py"
    if path.exists():
        path.unlink()
        return {"deleted": str(path)}
    return {"error": f"No cached parser for {domain}."}


def _safe_domain(domain: str) -> str:
    """Strip leading www. and sanitize for use as a filename."""
    domain = domain.lower().replace("www.", "", 1)
    return "".join(c if c.isalnum() or c in ".-_" else "_" for c in domain)


# ── Dispatch map used by the agentic loop ─────────────────────────────────────

TOOL_FUNCTIONS: dict[str, Any] = {
    "run_parser":    run_parser,
    "save_parser":   save_parser,
    "read_parser":   read_parser,
    "list_parsers":  list_parsers,
    "delete_parser": delete_parser,
}
