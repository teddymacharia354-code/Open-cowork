"""Together AI (free-tier / zero-priced models only)."""
from __future__ import annotations

from typing import Any

from providers.base import OpenAICompat, export


def _is_free_chat(m: dict[str, Any]) -> bool:
    if m.get("type") not in (None, "chat", "language"):
        return False
    pricing = m.get("pricing") or {}
    zero = pricing and float(pricing.get("input", 1) or 0) == 0 and float(pricing.get("output", 1) or 0) == 0
    return bool(zero or "free" in str(m["id"]).lower())


_P = OpenAICompat(
    "together", "TOGETHER_API_KEY", "https://api.together.xyz/v1",
    families=("llama-3.3", "llama", "qwen", "deepseek", "gpt-oss"),
    families_complex=("deepseek", "llama-3.3", "qwen", "llama"),
    fallback_simple="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
    extra_filter=_is_free_chat,
)
export(_P, globals())
