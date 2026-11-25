"""Configuration: loads ``.env`` and exposes a typed, read-only Config.

Secrets are never stored in this object's repr and are never written anywhere.
Environment variables always win over ``.env`` values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / ".open-cowork"
LOG_DIR = ROOT / "logs"
AUDIT_LOG = LOG_DIR / "audit.log"
ENV_FILE = ROOT / ".env"

DEFAULT_ORDER = "groq,gemini,cerebras,ollama,openrouter"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (used when python-dotenv is not installed)."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        out[key.strip()] = val
    return out


def load_env(path: Path = ENV_FILE) -> bool:
    """Load ``path`` into ``os.environ`` without overriding existing variables.

    Returns True if the file existed.
    """
    if not path.exists():
        return False
    for key, val in _parse_env_file(path).items():
        if val != "" and key not in os.environ:
            os.environ[key] = val
    return True


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip() or default
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    """Runtime configuration derived from the environment."""

    llm_order: tuple[str, ...]
    llm_primary: str
    complexity_routing: bool
    llm_timeout: float
    llm_local_timeout: float
    cooldown_seconds: float
    health_ttl: float
    openrouter_allow_paid: bool
    git_username: str
    git_token: str
    git_save_plaintext: bool
    git_cred_hosts: tuple[str, ...]
    disk_threshold: float
    mem_threshold: float
    watch_interval: float

    def __repr__(self) -> str:  # never leak the token
        return f"Config(order={self.llm_order}, primary={self.llm_primary!r}, ...)"


def get_config() -> Config:
    """Build a Config from the current environment."""
    return Config(
        llm_order=_list("LLM_ORDER", DEFAULT_ORDER),
        llm_primary=os.environ.get("LLM_PRIMARY", "").strip().lower(),
        complexity_routing=_bool("LLM_COMPLEXITY_ROUTING", True),
        llm_timeout=_float("LLM_TIMEOUT", 5.0),
        llm_local_timeout=_float("LLM_LOCAL_TIMEOUT", 120.0),
        cooldown_seconds=_float("LLM_COOLDOWN", 30.0),
        health_ttl=_float("LLM_HEALTH_TTL", 60.0),
        openrouter_allow_paid=_bool("OPENROUTER_ALLOW_PAID", False),
        git_username=os.environ.get("GIT_USERNAME", "").strip(),
        git_token=os.environ.get("GIT_TOKEN", "").strip(),
        git_save_plaintext=_bool("GIT_SAVE_PLAINTEXT", False),
        git_cred_hosts=_list("GIT_CRED_HOSTS", "github.com,gitlab.com,bitbucket.org"),
        disk_threshold=_float("WATCH_DISK_PCT", 90.0),
        mem_threshold=_float("WATCH_MEM_PCT", 90.0),
        watch_interval=_float("WATCH_INTERVAL", 60.0),
    )
