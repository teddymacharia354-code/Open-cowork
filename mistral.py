"""Mistral (free 'Experiment' tier: mistral-small, codestral)."""
from __future__ import annotations

from providers.base import OpenAICompat, export

_ALLOWED = ("mistral-small", "codestral", "ministral", "open-mistral")

_P = OpenAICompat(
    "mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1",
    families=("mistral-small", "codestral", "ministral", "open-mistral"),
    fallback_simple="mistral-small-latest", fallback_complex="codestral-latest",
    extra_filter=lambda m: any(a in str(m["id"]).lower() for a in _ALLOWED),
)
export(_P, globals())
