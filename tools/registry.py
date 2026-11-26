"""Tool registry: auto-loads every ``tools/*.py`` module and executes tools.

A tool module just decorates functions with :func:`tool`; nothing else needs
to be registered. Modules whose name starts with ``_`` are helpers.
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from tools._core import clip, result

Param = tuple  # (json_type, description[, enum_list])


@dataclass
class Tool:
    """A callable capability exposed to the LLM and the CLI."""

    name: str
    description: str
    properties: dict[str, Param]
    required: tuple[str, ...]
    func: Callable[..., Any]
    category: str
    destructive: bool = False

    def schema(self) -> dict[str, Any]:
        """JSON-schema function definition (OpenAI style)."""
        props: dict[str, Any] = {}
        for k, spec in self.properties.items():
            p: dict[str, Any] = {"type": spec[0], "description": spec[1]}
            if spec[0] == "array":
                p["items"] = {"type": "string"}
            if len(spec) > 2 and spec[2]:
                p["enum"] = list(spec[2])
            props[k] = p
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": props, "required": list(self.required)}}


_TOOLS: dict[str, Tool] = {}
LOAD_ERRORS: dict[str, str] = {}
_loaded = False


def tool(name: str, desc: str, params: Optional[dict[str, Param]] = None, required: Iterable[str] = (),
         category: str = "misc", destructive: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator registering a function as a tool."""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        _TOOLS[name] = Tool(name, desc, params or {}, tuple(required), fn, category, destructive)
        return fn
    return deco


def load_all() -> None:
    """Import every tool module in this package (idempotent)."""
    global _loaded
    if _loaded:
        return
    import tools
    for mod in pkgutil.iter_modules(tools.__path__):
        if mod.name.startswith("_") or mod.name == "registry":
            continue
        try:
            importlib.import_module(f"tools.{mod.name}")
        except Exception as e:  # a broken module must not kill the agent
            LOAD_ERRORS[mod.name] = f"{type(e).__name__}: {e}"
    _loaded = True


def all_tools() -> list[Tool]:
    """All registered tools sorted by category then name."""
    return sorted(_TOOLS.values(), key=lambda t: (t.category, t.name))


def get(name: str) -> Optional[Tool]:
    """Look up a tool by name."""
    return _TOOLS.get(name)


def categories() -> list[str]:
    """Sorted list of tool categories."""
    return sorted({t.category for t in _TOOLS.values()})


CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "git": ["git", "commit", "push", "pull", "branch", "repo", "stash", "checkout", "clone", "gitignore",
            "github", "gitlab", "merge", "rebase", "diff", "fetch", "credential", "token"],
    "docker": ["docker", "container", "compose", "image", "volume", "dockerfile", "crash", "restart", "fix it"],
    "cloud": ["aws", "ec2", "s3", "cloud", "deploy", "public ip", "ip address", "port", "ping", "digitalocean",
              "droplet", "hetzner", "gcp", "gcloud", "url", "http", "health", "security group", "ebs",
              "cloudwatch", "cost", "bill", "server", "instance"],
    "system": ["disk", "memory", "ram", "cpu", "system", "process", "service", "systemd", "cron", "log",
               "cleanup", "clean", "monitor", "watch", "toolchain", "installed", "install", "uptime",
               "port", "kill", "journal", "zombie", "space", "load", "overview", "status"],
    "files": ["file", "folder", "directory", "ls", "cat", "read", "search", "grep", "find", "create",
              "edit", "delete", "remove", "move", "copy", "chmod", "large", "write", "rename"],
    "dev": ["run", "test", "pytest", "npm", "pip", "install", "fix", "build", "execute", "script", "dependenc",
            "error", "debug"],
    "scaffold": ["scaffold", "flask", "fastapi", "express", "static site", "dockerfile", "terraform",
                 "workflow", "ci", "readme", "boilerplate", "generate", "new app", "template", "compose"],
}
ALWAYS = {"run_command", "ls", "cat", "system_overview"}


def categories_for(text: str) -> set[str]:
    """Which tool categories look relevant to ``text`` (keyword prefix match)."""
    low = text.lower()
    cats = set()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(re.search(r"\b" + re.escape(k), low) for k in kws):
            cats.add(cat)
    return cats


def select(text: str, sticky: Iterable[str] = ()) -> list[Tool]:
    """Pick a relevant subset of tools for a request (keeps prompts small)."""
    cats = categories_for(text) | set(sticky)
    if not cats:
        cats = {"system", "files", "dev"}
    chosen = [t for t in all_tools() if t.category in cats or t.name in ALWAYS]
    return chosen


def _coerce(value: Any, typ: str) -> Any:
    if value is None:
        return None
    try:
        if typ == "integer":
            return int(float(value))
        if typ == "number":
            return float(value)
        if typ == "boolean":
            return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "y", "on")
        if typ == "array":
            if isinstance(value, list):
                return [str(v) for v in value]
            s = str(value).strip()
            if s.startswith("["):
                return [str(v) for v in json.loads(s)]
            return [x for x in re.split(r"[,\s]+", s) if x]
        return str(value)
    except (ValueError, TypeError):
        return value


def execute(name: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Run a tool by name with already-parsed arguments; never raises."""
    t = _TOOLS.get(name)
    if t is None:
        return result(False, error=f"Unknown tool '{name}'. Use /tools to list tools.", exit_code=2)
    args = dict(args or {})
    if "__parse_error__" in args:
        return result(False, error="Tool arguments were not valid JSON: " + str(args["__parse_error__"])[:200],
                      exit_code=2)
    clean: dict[str, Any] = {}
    for k, v in args.items():
        if k in t.properties and v is not None and v != "":
            clean[k] = _coerce(v, t.properties[k][0])
    missing = [r for r in t.required if r not in clean]
    if missing:
        return result(False, error=f"Missing required argument(s): {', '.join(missing)}", exit_code=2)
    try:
        res = t.func(**clean)
    except KeyboardInterrupt:
        return result(False, error="Interrupted by user.", exit_code=130)
    except Exception as e:
        tb = traceback.format_exc().strip().splitlines()[-1]
        return result(False, error=f"Tool '{name}' crashed: {tb}", exit_code=1)
    if isinstance(res, dict):
        res.setdefault("ok", True)
        res.setdefault("output", "")
        res.setdefault("error", "")
        res.setdefault("exit_code", 0 if res["ok"] else 1)
        res["output"] = clip(str(res["output"]))
        return res
    return result(True, output=clip(str(res)))
