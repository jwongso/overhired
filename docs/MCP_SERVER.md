# MCP Server

`companion/mcp_server.py` is a real [Model Context Protocol](https://modelcontextprotocol.io) server.
It exposes the same parser tools used by the companion's internal agentic loop, but over the standard
MCP protocol — so any MCP client can connect to it.

## What is MCP?

MCP is a protocol (JSON-RPC over stdio or HTTP/SSE) that lets LLM clients discover and call tools
exposed by a server. The client spawns your server as a subprocess and talks to it over stdin/stdout.

```
MCP Client (Claude Desktop / Cursor / MCP Inspector)
      │
      │  JSON-RPC over stdio
      │
      ▼
mcp_server.py  ──► tool_server.py  ──► ~/.overhired/parsers/
```

The server is **stateless** — safe to spawn per-session or keep alive.

## Tools

| Tool | Description |
|---|---|
| `run_parser(code, text)` | Run a `def extract(text)` function against page text in a sandbox |
| `save_parser(domain, code)` | Cache the parser to `~/.overhired/parsers/{domain}.py` |
| `read_parser(domain)` | Read an existing cached parser's source |
| `list_parsers()` | List all cached parsers with size and modified date |
| `delete_parser(domain)` | Remove a cached parser to force regeneration |

## Quick Start

### 1. Install dependencies

```bash
cd companion
pip install -r requirements.txt   # includes mcp>=1.0.0
```

### 2. Test with MCP Inspector

MCP Inspector is a browser-based UI for calling your server's tools manually — no LLM needed.

```bash
npx @modelcontextprotocol/inspector python companion/mcp_server.py
```

Open http://localhost:5173 in your browser. You'll see all five tools listed.
Click any tool, fill in the arguments, and run it.

### 3. Connect to Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "overhired-parser-tools": {
      "command": "python",
      "args": ["/absolute/path/to/overhired/companion/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop. The tools appear in the 🔧 panel. Claude can now:
- Write a parser for any job board
- Test it with `run_parser`
- Save it to your local cache with `save_parser`

### 4. Connect to Cursor

Add to `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` globally):

```json
{
  "mcpServers": {
    "overhired-parser-tools": {
      "command": "python",
      "args": ["/absolute/path/to/overhired/companion/mcp_server.py"]
    }
  }
}
```

### 5. Connect to VS Code (GitHub Copilot)

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "overhired-parser-tools": {
      "type": "stdio",
      "command": "python",
      "args": ["${workspaceFolder}/companion/mcp_server.py"]
    }
  }
}
```

## How stdio Transport Works

This is the core of MCP for local servers. When you configure Claude Desktop with the snippet above:

1. **Spawn**: Claude Desktop runs `python mcp_server.py` as a child process.
2. **Handshake**: Client sends `initialize` → server responds with capabilities and tool list.
3. **Discovery**: Client sends `tools/list` → server returns all tool schemas (JSON Schema format).
4. **Invocation**: When the LLM decides to call a tool, client sends `tools/call` with arguments.
5. **Result**: Server executes the tool, returns result as `TextContent` or `ImageContent`.
6. **Loop**: LLM reads result, decides next action. Repeat until done.

All messages are newline-delimited JSON-RPC 2.0 on stdin/stdout. The server writes logs to stderr
(not stdout) to avoid polluting the protocol stream.

## How It Differs from the Internal Agentic Loop

| | Internal loop (`tool_server.py`) | MCP server (`mcp_server.py`) |
|---|---|---|
| Protocol | Direct Python function calls | JSON-RPC over stdio |
| Client | Companion's `ai_client.generate_with_tools()` | Any MCP client |
| Transport | In-process | Subprocess |
| Discoverability | Hardcoded in companion | Standard `tools/list` |
| Reusability | Overhired only | Any MCP-compatible tool |

Both use the same underlying tool implementations from `tool_server.py`.

## Example Session (MCP Inspector)

```
> list_parsers()
{ "parsers": [], "count": 0 }

> run_parser(
    code = "def extract(text):\n    lines = text.splitlines()\n    return {'title': lines[0], 'company': lines[1], 'description': '', 'location': ''}",
    text = "Senior Engineer\nAcme Corp\nWe are hiring..."
  )
{ "title": "Senior Engineer", "company": "Acme Corp", "description": "", "location": "" }

> save_parser(domain="acme.com", code="def extract(text):\n    ...")
{ "saved": "/home/you/.overhired/parsers/acme.com.py" }

> list_parsers()
{ "parsers": [{ "domain": "acme.com", "bytes": 312, "modified": "2026-05-13" }], "count": 1 }
```

## Extending for Other Projects

The MCP server pattern is generic. To reuse it for a different project:

1. Copy `mcp_server.py` and replace the `@mcp.tool()` functions with your own.
2. The tool implementations can be anything — file I/O, API calls, database queries.
3. Register with Claude Desktop or Cursor using the same JSON config pattern.

The `tool_server.py` → `mcp_server.py` separation keeps business logic decoupled from the
protocol layer, making both testable and reusable independently.
