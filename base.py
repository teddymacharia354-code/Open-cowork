"""Shared provider plumbing: error taxonomy, model discovery/ranking, and a
generic OpenAI-compatible client that most providers are thin wrappers around.

A provider module only has to expose ``PROVIDER_NAME``, ``ENV_KEY`` and
``call(messages, tools, complexity)``. Optional: ``available()``,
``health(timeout)``, ``models()``, ``FREE``, ``LOCAL``, ``PAID``.

Normalised response from ``call``::

    {"content": str, "tool_calls": [{"id", "name", "arguments": dict}],
     "model": str, "provider": str}
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Optional, Sequence

import requests

NON_CHAT_MARKERS = ("orpheus", "whisper", "tts", "audio", "image", "playai", "canopy", "embed",
                    "guard", "moderation", "rerank", "transcribe", "dall-e", "davinci", "babbage")

_BANNED: set[tuple[str, str]] = set()


class ProviderError(Exception):
    """Failure from a provider.

    ``kind`` is one of: rate_limit, fatal, timeout, transient, bad_model.
    """

    def __init__(self, kind: str, message: str, status: Optional[int] = None, model: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.model = model


def ban_model(provider: str, model: str) -> None:
    """Never use ``model`` of ``provider`` again in this session."""
    _BANNED.add((provider, model))


def is_banned(provider: str, model: str) -> bool:
    """True if the model was banned this session."""
    return (provider, model) in _BANNED


def map_status(status: int, text: str) -> ProviderError:
    """Translate an HTTP failure into a :class:`ProviderError`."""
    low = text.lower()
    snippet = re.sub(r"\s+", " ", text)[:200]
    if status == 429 or status == 529 or status == 413:
        return ProviderError("rate_limit", snippet, status)
    if status in (401, 403):
        return ProviderError("fatal", snippet, status)
    if "does not support tools" in low or "tools are not supported" in low or "tool use is not supported" in low:
        return ProviderError("bad_model", snippet, status)
    if "tool_use_failed" in low or "failed to call a function" in low or "failed_generation" in low:
        return ProviderError("transient", snippet, status)
    if status == 404 or (status in (400, 422) and "model" in low and
                         any(x in low for x in ("not found", "does not exist", "not supported", "decommissioned",
                                                "unavailable", "invalid model"))):
        return ProviderError("bad_model", snippet, status)
    if status in (408, 409) or status >= 500:
        return ProviderError("transient", snippet, status)
    return ProviderError("fatal", snippet, status)


def _size(model_id: str) -> float:
    m = re.search(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)b(?![A-Za-z0-9])", model_id.lower())
    return float(m.group(1)) if m else 50.0


def rank_models(ids: Sequence[str], families: Sequence[str], complexity: str) -> list[str]:
    """Order model ids: family preference, then size (small=simple, big=complex)."""
    def key(mid: str) -> tuple:
        low = mid.lower()
        fam = next((i for i, f in enumerate(families) if f.lower() in low), len(families))
        size = _size(mid)
        if complexity == "complex":
            sz = -size
        else:
            sz = (0 if size >= 7 else 1, size)
        return (fam, sz, 0 if low.endswith("latest") else 1, len(mid))
    return sorted(ids, key=key)


def parse_openai_response(data: dict[str, Any], model: str, provider: str) -> dict[str, Any]:
    """Normalise an OpenAI-style chat completion."""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("transient", f"malformed response: {str(data)[:200]}", model=model)
    content = msg.get("content") or ""
    if isinstance(content, list):  # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                args = {"__parse_error__": str(raw)}
        calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""), "arguments": args})
    return {"content": content, "tool_calls": calls, "model": model, "provider": provider}


def to_openai_tools(tools: Optional[Sequence[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
    """Wrap raw function schemas in the OpenAI ``tools`` envelope."""
    if not tools:
        return None
    return [{"type": "function", "function": t} for t in tools]


def clean_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make internal messages safe for strict OpenAI-compatible servers."""
    out = []
    for m in messages:
        m = dict(m)
        if m.get("role") == "assistant" and m.get("tool_calls") and m.get("content") is None:
            m["content"] = ""
        out.append(m)
    return out


