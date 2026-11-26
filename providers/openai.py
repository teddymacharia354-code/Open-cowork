"""OpenAI (optional, paid). Only loaded when OPENAI_API_KEY is set."""
from __future__ import annotations

from providers.base import OpenAICompat, export

_SKIP = ("realtime", "search", "instruct", "audio", "transcribe", "codex", "chat-latest")

_P = OpenAICompat(
    "openai", "OPENAI_API_KEY", "https://api.openai.com/v1", free=False, paid=True,
    families=("gpt-4o-mini", "gpt-5-mini", "gpt-4.1-mini"),
    families_complex=("gpt-5-mini", "gpt-4.1", "gpt-4o"),
    fallback_simple="gpt-4o-mini", fallback_complex="gpt-5-mini",
    extra_filter=lambda m: str(m["id"]).startswith("gpt-") and not any(s in str(m["id"]) for s in _SKIP),
)
export(_P, globals())
