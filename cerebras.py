"""Cerebras (free tier, very fast inference)."""
from __future__ import annotations

from providers.base import OpenAICompat, export

_P = OpenAICompat(
    "cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1",
    families=("llama-3.3", "qwen", "gpt-oss", "llama"),
    families_complex=("gpt-oss", "qwen", "llama-3.3", "llama"),
    fallback_simple="llama-3.3-70b", fallback_complex="gpt-oss-120b",
)
export(_P, globals())
