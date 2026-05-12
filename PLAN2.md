# PLAN2 - Adaptive LLM-Generated Site Parsers with MCP

## Problem Statement

Every job board has its own DOM structure. Maintaining hand-written scrapers per site
(LinkedIn bpr-guid JSON, Seek data-automation attributes, etc.) is fragile - sites
change their markup without notice, and each new job board requires custom code.

## Core Idea

Replace per-site scraper code with **LLM-generated parser scripts** that are cached
on disk. The LLM writes and tests its own parser via an **MCP server** before saving it.
Once saved, the parser runs instantly without any LLM involvement.

---

## Does the MCP Server Need to Be Python?

No. MCP (Model Context Protocol) is a **language-agnostic JSON-RPC protocol**.
Official SDKs exist for Python and TypeScript. Community SDKs exist for Go, Rust, etc.

Since the companion is already Python, use the **Python MCP SDK** (`mcp` package from
Anthropic). It handles the protocol, you just write the tool functions.

```
pip install mcp
```

The MCP server can run as:
- **stdio** (subprocess) - companion spawns it, communicates via stdin/stdout pipes.
  Simple, no extra ports, dies when companion dies. Best for this use case.
- **HTTP/SSE** - runs as a separate network service. Useful if multiple clients
  need the same tools. Overkill here.

---

## How Does the LLM Interact with MCP Tools?

This is the key question. The LLM does NOT directly call or execute anything.
The flow is:

```
1. Companion builds a chat request that includes tool definitions (JSON schema)
   and sends it to the LLM (Ollama/OpenAI/Claude API).

2. LLM reads the task + available tools. Instead of replying with text, it
   replies with a tool_call:
   {
     "tool": "run_parser",
     "arguments": { "code": "def extract(text):\n  ...", "text": "..." }
   }

3. Companion receives the tool_call, forwards it to the MCP server.

4. MCP server executes the tool (runs the Python code), returns result:
   { "title": "Senior C++ Developer", "company": "CompuGroup Medical", ... }

5. Companion feeds the result back to the LLM as a tool_result message.

6. LLM reads the result, decides what to do next:
   - Result looks correct → call save_parser tool
   - Result is wrong (title = "Jobs where you're a top applicant") → fix code, call run_parser again

7. Loop repeats until LLM calls save_parser or hits max iterations.
```

The LLM never executes code. It only *requests* tool calls. The companion (MCP client)
is the one that actually runs them. The LLM sees inputs and outputs, reasons about them,
and decides what to request next. This is standard "tool use" / "function calling" -
supported by Qwen3, Claude, OpenAI GPT-4, and most modern models via Ollama.

---

## Full Architecture

```
Browser Extension
  scrapeJobFromPage() - grabs document.body.innerText (~12KB) + domain
  POST /extract { domain, page_text } to companion
        |
        v
Companion (/extract endpoint)
  |
  +-- Cache hit: ~/.overhired/parsers/{domain}.py exists
  |     run parser(page_text) -> { title, company, description, location }
  |     if result ok: return immediately (no LLM, <10ms)
  |     if result empty/exception: delete parser, fall through
  |
  +-- Cache miss or broken parser:
        Spawn MCP server subprocess
        Build LLM request with tool definitions
        Start agentic loop (max 5 iterations):
          LLM receives: page_text + domain + available tools
          LLM calls: run_parser(code, page_text)
          MCP executes, returns result
          LLM checks result:
            wrong → fix code → call run_parser again
            right → call save_parser(domain, code)
          Loop ends when save_parser called or max iterations reached
        Return extracted { title, company, description, location }

MCP Server (subprocess, stdio transport)
  Tools:
    run_parser(code, text)      - exec Python code, call extract(text), return result
    save_parser(domain, code)   - write ~/.overhired/parsers/{domain}.py
    read_parser(domain)         - read existing parser (for LLM to inspect/modify)
    list_parsers()              - list all cached parsers with metadata
    delete_parser(domain)       - force regeneration next time
```

---

## File Structure

```
companion/
  main.py              - existing FastAPI app, add POST /extract
  ai_client.py         - extend to support tool_use agentic loop
  extractor.py         - NEW: orchestrator (cache check, start MCP, run loop)
  mcp_server.py        - NEW: MCP server with run_parser/save_parser tools
  parsers/             - gitignored, user-local, auto-generated
    linkedin.com.py
    seek.co.nz.py
    indeed.com.py
    ...
```

---

## Parser File Format

Fixed contract - every parser has the same interface:

