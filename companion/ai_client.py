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
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

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
        self.timeout      = float(ai_cfg.get("timeout", 120))
        # tool_timeout is used for agentic tool-calling loops, which can be
        # much slower (multiple model calls, large context).  Defaults to 3x
        # the regular timeout if not explicitly set.
        self.tool_timeout = float(
            ai_cfg.get("tool_timeout", max(self.timeout * 3, 360))
        )

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

    def generate(self, system_prompt: str, user_prompt: str, *, timeout: float | None = None) -> str:
        """Send a chat request and return the assistant reply as a string."""
        if self.provider == "claude":
            return self._claude(system_prompt, user_prompt)
        return self._openai_compatible(system_prompt, user_prompt, timeout=timeout)

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
        last_text: str = ""

        for i in range(max_iters):
            t_iter = time.monotonic()
            response = self._call_with_tools(messages, tools)
            assistant_msg = response["content"]
            tool_calls = response.get("tool_calls", [])
            last_text = assistant_msg.get("content") or ""
            iter_elapsed = time.monotonic() - t_iter

            logger.info("[tools] iter=%d elapsed=%.1fs tool_calls=%s text=%r",
                        i + 1, iter_elapsed,
                        [tc["name"] for tc in tool_calls],
                        last_text if last_text else "")

            # Log each tool call's arguments so we can see what the LLM generated
            for tc in tool_calls:
                args = tc["arguments"]
                if tc["name"] == "run_parser":
                    code_preview = args.get("code", "").replace("\n", "↵")
                    logger.debug("[tools]   run_parser code_preview=%r", code_preview)
                elif tc["name"] == "save_parser":
                    logger.info("[tools]   save_parser domain=%r", args.get("domain"))
                else:
                    logger.debug("[tools]   %s args=%r", tc["name"], str(args))

            messages.append(assistant_msg)

            if not tool_calls:
                logger.info("[tools] iter=%d — no tool calls, LLM stopped (finish_reason=%r)",
                            i + 1, response.get("stop_reason"))
                break

            # Execute each tool call, log arguments + result
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

                # Log tool result
                if tc["name"] == "run_parser":
                    logger.info("[tools]   run_parser result: title=%r company=%r location=%r err=%r",
                                result.get("title", ""),
                                result.get("company", ""),
                                result.get("location", ""),
                                result.get("error", ""))
                else:
                    logger.info("[tools]   %s result=%r", tc["name"], str(result))

                if tc["name"].startswith("save_"):
                    saved = True
                    logger.info("[tools] save_parser called — domain=%s parser saved ✓",
                                tc["arguments"].get("domain", "<missing>"))

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
            "iterations": i + 1,  # type: ignore[possibly-undefined]
            "last_text":  last_text,
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

    def _openai_compatible(self, system: str, user: str, *, timeout: float | None = None) -> str:
        url = f"{self.endpoint}/v1/chat/completions"
        t = timeout if timeout is not None else self.timeout
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "temperature": 0.7,
            "stream": False,
        }
        if self.provider == "ollama":
            payload["think"] = True   # expose chain-of-thought in companion.log

        logger.debug("[llm] generate request model=%s timeout=%ss prompt_chars=%d",
                     self.model, t, len(system) + len(user))
        logger.debug("[llm] system_prompt=%r", system)
        logger.debug("[llm] user_prompt=%r", user)

        t0 = time.monotonic()
        try:
            resp = httpx.post(url, headers=self._openai_headers(), json=payload, timeout=t)
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise AIError(f"AI request timed out after {t}s. Is Ollama running? Try: ollama serve")
        except httpx.HTTPStatusError as exc:
            raise AIError(f"AI provider returned {exc.response.status_code}: {exc.response.text[:300]}")
        except httpx.ConnectError:
            raise AIError(f"Cannot connect to AI endpoint {self.endpoint}. Start Ollama with: ollama serve")

        elapsed = time.monotonic() - t0
        data = resp.json()

        # Log Ollama eval stats when available
        if "eval_count" in data or "prompt_eval_count" in data:
            logger.info("[llm] ollama stats: prompt_tokens=%s eval_tokens=%s elapsed=%.1fs",
                        data.get("prompt_eval_count", "?"),
                        data.get("eval_count", "?"),
                        elapsed)
        else:
            logger.info("[llm] generate done in %.1fs", elapsed)

        try:
            reply = data["choices"][0]["message"]["content"].strip()
            logger.debug("[llm] reply=%r", reply)
            return reply
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
            payload["think"] = True   # expose chain-of-thought in companion.log

        msg_summary = [{"role": m["role"], "chars": len(str(m.get("content", "")))}
                       for m in messages]
        logger.debug("[llm] tool-call request model=%s timeout=%ss messages=%s tools=%s",
                     self.model, self.tool_timeout,
                     msg_summary,
                     [t["function"]["name"] for t in tools])

        # Log the last user/tool message fully so we can see what context LLM has
        for m in reversed(messages):
            if m["role"] in ("user", "tool"):
                logger.debug("[llm] last context message role=%s content=%r",
                             m["role"], str(m.get("content", "")))
                break

        t0 = time.monotonic()
        try:
            resp = httpx.post(url, headers=self._openai_headers(), json=payload,
                              timeout=self.tool_timeout)
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise AIError(f"Tool-use request timed out after {self.tool_timeout}s.")
        except httpx.HTTPStatusError as exc:
            raise AIError(f"AI provider returned {exc.response.status_code}: "
                          f"{exc.response.text[:300]}")
        except httpx.ConnectError:
            raise AIError(f"Cannot connect to AI endpoint {self.endpoint}.")

        elapsed = time.monotonic() - t0
        data = resp.json()

        # Log Ollama eval stats when available
        if "eval_count" in data or "prompt_eval_count" in data:
            logger.info("[llm] tool-call done: elapsed=%.1fs prompt_tokens=%s eval_tokens=%s",
                        elapsed,
                        data.get("prompt_eval_count", "?"),
                        data.get("eval_count", "?"))
        else:
            logger.info("[llm] tool-call done in %.1fs", elapsed)

        try:
            choice  = data["choices"][0]
            message = choice["message"]
            raw_tcs = message.get("tool_calls") or []

            # Log assistant text reply if any
            text_content = message.get("content") or ""
            if text_content:
                logger.debug("[llm] assistant text=%r", text_content)

            # Log each raw tool call from the LLM
            for tc in raw_tcs:
                fn_name = tc["function"]["name"]
                fn_args_raw = tc["function"]["arguments"]
                logger.debug("[llm] raw tool_call: name=%r arguments=%r",
                             fn_name, fn_args_raw)

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