class OpenAICompat:
    """Generic OpenAI-compatible chat provider with model auto-discovery."""

    def __init__(self, name: str, env_key: str, base_url: str, *, families: Sequence[str] = (),
                 families_complex: Optional[Sequence[str]] = None, fallback_simple: str = "",
                 fallback_complex: Optional[str] = None, keyless: bool = False, local: bool = False,
                 free: bool = True, paid: bool = False, base_url_env: str = "",
                 extra_filter: Optional[Callable[[dict[str, Any]], bool]] = None,
                 headers: Optional[dict[str, str]] = None, alt_key_env: str = "") -> None:
        self.name = name
        self.env_key = env_key
        self.base_url = base_url
        self.families = list(families)
        self.families_complex = list(families_complex) if families_complex is not None else list(families)
        self.fallback_simple = fallback_simple
        self.fallback_complex = fallback_complex or fallback_simple
        self.keyless = keyless
        self.local = local
        self.free = free
        self.paid = paid
        self.base_url_env = base_url_env
        self.extra_filter = extra_filter
        self.extra_headers = headers or {}
        self.alt_key_env = alt_key_env
        self._cache: tuple[float, list[str]] = (0.0, [])

    # ---- configuration
    def key(self) -> str:
        """API key from the environment ('' if unset)."""
        k = os.environ.get(self.env_key, "").strip()
        if not k and self.alt_key_env:
            k = os.environ.get(self.alt_key_env, "").strip()
        return k

    def url(self) -> str:
        """Base URL (env override allowed)."""
        u = (os.environ.get(self.base_url_env, "").strip() if self.base_url_env else "") or self.base_url
        return u.rstrip("/")

    def available(self) -> bool:
        """Configured? (key present, or keyless/local provider)."""
        return self.keyless or bool(self.key())

    def timeout(self, override: Optional[float]) -> float:
        """Request timeout in seconds."""
        if override:
            return override
        from config import get_config
        c = get_config()
        return c.llm_local_timeout if self.local else c.llm_timeout

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if self.key():
            h["Authorization"] = f"Bearer {self.key()}"
        return h

    def families_for(self, complexity: str) -> list[str]:
        """Preferred model-name substrings for a complexity level."""
        return self.families_complex if complexity == "complex" else self.families

    # ---- discovery
    def _get(self, url: str, timeout: float, headers: Optional[dict[str, str]] = None) -> Any:
        try:
            r = requests.get(url, headers=headers if headers is not None else self._headers(), timeout=timeout)
        except requests.exceptions.Timeout:
            raise ProviderError("timeout", "timeout")
        except requests.exceptions.RequestException as e:
            raise ProviderError("timeout", f"unreachable: {type(e).__name__}")
        if r.status_code >= 400:
            raise map_status(r.status_code, r.text)
        try:
            return r.json()
        except ValueError:
            raise ProviderError("transient", "invalid JSON from models endpoint")

    def _raw_models(self, timeout: float) -> list[dict[str, Any]]:
        data = self._get(self.url() + "/models", timeout)
        items = data.get("data", data) if isinstance(data, dict) else data
        return [m for m in items if isinstance(m, dict) and m.get("id")]

    def models(self, force: bool = False, timeout: float = 3.0) -> list[str]:
        """Discover chat-capable models (cached 5 minutes)."""
        ts, ids = self._cache
        if ids and not force and time.time() - ts < 300:
            return ids
        raw = self._raw_models(timeout)
        out = []
        for m in raw:
            mid = str(m["id"])
            if any(x in mid.lower() for x in NON_CHAT_MARKERS):
                continue
            if self.extra_filter and not self.extra_filter(m):
                continue
            out.append(mid)
        self._cache = (time.time(), out)
        return out

    def health(self, timeout: float = 3.0) -> bool:
        """Cheap liveness + auth probe (lists models)."""
        ids = self.models(force=True, timeout=timeout)
        if not ids and self.keyless:
            raise ProviderError("transient", "reachable but no models available")
        return True

    def pick_model(self, complexity: str) -> str:
        """Choose a model: env override > discovered+ranked > fallback."""
        override = os.environ.get(f"{self.name.upper()}_MODEL", "").strip()
        if override:
            return override
        ids = self._cache[1]
        disc_err: Optional[ProviderError] = None
        if not ids:
            try:
                ids = self.models(timeout=2.0)
            except ProviderError as e:
                ids, disc_err = [], e
        if ids:
            for mid in rank_models(ids, self.families_for(complexity), complexity):
                if not is_banned(self.name, mid):
                    return mid
            raise ProviderError("fatal", "all discovered models failed in this session")
        fb = self.fallback_complex if complexity == "complex" else self.fallback_simple
        if fb and not is_banned(self.name, fb):
            return fb
        if disc_err is not None and not fb:
            raise disc_err  # e.g. local server not running -> timeout/unreachable, not fatal
        raise ProviderError("fatal", "no usable model (discovery failed, fallback banned)")

    # ---- chat
    def call(self, messages: Sequence[dict[str, Any]], tools: Optional[Sequence[dict[str, Any]]] = None,
             complexity: str = "simple", timeout: Optional[float] = None) -> dict[str, Any]:
        """Send a chat completion and return the normalised response."""
        model = self.pick_model(complexity)
        payload: dict[str, Any] = {"model": model, "messages": clean_messages(messages)}
        wrapped = to_openai_tools(tools)
        if wrapped:
            payload["tools"] = wrapped
        t = self.timeout(timeout)
        try:
            r = requests.post(self.url() + "/chat/completions", headers=self._headers(),
                              data=json.dumps(payload), timeout=t)
        except requests.exceptions.Timeout:
            raise ProviderError("timeout", f"timeout after {t:.0f}s", model=model)
        except requests.exceptions.RequestException as e:
            raise ProviderError("timeout", f"unreachable: {type(e).__name__}", model=model)
        if r.status_code >= 400:
            err = map_status(r.status_code, r.text)
            err.model = model
            if err.kind == "bad_model":
                ban_model(self.name, model)
            raise err
        try:
            data = r.json()
        except ValueError:
            raise ProviderError("transient", "invalid JSON response", model=model)
        return parse_openai_response(data, model, self.name)


def export(p: OpenAICompat, module_globals: dict[str, Any]) -> None:
    """Populate a provider module's required attributes from an instance."""
    module_globals.update(
        PROVIDER_NAME=p.name, ENV_KEY=p.env_key, FREE=p.free, LOCAL=p.local, PAID=p.paid,
        call=p.call, available=p.available, health=p.health, models=p.models, PROVIDER=p,
    )
