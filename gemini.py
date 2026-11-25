"""Google Gemini (free tier). Discovers models via the native ListModels API and
chats through Google's OpenAI-compatible endpoint."""
from __future__ import annotations

from typing import Any

from providers.base import OpenAICompat, export

_SKIP = ("tts", "image", "live", "audio", "embedding", "robotics", "computer-use", "learnlm", "aqa",
         "vision", "native-audio", "thinking-exp")


class _Gemini(OpenAICompat):
    def _raw_models(self, timeout: float) -> list[dict[str, Any]]:
        data = self._get(f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key={self.key()}",
                         timeout, headers={})
        out = []
        for m in data.get("models", []):
            mid = str(m.get("name", "")).split("/")[-1]
            if mid.startswith("gemini") and "flash" in mid and "generateContent" in m.get("supportedGenerationMethods", []) \
                    and not any(s in mid for s in _SKIP):
                out.append({"id": mid})
        return out


_P = _Gemini(
    "gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai",
    families=("gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash", "flash"),
    families_complex=("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "flash"),
    fallback_simple="gemini-2.0-flash", fallback_complex="gemini-2.5-flash",
    alt_key_env="GOOGLE_API_KEY",
)
export(_P, globals())
