"""Ollama (local, $0, no key). Installed models are auto-detected via /api/tags.

ENV_KEY is OLLAMA_HOST (optional, default http://localhost:11434)."""
from __future__ import annotations

import os
from typing import Any

from providers.base import OpenAICompat, export


def _host() -> str:
    h = os.environ.get("OLLAMA_HOST", "").strip() or "http://localhost:11434"
    if not h.startswith("http"):
        h = "http://" + h
    return h.rstrip("/")


class _Ollama(OpenAICompat):
    def url(self) -> str:
        return _host() + "/v1"

    def _raw_models(self, timeout: float) -> list[dict[str, Any]]:
        data = self._get(_host() + "/api/tags", timeout, headers={})
        return [{"id": m["name"]} for m in data.get("models", []) if m.get("name")]


_P = _Ollama(
    "ollama", "OLLAMA_HOST", "http://localhost:11434/v1", keyless=True, local=True,
    families=("qwen3", "qwen2.5", "llama3.1", "llama3.3", "llama3.2", "gpt-oss", "mistral", "granite",
              "phi4", "gemma3", "llama"),
)
export(_P, globals())
