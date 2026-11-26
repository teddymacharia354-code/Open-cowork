"""Anthropic Claude (optional, paid). Only loaded when ANTHROPIC_API_KEY is set.

Translates the internal OpenAI-style message format to the Messages API.
Simple -> haiku, complex -> sonnet. Override with CLAUDE_MODEL / CLAUDE_COMPLEX_MODEL
(e.g. an opus model id)."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional, Sequence

import requests

from config import get_config
from providers.base import ProviderError, ban_model, is_banned, map_status

PROVIDER_NAME = "claude"
ENV_KEY = "ANTHROPIC_API_KEY"
FREE = False
LOCAL = False
PAID = True
_API = "https://api.anthropic.com/v1"
_FALLBACK = {"simple": "claude-haiku-4-5", "complex": "claude-sonnet-4-5"}
_cache: tuple[float, list[str]] = (0.0, [])


def _key() -> str:
    return os.environ.get(ENV_KEY, "").strip()


def _headers() -> dict[str, str]:
    return {"x-api-key": _key(), "anthropic-version": "2023-06-01", "content-type": "application/json"}


def available() -> bool:
    """True if an Anthropic API key is configured."""
    return bool(_key())


def models(force: bool = False, timeout: float = 3.0) -> list[str]:
    """List available Claude model ids (cached 5 min)."""
    global _cache
    if _cache[1] and not force and time.time() - _cache[0] < 300:
        return _cache[1]
    try:
        r = requests.get(f"{_API}/models?limit=100", headers=_headers(), timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise ProviderError("timeout", f"unreachable: {type(e).__name__}")
    if r.status_code >= 400:
        raise map_status(r.status_code, r.text)
    ids = [m["id"] for m in r.json().get("data", []) if "id" in m]
    _cache = (time.time(), ids)
    return ids


def health(timeout: float = 3.0) -> bool:
    """Probe the API and validate the key."""
    models(force=True, timeout=timeout)
    return True


def _pick(complexity: str) -> str:
    env = os.environ.get("CLAUDE_COMPLEX_MODEL" if complexity == "complex" else "CLAUDE_MODEL", "").strip() \
        or os.environ.get("CLAUDE_MODEL", "").strip()
    if env:
        return env
    ids = _cache[1]
    want = "sonnet" if complexity == "complex" else "haiku"
    cands = sorted((i for i in ids if want in i and not is_banned(PROVIDER_NAME, i)), reverse=True)
    if cands:
        return cands[0]
    return _FALLBACK[complexity if complexity in _FALLBACK else "simple"]


def _convert(messages: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append(str(m.get("content") or ""))
        elif role == "user":
            out.append({"role": "user", "content": str(m.get("content") or "")})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                raw = fn.get("arguments")
                try:
                    inp = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
                except ValueError:
                    inp = {}
                blocks.append({"type": "tool_use", "id": tc["id"], "name": fn.get("name", ""), "input": inp})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(empty)"}]})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": str(m.get("content") or "")}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return "\n\n".join(system), out


def call(messages: Sequence[dict[str, Any]], tools: Optional[Sequence[dict[str, Any]]] = None,
         complexity: str = "simple", timeout: Optional[float] = None) -> dict[str, Any]:
    """Call the Anthropic Messages API and return the normalised response."""
    model = _pick(complexity)
    system, msgs = _convert(messages)
    payload: dict[str, Any] = {"model": model, "max_tokens": 4096, "messages": msgs}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [{"name": t["name"], "description": t["description"],
                             "input_schema": t["parameters"]} for t in tools]
    t = timeout or get_config().llm_timeout
    try:
        r = requests.post(f"{_API}/messages", headers=_headers(), data=json.dumps(payload), timeout=t)
    except requests.exceptions.Timeout:
        raise ProviderError("timeout", f"timeout after {t:.0f}s", model=model)
    except requests.exceptions.RequestException as e:
        raise ProviderError("timeout", f"unreachable: {type(e).__name__}", model=model)
    if r.status_code >= 400:
        err = map_status(r.status_code, r.text)
        err.model = model
        if err.kind == "bad_model":
            ban_model(PROVIDER_NAME, model)
        raise err
    data = r.json()
    text, calls = [], []
    for b in data.get("content", []):
        if b.get("type") == "text":
            text.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            calls.append({"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}})
    return {"content": "".join(text).strip(), "tool_calls": calls, "model": model, "provider": PROVIDER_NAME}
