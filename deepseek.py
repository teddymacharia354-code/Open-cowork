"""DeepSeek (free credits / very cheap). 'reasoner' is excluded: it has no tool calling."""
from __future__ import annotations

from providers.base import OpenAICompat, export

_P = OpenAICompat(
    "deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1",
    families=("deepseek-chat", "deepseek"), fallback_simple="deepseek-chat",
    extra_filter=lambda m: "reasoner" not in str(m["id"]).lower(),
)
export(_P, globals())
