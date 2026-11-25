"""Workspace memory persisted to ``.open-cowork/memory.json`` (gitignored)."""
from __future__ import annotations

import json
import os
import time
from typing import Any

import config

_PATH = config.DATA_DIR / "memory.json"
_MAX_RECENT = 10


class Memory:
    """Remembers the current project dir, last repo, last deploy and recent tools."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"cwd": None, "last_repo": None, "last_deploy": None,
                                     "recent_tools": []}
        self.load()

    def load(self) -> None:
        """Load memory from disk (ignores corrupt/missing files)."""
        try:
            self.data.update(json.loads(_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        """Atomically persist memory."""
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, _PATH)
        except OSError:
            pass

    def set(self, key: str, value: Any) -> None:
        """Set a key and save."""
        self.data[key] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        """Get a key."""
        return self.data.get(key, default)

    def record_tool(self, name: str) -> None:
        """Track recently used tools."""
        recent = [t for t in self.data.get("recent_tools", []) if t != name]
        self.data["recent_tools"] = ([name] + recent)[:_MAX_RECENT]
        self.save()

    def record_deploy(self, path: str, ok: bool) -> None:
        """Remember the last deploy."""
        self.set("last_deploy", {"path": path, "ok": ok,
                                 "at": time.strftime("%Y-%m-%d %H:%M:%S")})

    def summary(self) -> str:
        """Short text for the system prompt."""
        parts = []
        if self.data.get("last_repo"):
            parts.append(f"last repo: {self.data['last_repo']}")
        d = self.data.get("last_deploy")
        if d:
            parts.append(f"last deploy: {d.get('path')} ({'ok' if d.get('ok') else 'failed'} at {d.get('at')})")
        return "; ".join(parts) or "none yet"
