"""HuggingFace Inference Providers (free monthly credits). Token: HF_TOKEN."""
from __future__ import annotations

from providers.base import OpenAICompat, export

_P = OpenAICompat(
    "huggingface", "HF_TOKEN", "https://router.huggingface.co/v1",
    families=("Qwen2.5", "Qwen3", "Llama-3.3", "gpt-oss", "Llama", "DeepSeek"),
    families_complex=("gpt-oss", "Qwen3", "Qwen2.5", "Llama-3.3", "DeepSeek"),
    fallback_simple="Qwen/Qwen2.5-72B-Instruct",
    alt_key_env="HUGGINGFACE_API_KEY",
)
export(_P, globals())
