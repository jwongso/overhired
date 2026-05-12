"""
overhired — unified AI client

Supports:
  - ollama     : OpenAI-compatible endpoint (default: localhost:11434)
  - openai     : api.openai.com /v1/chat/completions
  - claude     : api.anthropic.com /v1/messages  (different schema)

All paths return a plain string (the model's reply text).
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

    def health_check(self) -> bool:
        """Return True if the endpoint responds to a lightweight probe."""
        try:
            if self.provider == "claude":
                url = f"{self.endpoint}/v1/models"
                headers = {"x-api-key": self.api_key,
                           "anthropic-version": CLAUDE_API_VERSION}
            else:
                url = f"{self.endpoint}/v1/models"
                headers = self._openai_headers()
            r = httpx.get(url, headers=headers, timeout=5)
            return 200 <= r.status_code < 300
        except Exception:
            return False

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