```python
# ~/.overhired/parsers/linkedin.com.py
# Generated: 2026-05-12  Domain: linkedin.com
# To regenerate: delete this file and scan a LinkedIn job page again.

def extract(text: str) -> dict:
    """Extract job info from linkedin.com innerText."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    title = company = location = description = ''

    for i, line in enumerate(lines):
        if line == 'About the job':
            description = '\n'.join(lines[i+1:i+150]).strip()
            break

    # Title and company are reliably in the first visible lines on LinkedIn
    if lines: title   = lines[0]
    if len(lines) > 2: company = lines[2]

    return {
        'title':       title,
        'company':     company,
        'description': description[:6000],
        'location':    location,
    }
```

- Input: `text: str` (document.body.innerText, capped 12KB)
- Output: `dict` with keys `title`, `company`, `description`, `location`
- Any exception or empty `title` = broken, triggers regeneration

---

## MCP Server Implementation (mcp_server.py)

```python
import asyncio
import traceback
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

PARSERS_DIR = Path('~/.overhired/parsers').expanduser()
server = Server('overhired-parser-tools')

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name='run_parser',
            description=(
                'Execute a Python extract(text) function against page text. '
                'Returns the dict result or an error string. '
                'Use this to test your parser before saving it.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'code': {'type': 'string', 'description': 'Python code defining extract(text: str) -> dict'},
                    'text': {'type': 'string', 'description': 'Page innerText to test against'},
                },
                'required': ['code', 'text'],
            },
        ),
        types.Tool(
            name='save_parser',
            description='Save a working parser to disk. Call only after run_parser confirms correct output.',
            inputSchema={
                'type': 'object',
                'properties': {
                    'domain':  {'type': 'string', 'description': 'Domain e.g. linkedin.com'},
                    'code':    {'type': 'string', 'description': 'The verified Python parser code'},
                },
                'required': ['domain', 'code'],
            },
        ),
        types.Tool(
            name='read_parser',
            description='Read an existing cached parser for a domain.',
            inputSchema={
                'type': 'object',
                'properties': {
                    'domain': {'type': 'string'},
                },
                'required': ['domain'],
            },
        ),
        types.Tool(
            name='list_parsers',
            description='List all cached parsers with their file sizes and dates.',
            inputSchema={'type': 'object', 'properties': {}},
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == 'run_parser':
        code = arguments['code']
        text = arguments['text']
        namespace = {}
        try:
            exec(compile(code, '<llm_parser>', 'exec'), namespace)
            fn = namespace.get('extract')
            if not callable(fn):
                return [types.TextContent(type='text', text='ERROR: no extract() function found in code')]
            result = fn(text)
            if not isinstance(result, dict):
                return [types.TextContent(type='text', text=f'ERROR: extract() must return dict, got {type(result)}')]
            return [types.TextContent(type='text', text=str(result))]
        except Exception:
            return [types.TextContent(type='text', text=f'ERROR:\n{traceback.format_exc()}')]

    if name == 'save_parser':
        domain = arguments['domain'].replace('www.', '')
        code   = arguments['code']
        PARSERS_DIR.mkdir(parents=True, exist_ok=True)
        path = PARSERS_DIR / f'{domain}.py'
        header = (
            f'# Generated by overhired MCP parser agent\n'
            f'# Domain: {domain}\n'
            f'# To regenerate: delete this file and scan again.\n\n'
        )
        path.write_text(header + code)
        return [types.TextContent(type='text', text=f'Saved to {path}')]

    if name == 'read_parser':
        domain = arguments['domain'].replace('www.', '')
        path = PARSERS_DIR / f'{domain}.py'
        if not path.exists():
            return [types.TextContent(type='text', text=f'No parser cached for {domain}')]
        return [types.TextContent(type='text', text=path.read_text())]

    if name == 'list_parsers':
        if not PARSERS_DIR.exists():
            return [types.TextContent(type='text', text='No parsers cached yet.')]
        files = sorted(PARSERS_DIR.glob('*.py'))
        lines = [f'{f.name}  ({f.stat().st_size} bytes)' for f in files]
        return [types.TextContent(type='text', text='\n'.join(lines) or 'Empty.')]

    return [types.TextContent(type='text', text=f'Unknown tool: {name}')]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == '__main__':
    asyncio.run(main())
```

---

## Agentic Loop in extractor.py

The companion spawns the MCP server as a subprocess and manages the tool-use loop:

