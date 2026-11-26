"""LM Studio / llama.cpp server / any local OpenAI-compatible endpoint (no key).

Default http://localhost:1234/v1 (LM Studio). For llama.cpp's ``llama-server``
set ``LMSTUDIO_BASE_URL=http://localhost:8080/v1``."""
from __future__ import annotations

from providers.base import OpenAICompat, export

_P = OpenAICompat(
    "lmstudio", "LMSTUDIO_BASE_URL", "http://localhost:1234/v1", keyless=True, local=True,
    base_url_env="LMSTUDIO_BASE_URL", fallback_simple="local-model",
    families=("qwen", "llama", "gpt-oss", "mistral"),
)
export(_P, globals())
