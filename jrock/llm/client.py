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
        # The generic API_KEY fallback only applies to the provider it was set
        # for. Otherwise a custom base URL with no key of its own would receive
        # the active provider's credential.
        if not key and provider == self.s.provider:
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
            # Anthropic serves the model list under /v1; everyone else under the
            # base URL we already store.
            url = f"{base}/v1/models" if provider == "anthropic" else f"{base}/models"
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
        system, convo = _to_anthropic(messages)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": convo,
        }
        if system:
            body["system"] = system
        if convo and convo[0]["role"] != "user":
            # Anthropic requires the first turn to come from the user.
            convo.insert(0, {"role": "user", "content": "(continuing)"})
        if tools:
            body["tools"] = [
                {"name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}})}
                for t in tools
            ]
        thinking_on = thinking != "off"
        if thinking_on:
            budget = {"low": 1024, "medium": 4096, "high": 10240}.get(thinking, 4096)
            # budget_tokens must be at least 1024 and strictly below max_tokens,
            # and extended thinking is incompatible with a custom temperature.
            budget = max(1024, budget)
            body["max_tokens"] = max(int(max_tokens), budget + 1024)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        else:
            body["temperature"] = temperature
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
                    {"id": block.get("id") or f"toolu_{len(out.get('tool_calls', []))}",
                     "type": "function",
                     "function": {"name": block.get("name", ""),
                                  "arguments": json.dumps(block.get("input", {}))}}
                )
        return out


def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert our OpenAI-shaped history into Anthropic's block format.

    Tool calls become ``tool_use`` blocks and tool results become
    ``tool_result`` blocks inside a user turn, so the model sees a real
    call/result pair instead of flattened prose.
    """
    system_parts: list[str] = []
    convo: list[dict] = []

    def push(role: str, content) -> None:
        if convo and convo[-1]["role"] == role and isinstance(convo[-1]["content"], list) \
                and isinstance(content, list):
            convo[-1]["content"].extend(content)
        else:
            convo.append({"role": role, "content": content})

    for m in messages:
        role = m.get("role")
        if role == "system":
            txt = str(m.get("content") or "").strip()
            if txt:
                system_parts.append(txt)
            continue

        if role == "assistant":
            blocks: list[dict] = []
            text = str(m.get("content") or "")
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    inp = {}
                blocks.append({"type": "tool_use",
                               "id": tc.get("id") or f"toolu_{len(blocks)}",
                               "name": fn.get("name", ""),
                               "input": inp if isinstance(inp, dict) else {}})
            if blocks:
                push("assistant", blocks)
            continue

        if role == "tool":
            block = {"type": "tool_result",
                     "tool_use_id": m.get("tool_call_id") or "call_0",
                     "content": str(m.get("content") or "(no output)")}
            push("user", [block])
            continue

        text = str(m.get("content") or "")
        if text.strip():
            push("user", [{"type": "text", "text": text}])

    return "\n\n".join(system_parts), convo
