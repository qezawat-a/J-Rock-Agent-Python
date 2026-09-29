"""Async LLM client with tool-calling + thinking controls.

Works with any OpenAI-compatible endpoint (see providers.py). For providers
whose model lists are dynamic (openrouter, local) we query /models live.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from ..config import Settings
from .providers import PROVIDERS, provider_meta

TIMEOUT = 300.0


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    # ------------------------------------------------------------------ meta
    def _endpoint(self, provider: str | None = None, model: str | None = None) -> tuple[str, dict]:
        provider = provider or self.s.provider
        meta = provider_meta(provider)
        base = meta["base_url"]
        if provider == "local":
            base = self.s.api_keys.get("local_base_url", base)
        if provider == "anthropic":
            base = "https://api.anthropic.com"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        key = self.s.api_keys.get(provider, "")
        if not key:
            key = self.s.api_keys.get("API_KEY", "")
        if meta.get("auth") == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return base, headers

    async def list_models(self, provider: str | None = None) -> list[str]:
        provider = provider or self.s.provider
        try:
            base, headers = self._endpoint(provider)
            url = f"{base}/models"
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
                    if ids:
                        return sorted(ids)[:200]
        except Exception:
            pass
        return provider_meta(provider).get("models", [])

    # ----------------------------------------------------------------- chat
    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        *,
        provider: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
    ) -> dict:
        """One chat completion. Returns the raw response message dict."""
        provider = provider or self.s.provider
        model = model or self.s.model
        thinking = thinking or self.s.thinking
        base, headers = self._endpoint(provider)
        meta = provider_meta(provider)

        if meta.get("auth") == "anthropic":
            return await self._chat_anthropic(base, headers, model, messages,
                                              tools, thinking, temperature, max_tokens)

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            if not meta.get("supports_tool_calls") and self.s.auto_compat:
                # compat shim: force JSON-mode style tool use
                body.pop("tools")
        # thinking / reasoning effort (honored where supported)
        if thinking != "off" and meta.get("supports_thinking"):
            effort = thinking if thinking in ("low", "medium", "high") else "medium"
            if provider in ("openai", "google"):
                body["reasoning_effort"] = effort
            elif provider == "deepseek":
                body["reasoning_effort"] = "high" if effort in ("medium", "high") else effort

        url = f"{base}/chat/completions"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            raise LLMError(f"{provider}/{model} -> HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        return data["choices"][0]["message"]

    async def _chat_anthropic(self, base, headers, model, messages, tools,
                              thinking, temperature, max_tokens) -> dict:
        system = ""
        convo = []
        for m in messages:
            if m.get("role") == "system":
                system = (system + "\n" + str(m.get("content", ""))).strip()
            elif m.get("role") == "assistant":
                convo.append({"role": "assistant",
                              "content": m.get("content") or ""})
            else:
                convo.append({"role": "user", "content": m.get("content") or ""})
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": convo,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {"name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}})}
                for t in tools
            ]
        if thinking != "off":
            budget = {"low": 1024, "medium": 4096, "high": 10240}.get(thinking, 4096)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{base}/v1/messages", headers=headers, json=body)
        if r.status_code >= 400:
            raise LLMError(f"anthropic/{model} -> HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        # normalize to openai-ish message
        out: dict[str, Any] = {"role": "assistant", "content": ""}
        for block in data.get("content", []):
            if block.get("type") == "text":
                out["content"] += block.get("text", "")
            elif block.get("type") == "tool_use":
                out.setdefault("tool_calls", []).append(
                    {"function": {"name": block.get("name", ""),
                                  "arguments": json.dumps(block.get("input", {}))}}
                )
        return out
