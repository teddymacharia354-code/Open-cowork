"""OpenRouter. Only ':free' models (zero-priced, tool-capable) unless OPENROUTER_ALLOW_PAID=true."""
from __future__ import annotations

import os
from typing import Any, Sequence

from providers.base import OpenAICompat, export


def _allow_paid() -> bool:
    return os.environ.get("OPENROUTER_ALLOW_PAID", "").strip().lower() in ("1", "true", "yes", "on")


def _ok(m: dict[str, Any]) -> bool:
    params = m.get("supported_parameters")
    if params is not None and "tools" not in params:
        return False
    if _allow_paid():
        return True
    pricing = m.get("pricing") or {}
    zero = str(pricing.get("prompt", "1")) in ("0", "0.0") and str(pricing.get("completion", "1")) in ("0", "0.0")
    return str(m["id"]).endswith(":free") or zero


class _OpenRouter(OpenAICompat):
    def families_for(self, complexity: str) -> Sequence[str]:
        if complexity == "complex" and _allow_paid():
            return ("anthropic/claude", "openai/gpt-5", "google/gemini-2.5", "qwen3-235b", "llama-3.3-70b")
        return super().families_for(complexity)


_P = _OpenRouter(
    "openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
    families=("llama-3.3-70b", "gemma-3", "qwen3", "deepseek", "gpt-oss"),
    families_complex=("qwen3-235b", "llama-3.3-70b", "deepseek", "gpt-oss"),
    fallback_simple="meta-llama/llama-3.3-70b-instruct:free", fallback_complex="qwen/qwen3-235b-a22b:free",
    extra_filter=_ok, headers={"HTTP-Referer": "https://github.com/open-cowork", "X-Title": "Open Co-Work"},
)
export(_P, globals())
