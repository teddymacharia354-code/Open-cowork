"""Groq (free tier). Models are auto-discovered; nothing is hard-coded except a last-resort fallback."""
from __future__ import annotations

from providers.base import OpenAICompat, export

# Substrings of model ids to prefer, in order (the ids themselves come from /models).
_FAMILIES = ("gpt-oss", "qwen", "llama", "deepseek", "moonshot", "kimi", "openai/")
_EXCLUDE = ("compound", "safeguard", "prompt-guard", "allam")

_P = OpenAICompat(
    "groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1",
    families=_FAMILIES, fallback_simple="openai/gpt-oss-20b", fallback_complex="openai/gpt-oss-20b",
    extra_filter=lambda m: not any(x in str(m["id"]).lower() for x in _EXCLUDE) and m.get("active", True),
)
export(_P, globals())