```python
import asyncio
import json
import subprocess
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import stdio_client

PARSERS_DIR = Path('~/.overhired/parsers').expanduser()
MAX_ITERATIONS = 5

async def extract_with_mcp(domain: str, page_text: str, ai_client) -> dict:
    """Run agentic loop: LLM writes parser, MCP server tests it, saves when correct."""

    # Start MCP server subprocess
    server_proc = await asyncio.create_subprocess_exec(
        'python', 'mcp_server.py',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    async with stdio_client(server_proc.stdin, server_proc.stdout) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Get tool definitions to pass to LLM
            tools_result = await session.list_tools()
            tools = [t.model_dump() for t in tools_result.tools]

            system = (
                'You are a web scraping expert. '
                'You write Python parser functions and test them using the run_parser tool. '
                'Only call save_parser once run_parser returns a correct title and company. '
                'Be concise - no explanation, just tool calls.'
            )
            user = (
                f'Domain: {domain}\n\n'
                f'Page text (innerText, first 12KB):\n{page_text}\n\n'
                f'Task: Write an extract(text: str) -> dict function that reliably extracts '
                f'title, company, description, location from any job page on {domain}. '
                f'Test it with run_parser first. Fix until correct. Then save_parser.'
            )

            messages = [{'role': 'user', 'content': user}]
            extracted = {}

            for _ in range(MAX_ITERATIONS):
                response = ai_client.generate_with_tools(system, messages, tools)

                if response.get('stop_reason') == 'tool_use':
                    for block in response.get('tool_calls', []):
                        tool_name = block['name']
                        tool_args = block['arguments']

                        # Forward tool call to MCP server
                        result = await session.call_tool(tool_name, tool_args)
                        result_text = result.content[0].text if result.content else ''

                        # If save_parser was called, we are done
                        if tool_name == 'save_parser':
                            # Run the saved parser to get the actual extracted values
                            parser_path = PARSERS_DIR / f"{domain.replace('www.', '')}.py"
                            extracted = _run_cached_parser(parser_path, page_text) or {}
                            break

                        # Feed tool result back to LLM
                        messages.append({'role': 'assistant', 'content': response['content']})
                        messages.append({'role': 'tool', 'tool_use_id': block['id'], 'content': result_text})
                else:
                    # LLM stopped without saving - extract from its text response as fallback
                    break

                if extracted:
                    break

    server_proc.terminate()
    return extracted


def _run_cached_parser(path: Path, text: str) -> dict | None:
    namespace = {}
    try:
        exec(compile(path.read_text(), str(path), 'exec'), namespace)
        result = namespace['extract'](text)
        if isinstance(result, dict) and result.get('title'):
            return result
    except Exception as e:
        print(f'[overhired] Parser {path.name} failed: {e}')
    return None


def extract(domain: str, page_text: str, ai_client) -> dict:
    """Public API - sync wrapper around the async agentic loop."""
    domain = domain.replace('www.', '')
    parser_path = PARSERS_DIR / f'{domain}.py'

    # Fast path: cached parser exists
    if parser_path.exists():
        result = _run_cached_parser(parser_path, page_text)
        if result:
            return result
        print(f'[overhired] Parser for {domain} failed, regenerating via MCP...')
        parser_path.unlink()

    # Slow path: MCP agentic loop
    print(f'[overhired] No parser for {domain} - starting MCP agent...')
    return asyncio.run(extract_with_mcp(domain, page_text, ai_client))
```

---

## LLM Tool Use - Which Models Support It?

Tool use (function calling) is not universal. Supported models:

| Provider | Model | Tool use |
|----------|-------|----------|
| Anthropic | Claude 3+ | Native, excellent |
| OpenAI | GPT-4, GPT-4o | Native, reliable |
| Ollama | Qwen3-8B | Yes, works well |
| Ollama | Llama 3.1+ | Yes |
| Ollama | Mistral 7B | Partial, unreliable |
| Ollama | older models | No |

