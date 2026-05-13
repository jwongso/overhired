"""
overhired — unified AI client

Supports:
  - ollama     : OpenAI-compatible endpoint (default: localhost:11434)
  - openai     : api.openai.com /v1/chat/completions
  - claude     : api.anthropic.com /v1/messages  (different schema)

All paths return a plain string (the model's reply text).
generate_with_tools() runs an OpenAI-compatible tool-use agentic loop.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

OLLAMA_DEFAULT_ENDPOINT = "http://localhost:11434"
OPENAI_DEFAULT_ENDPOINT = "https://api.openai.com"
CLAUDE_DEFAULT_ENDPOINT = "https://api.anthropic.com"
CLAUDE_API_VERSION      = "2023-06-01"


class AIError(Exception):
    """Raised when the AI provider returns an error or times out."""


class AIClient:
    """Provider-agnostic text generation client.

    Usage:
        client = AIClient(cfg["ai"])
        reply  = client.generate(system_prompt, user_prompt)
    """

    def __init__(self, ai_cfg: dict[str, Any]) -> None:
        self.provider = ai_cfg.get("provider", "ollama").lower()
        self.model    = ai_cfg.get("model", "llama3.2")
        self.api_key  = ai_cfg.get("api_key", "")
        self.timeout  = float(ai_cfg.get("timeout", 120))

        raw_endpoint = ai_cfg.get("endpoint", "").rstrip("/")
        if not raw_endpoint:
            if self.provider == "claude":
                raw_endpoint = CLAUDE_DEFAULT_ENDPOINT
            elif self.provider == "openai":
                raw_endpoint = OPENAI_DEFAULT_ENDPOINT
            else:
                raw_endpoint = OLLAMA_DEFAULT_ENDPOINT
        self.endpoint = raw_endpoint

    # ── Public ────────────────────────────────────────────────────────────────

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Send a chat request and return the assistant reply as a string."""
        if self.provider == "claude":
            return self._claude(system_prompt, user_prompt)
        return self._openai_compatible(system_prompt, user_prompt)

    def generate_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict],
        tool_functions: dict,
        max_iters: int = 6,
    ) -> dict:
        """Run an OpenAI-compatible tool-use agentic loop.

        The LLM receives tool definitions and can call them repeatedly.
        The loop ends when the LLM stops calling tools, calls save_parser,
        or max_iters is reached.

        Returns:
            {
                "result": dict | None,   # last tool result (if any)
                "saved": bool,           # True if save_parser was called
                "iterations": int,
                "error": str | None,
            }
        """
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]

        last_result: dict | None = None
        saved = False

        for i in range(max_iters):
            response = self._call_with_tools(messages, tools)
            assistant_msg = response["content"]
            tool_calls = response.get("tool_calls", [])

            # Append assistant turn (with or without tool_calls)
            messages.append(assistant_msg)

            if not tool_calls:
                break

            # Execute each requested tool call and feed results back
            for tc in tool_calls:
                fn = tool_functions.get(tc["name"])
                if fn is None:
                    result = {"error": f"Unknown tool: {tc['name']}"}
                else:
                    try:
                        result = fn(**tc["arguments"])
                    except Exception as exc:
                        result = {"error": str(exc)}

                last_result = result
                if tc["name"] == "save_parser":
                    saved = True

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "name":         tc["name"],
                    "content":      json.dumps(result),
                })

            if saved:
                break

        return {
            "result":     last_result,
            "saved":      saved,
            "iterations": i + 1,
            "error":      None,
        }

    def health_check(self) -> tuple[bool, str]:
        """Return (is_reachable, model_name).

        Queries /v1/models to both verify reachability and discover the
        actual model name served by the endpoint. Falls back to the
        configured model name when the endpoint is unreachable or the
        response shape is unexpected.
        """
        try:
            headers = (
                {"x-api-key": self.api_key, "anthropic-version": CLAUDE_API_VERSION}
                if self.provider == "claude"
                else self._openai_headers()
            )
            r = httpx.get(f"{self.endpoint}/v1/models", headers=headers, timeout=5)
            if 200 <= r.status_code < 300:
                models = r.json().get("data", [])
                model_name = models[0].get("id", self.model) if models else self.model
                return True, model_name
        except Exception:
            pass
        return False, self.model

    # ── OpenAI-compatible (Ollama, llama.cpp, OpenAI) ────────────────────────

    def _openai_compatible(self, system: str, user: str) -> str:
        url = f"{self.endpoint}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "temperature": 0.7,
            "stream": False,
        }
        # Qwen3 and other reasoning models expose chain-of-thought tokens by
        # default.  Suppress them so they don't bleed into the cover letter.
        # Ollama accepts "think": false at the top level of the request body.
        if self.provider == "ollama":
            payload["think"] = False
        try:
            resp = httpx.post(
                url,
                headers=self._openai_headers(),
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise AIError(
                f"AI request timed out after {self.timeout}s. "
                "Is Ollama running? Try: ollama serve"
            )
        except httpx.HTTPStatusError as exc:
            raise AIError(f"AI provider returned {exc.response.status_code}: "
                          f"{exc.response.text[:300]}")
        except httpx.ConnectError:
            raise AIError(
                f"Cannot connect to AI endpoint {self.endpoint}. "
                "Start Ollama with: ollama serve"
            )

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as exc:
            raise AIError(f"Unexpected response shape: {exc}\n{json.dumps(data)[:300]}")

    def _openai_headers(self) -> dict:
        headers: dict = {"Content-Type": "application/json"}
        # Ollama accepts any non-empty bearer token; OpenAI requires the real key
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.provider == "ollama":
            headers["Authorization"] = "Bearer ollama"
        return headers

    def _call_with_tools(self, messages: list[dict], tools: list[dict]) -> dict:
        """Single LLM call with tool definitions. Returns normalised response dict."""
        url = f"{self.endpoint}/v1/chat/completions"
        payload = {
            "model":       self.model,
            "messages":    messages,
            "tools":       tools,
            "tool_choice": "auto",
            "stream":      False,
        }
        if self.provider == "ollama":
            payload["think"] = False
        try:
            resp = httpx.post(
                url,
                headers=self._openai_headers(),
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise AIError(f"Tool-use request timed out after {self.timeout}s.")
        except httpx.HTTPStatusError as exc:
            raise AIError(f"AI provider returned {exc.response.status_code}: "
                          f"{exc.response.text[:300]}")
        except httpx.ConnectError:
            raise AIError(f"Cannot connect to AI endpoint {self.endpoint}.")

        data = resp.json()
        try:
            choice  = data["choices"][0]
            message = choice["message"]
            raw_tcs = message.get("tool_calls") or []
            return {
                "stop_reason": "tool_use" if raw_tcs else choice.get("finish_reason", "stop"),
                "content":     message,
                "tool_calls": [
                    {
                        "id":        tc["id"],
                        "name":      tc["function"]["name"],
                        "arguments": json.loads(tc["function"]["arguments"]),
                    }
                    for tc in raw_tcs
                ],
            }
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise AIError(f"Unexpected tool response shape: {exc}\n{json.dumps(data)[:300]}")

    # ── Anthropic Claude ─────────────────────────────────────────────────────

    def _claude(self, system: str, user: str) -> str:
        url = f"{self.endpoint}/v1/messages"
        payload = {
            "model": self.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": 2048,
        }
        headers = {
            "Content-Type":      "application/json",
            "x-api-key":         self.api_key,
            "anthropic-version": CLAUDE_API_VERSION,
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise AIError(f"Claude request timed out after {self.timeout}s.")
        except httpx.HTTPStatusError as exc:
            raise AIError(f"Claude returned {exc.response.status_code}: "
                          f"{exc.response.text[:300]}")
        except httpx.ConnectError:
            raise AIError(f"Cannot connect to Claude endpoint {self.endpoint}.")

        data = resp.json()
        try:
            return data["content"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            raise AIError(f"Unexpected Claude response: {exc}\n{json.dumps(data)[:300]}")
