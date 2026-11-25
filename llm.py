"""LLM router: provider auto-loading, parallel health checks, fast failover and a
circuit breaker (cooldown dict, fatal set, last_used tracking).

Failover rules
  * hard per-request deadline (``LLM_TIMEOUT``, default 5s; local providers get
    ``LLM_LOCAL_TIMEOUT``) -> on timeout fail over instantly
  * at most ONE immediate retry per provider, only for transient errors
  * 429 -> provider cooldown for 30s and move on instantly (never sleeps)
  * 400/401/403 -> provider marked fatal for the rest of the session
  * startup: all providers are pinged concurrently; the healthy set is cached
    for 60s and refreshed in the background
"""
from __future__ import annotations

import concurrent.futures as cf
import importlib
import inspect
import pkgutil
import re
import sys
import threading
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Optional, Sequence

import providers
import ui
from config import get_config
from providers.base import ProviderError


class AllProvidersFailed(Exception):
    """No provider produced a response. ``attempts`` is [(provider, reason)]."""

    def __init__(self, attempts: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{p}: {r}" for p, r in attempts) or "no providers available")
        self.attempts = attempts
        self.unreachable = False  # True when skipped because health checks showed nothing reachable


@dataclass
class ProviderInfo:
    """A loaded provider module plus its metadata."""

    name: str
    module: ModuleType
    free: bool
    local: bool
    paid: bool

    def available(self) -> bool:
        """Is the provider configured (key present / keyless)?"""
        fn = getattr(self.module, "available", None)
        if callable(fn):
            return bool(fn())
        import os
        return bool(os.environ.get(self.module.ENV_KEY, "").strip())


_COMPLEX_HINT = re.compile(
    r"\b(plan|deploy|debug|refactor|analy[sz]e|why|architect|design|terraform|scaffold|migrate|optimi[sz]e|"
    r"investigate|root cause|write|implement|fix|compare|explain how)\b", re.I)


def classify(text: str) -> str:
    """Heuristic complexity: 'complex' for planning/coding/debugging, else 'simple'."""
    return "complex" if len(text) > 240 or _COMPLEX_HINT.search(text) else "simple"


def _default_log(msg: str) -> None:
    print(ui.c(msg, "dim"), file=sys.stderr)


class Router:
    """Routes chat requests across providers with fast failover."""

    def __init__(self, log: Callable[[str], None] = _default_log, load: bool = True) -> None:
        self.log = log
        self.providers: dict[str, ProviderInfo] = {}
        self.cooldown: dict[str, float] = {}
        self.fatal: dict[str, str] = {}
        self.last_used: dict[str, float] = {}
        self.last_error: dict[str, str] = {}
        self.last_model: dict[str, str] = {}
        self.health: dict[str, bool] = {}
        self._health_at = 0.0
        self._refreshing = False
        self._lock = threading.RLock()
        if load:
            self.load_providers()

    # ------------------------------------------------------------ loading
    def load_providers(self) -> None:
        """Import every providers/*.py exposing PROVIDER_NAME, ENV_KEY and call()."""
        for mod in pkgutil.iter_modules(providers.__path__):
            if mod.name.startswith("_") or mod.name == "base":
                continue
            try:
                m = importlib.import_module(f"providers.{mod.name}")
            except Exception as e:
                self.last_error[mod.name] = f"load failed: {type(e).__name__}: {e}"
                continue
            if all(hasattr(m, a) for a in ("PROVIDER_NAME", "ENV_KEY", "call")):
                self.register(m)

    def register(self, module: ModuleType) -> None:
        """Register a provider module (also used by tests)."""
        name = str(module.PROVIDER_NAME)
        self.providers[name] = ProviderInfo(
            name, module, bool(getattr(module, "FREE", True)), bool(getattr(module, "LOCAL", False)),
            bool(getattr(module, "PAID", False)))

    # ------------------------------------------------------------ ordering
    def order(self, complexity: str = "simple") -> list[str]:
        """Provider names in the order they should be tried (configured only)."""
        cfg = get_config()
        names: list[str] = list(cfg.llm_order)
        if cfg.llm_primary:
            names.insert(0, cfg.llm_primary)
        rest = sorted((n for n in self.providers if n not in names),
                      key=lambda n: (self.providers[n].paid, not self.providers[n].free, n))
        names = [n for n in dict.fromkeys(names + rest) if n in self.providers and self.providers[n].available()]
        if cfg.complexity_routing and complexity == "complex":
            strong = [n for n in names if self.providers[n].paid]
            pref = strong + [n for n in ("groq", "gemini") if n in names and n not in strong]
            names = pref + [n for n in names if n not in pref]
        if cfg.llm_primary in names:
            names.remove(cfg.llm_primary)
            names.insert(0, cfg.llm_primary)
        return names

    def set_primary(self, name: str, model: str = "") -> bool:
        """Make ``name`` the primary provider (optionally pin a model)."""
        import os
        if name not in self.providers:
            return False
        os.environ["LLM_PRIMARY"] = name
        if model:
            os.environ[f"{name.upper()}_MODEL"] = model
        self.fatal.pop(name, None)
        self.cooldown.pop(name, None)
        return True

    # ------------------------------------------------------------ health
    def _ping(self, name: str, timeout: float) -> None:
        info = self.providers[name]
        fn = getattr(info.module, "health", None)
        try:
            if callable(fn):
                fn(timeout)
            self.health[name] = True
            self.last_error.pop(name, None)
        except ProviderError as e:
            self.health[name] = False
            self.last_error[name] = f"{e.status or e.kind}: {e}"[:160]
            if e.status in (401, 403):
                self.fatal[name] = str(e)
            elif e.kind == "rate_limit":
                self.cooldown[name] = time.time() + get_config().cooldown_seconds
                self.health[name] = True  # alive, just limited
        except Exception as e:
            self.health[name] = False
            self.last_error[name] = f"{type(e).__name__}: {e}"[:160]

    def refresh_health(self, timeout: float = 3.0) -> dict[str, bool]:
        """Ping all configured providers concurrently and cache the result."""
        names = [n for n in self.providers if self.providers[n].available()]
        if names:
            ex = cf.ThreadPoolExecutor(max_workers=len(names))
            futs = {ex.submit(self._ping, n, timeout): n for n in names}
            done, pending = cf.wait(futs, timeout=timeout + 1.0)
            for f in pending:
                self.health[futs[f]] = False
                self.last_error[futs[f]] = "health check timed out"
            ex.shutdown(wait=False, cancel_futures=True)
        self._health_at = time.time()
        return dict(self.health)

    def _maybe_refresh_async(self) -> None:
        ttl = get_config().health_ttl
        if time.time() - self._health_at < ttl or self._refreshing:
            return
        if not self._health_at:  # never checked: do it now, in parallel
            self.refresh_health()
            return
        self._refreshing = True

        def bg() -> None:
            try:
                self.refresh_health()
            finally:
                self._refreshing = False
        threading.Thread(target=bg, daemon=True).start()

    # ------------------------------------------------------------ calling
    def _call_with_deadline(self, info: ProviderInfo, messages: Sequence[dict[str, Any]],
                            tools: Optional[Sequence[dict[str, Any]]], complexity: str) -> dict[str, Any]:
        cfg = get_config()
        timeout = cfg.llm_local_timeout if info.local else cfg.llm_timeout
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                fn = info.module.call
                kwargs = {"timeout": timeout} if "timeout" in inspect.signature(fn).parameters else {}
                box["r"] = fn(messages, tools, complexity, **kwargs)
            except BaseException as e:  # noqa: BLE001 - re-raised in caller
                box["e"] = e

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout + 0.75)
        if th.is_alive():
            raise ProviderError("timeout", f"no response within {timeout:.0f}s")
        if "e" in box:
            e = box["e"]
            if isinstance(e, ProviderError):
                raise e
            raise ProviderError("transient", f"{type(e).__name__}: {e}")
        return box["r"]

    def chat(self, messages: Sequence[dict[str, Any]], tools: Optional[Sequence[dict[str, Any]]] = None,
             complexity: str = "simple", quiet: bool = False) -> dict[str, Any]:
        """Get a response from the first provider that works.

        Raises :class:`AllProvidersFailed` if none does.
        """
        cfg = get_config()
        log = (lambda _m: None) if quiet else self.log
        names = self.order(complexity)
        attempts: list[tuple[str, str]] = []
        if not names:
            raise AllProvidersFailed([("none", "no LLM provider configured or running")])
        self._maybe_refresh_async()
        now = time.time()
        cands = [n for n in names if n not in self.fatal and self.cooldown.get(n, 0) <= now]
        healthy = [n for n in cands if self.health.get(n, True)]
        if self._health_at and cands and not healthy:
            # Every configured provider failed its last health check (re-checked in the background
            # every LLM_HEALTH_TTL seconds): don't burn time/noise calling dead endpoints.
            err = AllProvidersFailed([(n, self.last_error.get(n, "unreachable")) for n in cands])
            err.unreachable = True
            raise err
        cands = healthy or cands
        if not cands:
            wait = max([self.cooldown.get(n, 0) - now for n in names if n not in self.fatal] or [0])
            why = "all providers are fatal/auth-failed" if wait <= 0 else f"all providers cooling down (~{wait:.0f}s)"
            raise AllProvidersFailed([(n, self.fatal.get(n) or "cooldown") for n in names] or [("none", why)])

        for i, name in enumerate(cands):
            info = self.providers[name]
            nxt = cands[i + 1] if i + 1 < len(cands) else None
            tries = 0
            while True:
                tries += 1
                t0 = time.time()
                try:
                    resp = self._call_with_deadline(info, messages, tools, complexity)
                except ProviderError as e:
                    reason = str(e.status or e.kind)
                    self.last_error[name] = f"{e.status or e.kind}: {e}"[:160]
                    if e.kind in ("transient", "bad_model") and tries < 2:
                        continue  # the single immediate retry (bad_model -> next model)
                    if e.kind == "fatal":
                        self.fatal[name] = str(e)
                    else:  # rate_limit, timeout, exhausted transient
                        self.cooldown[name] = time.time() + cfg.cooldown_seconds
                    attempts.append((name, f"{reason} {e}"[:140]))
                    log(f"[LLM: {name}] failed ({reason}) -> " + (f"next [LLM: {nxt}]" if nxt else "no more providers"))
                    break
                self.last_used[name] = time.time()
                self.last_model[name] = resp.get("model", "")
                self.health[name] = True
                log(f"[LLM: {name}] {resp.get('model', '')} {time.time() - t0:.1f}s")
                return resp
        raise AllProvidersFailed(attempts)

    def complete_text(self, system: str, user: str, complexity: str = "simple") -> Optional[str]:
        """One-shot helper used by tools (commit messages, auto-fix). None on failure."""
        try:
            r = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                          None, complexity, quiet=True)
        except AllProvidersFailed:
            return None
        return (r.get("content") or "").strip() or None

    # ------------------------------------------------------------ status
    def any_available(self) -> bool:
        """True if at least one configured provider looks usable."""
        return any(n not in self.fatal and self.health.get(n, False) for n in self.order())

    def status_rows(self) -> list[list[str]]:
        """Rows for the /providers table."""
        now = time.time()
        rows = []
        for n, info in sorted(self.providers.items()):
            if not info.available():
                state = "not configured"
            elif n in self.fatal:
                state = "FATAL (auth/400)"
            elif self.cooldown.get(n, 0) > now:
                state = f"cooldown {self.cooldown[n] - now:.0f}s"
            elif self.health.get(n):
                state = "healthy"
            elif n in self.health:
                state = "unreachable"
            else:
                state = "unchecked"
            lu = self.last_used.get(n)
            rows.append([n, "free" if info.free else "paid", "local" if info.local else "cloud", state,
                         self.last_model.get(n, "-"),
                         time.strftime("%H:%M:%S", time.localtime(lu)) if lu else "-",
                         self.last_error.get(n, "")[:50]])
        return rows


_router: Optional[Router] = None


def get_router() -> Router:
    """Process-wide router singleton (lazy)."""
    global _router
    if _router is None:
        _router = Router()
    return _router