Qwen3-8B (the user's current model) **supports tool use** via Ollama's OpenAI-compatible
API. The `tools` parameter in the chat completion request is standard.

The companion's `ai_client.py` needs a new method `generate_with_tools()` that handles
the multi-turn loop (send tools, receive tool_call, send tool_result, repeat).

---

## ai_client.py Extension

Add `generate_with_tools()` to `AIClient`:

```python
def generate_with_tools(self, system: str, messages: list, tools: list) -> dict:
    """Single LLM turn that may return tool_calls. Caller manages the loop."""
    if self.provider == 'claude':
        return self._claude_with_tools(system, messages, tools)
    return self._openai_with_tools(system, messages, tools)

def _openai_with_tools(self, system: str, messages: list, tools: list) -> dict:
    url = f'{self.endpoint}/v1/chat/completions'
    payload = {
        'model': self.model,
        'messages': [{'role': 'system', 'content': system}] + messages,
        'tools': [{'type': 'function', 'function': t} for t in tools],
        'tool_choice': 'auto',
    }
    resp = httpx.post(url, headers=self._openai_headers(), json=payload, timeout=self.timeout)
    resp.raise_for_status()
    data = resp.json()
    choice = data['choices'][0]
    message = choice['message']
    tool_calls = message.get('tool_calls', [])
    return {
        'stop_reason': 'tool_use' if tool_calls else choice['finish_reason'],
        'content': message,
        'tool_calls': [
            {'id': tc['id'], 'name': tc['function']['name'],
             'arguments': json.loads(tc['function']['arguments'])}
            for tc in tool_calls
        ],
    }
```

---

## New Companion Endpoint

Add to `main.py`:

```python
class ExtractRequest(BaseModel):
    domain:    str
    page_text: str

class ExtractResponse(BaseModel):
    title:       str = ''
    company:     str = ''
    description: str = ''
    location:    str = ''

@app.post('/extract', response_model=ExtractResponse)
def extract_job(req: ExtractRequest, _: None = Depends(_require_token)):
    result = extractor.extract(req.domain, req.page_text[:12000], AI)
    return ExtractResponse(**result)
```

---

## Extension Changes

### scrapeJobFromPage() - simplified to 15 lines

```javascript
function scrapeJobFromPage() {
  const ATS_PATTERNS = [
    { name: 'greenhouse',  pattern: /greenhouse\.io|boards\.greenhouse/i },
    { name: 'ashby',       pattern: /ashbyhq\.com/i },
    { name: 'workable',    pattern: /workable\.com/i },
    { name: 'lever',       pattern: /jobs\.lever\.co/i },
    { name: 'linkedin',    pattern: /linkedin\.com/i },
  ];
  const url = window.location.href;
  const ats = ATS_PATTERNS.find(p => p.pattern.test(url))?.name || 'generic';
  const domain = window.location.hostname.replace(/^www\./, '');
  const text = document.body.innerText.slice(0, 12000);
  return { domain, page_text: text, ats };
}
```

### scanPage() - calls /extract instead of parsing locally

The service worker forwards the /extract call to the companion (same pattern as
/generate today). The popup waits for the response and populates title/company/desc.

---

## Reliability & Fallback Chain

```
1. Cached parser exists, returns non-empty title    -> instant (~0ms, no LLM)
2. Parser missing or broken                         -> MCP agentic loop (~15-60s)
3. MCP loop saves parser, parser runs ok            -> return result + fast next time
4. MCP loop fails (model does not support tools)    -> one-shot LLM extraction fallback
5. Everything fails                                 -> user fills title/company manually
```

---

## Parser Lifecycle

| Event | Action |
|-------|--------|
| First scan of new domain | MCP agent generates + tests + saves parser |
| Subsequent scans (parser works) | Run parser directly, no LLM (~0ms) |
| Site changes structure (empty title) | Delete parser, re-run MCP agent |
| User clicks "Regenerate" in Settings | DELETE /parsers/{domain}, next scan regenerates |
| Parser crashes | Exception caught, treated as missing, regenerate |

Parsers live in `~/.overhired/parsers/` - gitignored, user-local.
Companion startup log lists all cached parsers.

---

## Implementation Order

### Phase 1 - MCP Server (start here)
1. `pip install mcp` in companion venv
2. Write `companion/mcp_server.py` - four tools: run_parser, save_parser, read_parser, list_parsers
3. Test the MCP server standalone:
   `echo '{"method":"tools/list"}' | python mcp_server.py`
   Or use MCP Inspector: `npx @modelcontextprotocol/inspector python mcp_server.py`

### Phase 2 - Tool Use in ai_client.py
4. Add `generate_with_tools()` to `AIClient`
5. Test with Qwen3 via Ollama - verify tool_call responses work

### Phase 3 - Extractor Orchestrator
6. Write `companion/extractor.py` with cache check + MCP agentic loop
7. Test end-to-end: delete all cached parsers, scan LinkedIn, watch agent work

### Phase 4 - Companion Endpoint
8. Add `POST /extract` to `companion/main.py`
9. Wire auth token check (same as /generate)

### Phase 5 - Extension
10. Simplify `scrapeJobFromPage()` in popup.js
11. Route scan through /extract via service_worker.js
12. Add loading state: "Scanning... (learning this site for the first time)"

### Phase 6 - Polish
13. Add "Regenerate parser" button in Settings tab
14. Companion startup: print list of cached parsers
15. Test on: LinkedIn, Seek, Indeed, Greenhouse, Lever, Glassdoor

---

## Learning Resources for MCP

- MCP spec: https://modelcontextprotocol.io
- Python SDK: https://github.com/modelcontextprotocol/python-sdk
- MCP Inspector (test your server interactively):
  `npx @modelcontextprotocol/inspector python mcp_server.py`
- Tool use with Ollama (OpenAI-compatible):
  https://ollama.com/blog/tool-support

The MCP Inspector is particularly useful - it gives you a web UI to call your
MCP server tools manually before connecting any LLM to it.

---

## Notes

- Qwen3-8B supports tool use via Ollama - confirmed working
- document.body.innerText is universally available, no per-site code needed
- The MCP server is stateless - safe to spawn per-request or keep alive
- Generated parsers are plain Python, no dependencies, human-readable, easy to edit by hand
- The 12KB cap on page_text fits comfortably in Qwen3's 32K context window
- One-shot fallback (no MCP) should be kept as safety net for models without tool support
