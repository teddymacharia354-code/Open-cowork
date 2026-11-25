"""`python agent.py --test`: real self-test of providers, tools, git auth, docker and system."""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Callable

import config
import credentials
import ui
from llm import Router
from providers.base import ProviderError
from tools import registry
from tools._core import STATE, check_blocked, have, is_destructive, run_cmd

Row = tuple[str, str, str]  # name, status, detail


def _check(rows: list[Row], name: str, fn: Callable[[], tuple[str, str]]) -> None:
    try:
        status, detail = fn()
    except Exception as e:  # a crashing check is a failing check
        status, detail = "FAIL", f"{type(e).__name__}: {e}"
    rows.append((name, status, detail))


def _failover() -> tuple[str, str]:
    """Simulated 429 on provider 1 must fail over to provider 2 well under 7s."""
    def mod(name: str, behaviour: str) -> types.ModuleType:
        m = types.ModuleType(name)
        m.PROVIDER_NAME, m.ENV_KEY, m.FREE = name, "X", True
        m.available = lambda: True

        def call(messages, tools=None, complexity="simple", timeout=None):  # type: ignore[no-untyped-def]
            if behaviour == "429":
                raise ProviderError("rate_limit", "rate limited", 429)
            return {"content": "ok", "tool_calls": [], "model": "m", "provider": name}
        m.call = call
        return m

    logs: list[str] = []
    saved = {k: os.environ.get(k) for k in ("LLM_ORDER", "LLM_PRIMARY")}
    os.environ["LLM_ORDER"], os.environ["LLM_PRIMARY"] = "sim1,sim2", ""
    try:
        r = Router(log=logs.append, load=False)
        r.register(mod("sim1", "429"))
        r.register(mod("sim2", "ok"))
        t0 = time.time()
        resp = r.chat([{"role": "user", "content": "hi"}])
        dt = time.time() - t0
        cooled = "sim1" in r.cooldown
        ok = resp["provider"] == "sim2" and dt < 7 and cooled and any("failed (429) -> next [LLM: sim2]" in x for x in logs)
        return ("PASS" if ok else "FAIL"), f"{dt * 1000:.0f} ms, sim1 cooldown={cooled}, log='{logs[0] if logs else ''}'"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def run_self_test() -> int:
    """Run all checks, print a PASS/FAIL/SKIP table. Exit 1 if anything FAILs."""
    rows: list[Row] = []
    registry.load_all()
    work = Path(tempfile.mkdtemp(prefix="cowork-selftest-"))
    old_cwd, old_state = STATE.cwd, (STATE.dry_run, STATE.auto_yes, STATE.verbose)
    STATE.cwd, STATE.verbose, STATE.dry_run = str(work), False, False
    router = Router()

    _check(rows, "python >= 3.10", lambda: ("PASS" if sys.version_info >= (3, 10) else "FAIL", platform.python_version()))
    _check(rows, "config / .env", lambda: ("PASS", ".env loaded" if config.ENV_FILE.exists() else "no .env (zero-key mode is fine)"))
    _check(rows, "providers auto-loaded", lambda: ("PASS" if len(router.providers) >= 10 else "FAIL",
                                                   f"{len(router.providers)}: {', '.join(sorted(router.providers))}"))
    router.refresh_health()
    for name, info in sorted(router.providers.items()):
        if not info.available():
            rows.append((f"llm: {name}", "SKIP", "not configured (no key)"))
        elif router.health.get(name):
            rows.append((f"llm: {name}", "PASS", "reachable" + (f", cooldown" if name in router.cooldown else "")))
        elif name in router.fatal:
            rows.append((f"llm: {name}", "FAIL", f"auth/fatal: {router.fatal[name][:60]}"))
        else:
            rows.append((f"llm: {name}", "SKIP" if info.local else "FAIL", router.last_error.get(name, "unreachable")[:70]
                         if not info.local else "local server not running"))
    up = [n for n in router.providers if router.health.get(n)]
    rows.append(("llm: at least one usable", "PASS" if up else "SKIP",
                 f"{up[0]} ready" if up else "none -> offline mode (direct commands) works without an LLM"))
    _check(rows, "router failover (429 -> next, <7s)", _failover)
    _check(rows, "tool registry", lambda: ("PASS" if len(registry.all_tools()) >= 40 and not registry.LOAD_ERRORS else "FAIL",
                                           f"{len(registry.all_tools())} tools" + (f"; errors {registry.LOAD_ERRORS}" if registry.LOAD_ERRORS else "")))

    def schemas() -> tuple[str, str]:
        bad = [t.name for t in registry.all_tools() if not t.description or not t.schema()["parameters"]["type"]]
        miss = [(t.name, r) for t in registry.all_tools() for r in t.required if r not in t.properties]
        return ("PASS" if not bad and not miss else "FAIL"), f"bad={bad} missing_required={miss}"
    _check(rows, "tool schemas valid", schemas)

    def safety() -> tuple[str, str]:
        blocked = ["rm -rf /", "sudo rm -rf /*", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", ":(){ :|:& };:"]
        miss = [c for c in blocked if not check_blocked(c)]
        ok_cmds = ["ls -la", "rm -rf ./build", "git status"]
        wrong = [c for c in ok_cmds if check_blocked(c)]
        confirm = ["docker system prune", "git push --force", "git reset --hard", "kill -9 123", "apt remove x", "terraform destroy", "rm -rf ./x"]
        unconf = [c for c in confirm if not is_destructive(c)]
        r = run_cmd("rm -rf /", tool="selftest", shell=True)
        good = not miss and not wrong and not unconf and r.get("blocked")
        return ("PASS" if good else "FAIL"), f"unblocked={miss} overblocked={wrong} unconfirmed={unconf}"
    _check(rows, "safety blocklist + confirm list", safety)

    def redaction() -> tuple[str, str]:
        s = "token ghp_" + "a" * 36 + " https://u:hunter2pass@github.com AKIA" + "A" * 16 + " password=swordfish"
        out = credentials.redact(s)
        leaked = [x for x in ("ghp_aaaa", "hunter2pass", "AKIAAAAA", "swordfish") if x in out]
        return ("PASS" if not leaked else "FAIL"), f"leaked={leaked}"
    _check(rows, "secret redaction", redaction)

    def audit() -> tuple[str, str]:
        run_cmd(["echo", "audit-probe password=topsecret1"], tool="selftest")
        text = config.AUDIT_LOG.read_text()
        last = text.strip().splitlines()[-1]
        return ("PASS" if "selftest" in last and "topsecret1" not in last and "exit=0" in last else "FAIL"), last[-90:]
    _check(rows, "audit log (redacted)", audit)

    def dry() -> tuple[str, str]:
        target = work / "should-not-exist"
        STATE.dry_run = True
        r = run_cmd(["touch", str(target)], tool="selftest")
        STATE.dry_run = False
        return ("PASS" if r["ok"] and r.get("dry_run") and not target.exists() else "FAIL"), r["output"][:80]
    _check(rows, "dry-run executes nothing", dry)

    def files() -> tuple[str, str]:
        STATE.auto_yes = True
        ex = registry.execute
        a = ex("create_file", {"path": "a.txt", "content": "hello\n"})
        b = ex("edit_file", {"path": "a.txt", "old_text": "hello", "new_text": "world"})
        c = ex("cat", {"path": "a.txt"})
        s = ex("search", {"pattern": "world"})
        d = ex("delete_path", {"path": "a.txt"})
        p = ex("delete_path", {"path": "/"})
        STATE.auto_yes = False
        ok = all(x["ok"] for x in (a, b, c, s, d)) and "world" in c["output"] and not (work / "a.txt").exists() and p.get("blocked")
        return ("PASS" if ok else "FAIL"), "create/edit(backup)/cat/search/delete + protected path blocked"
    _check(rows, "file tools round-trip", files)

    def git() -> tuple[str, str]:
        if not have("git"):
            return "FAIL", "git not installed"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        os.environ.update(env)
        ex = registry.execute
        repo = str(work / "repo")
        steps = [ex("git_init", {"repo": repo}), ex("create_file", {"path": "repo/x.py", "content": "print(1)\n"}),
                 ex("git_commit", {"repo": repo, "add_all": True}), ex("git_log", {"repo": repo}), ex("git_status", {"repo": repo})]
        for k in env:
            os.environ.pop(k, None)
        ok = all(s["ok"] for s in steps)
        return ("PASS" if ok else "FAIL"), (steps[2].get("commit_message", "")[:50] + f" [{steps[2].get('message_source')}]") if ok else str([s["error"] for s in steps if not s["ok"]])[:90]
    _check(rows, "git init/add/commit(auto msg)/log", git)

    def gauth() -> tuple[str, str]:
        st = credentials.auth_status()
        methods = [k for k in ("keyring_backend", "env_credentials", "credential_helper", "ssh_keys", "gh_cli_authenticated") if st[k]]
        return "PASS", "available: " + (", ".join(methods) or "none") + " + interactive prompt fallback"
    _check(rows, "git auth methods", gauth)

    def docker() -> tuple[str, str]:
        if not have("docker"):
            return "SKIP", "docker not installed, install with: https://docs.docker.com/engine/install/"
        r = registry.execute("docker_health", {})
        if r["ok"]:
            return "PASS", r["output"].splitlines()[0]
        return "SKIP", "installed but daemon not reachable: " + (r.get("hint") or r["error"])[:70]
    _check(rows, "docker", docker)

    def system() -> tuple[str, str]:
        r = registry.execute("system_overview", {})
        return ("PASS" if r["ok"] and "== DISK ==" in r["output"] and "== MEMORY ==" in r["output"] else "FAIL"), \
            f"{len(r['output'].splitlines())} lines of real metrics"
    _check(rows, "system_overview", system)

    def tc() -> tuple[str, str]:
        r = registry.execute("toolchain", {})
        return ("PASS" if r["ok"] else "FAIL"), "missing: " + (", ".join(r.get("missing", [])) or "none")
    _check(rows, "toolchain", tc)

    def scaff() -> tuple[str, str]:
        r = registry.execute("scaffold", {"kind": "flask", "name": "demo"})
        t = registry.execute("scaffold", {"kind": "dockerfile", "path": "demo"})
        ci = registry.execute("scaffold", {"kind": "ci", "path": "demo"})
        ok = r["ok"] and t["ok"] and ci["ok"] and (work / "demo" / "app.py").exists() and (work / "demo/.github/workflows/ci.yml").exists()
        return ("PASS" if ok else "FAIL"), "flask + Dockerfile + CI written"
    _check(rows, "scaffold", scaff)

    def net() -> tuple[str, str]:
        r = registry.execute("public_ip", {})
        return ("PASS", r["output"][:40]) if r["ok"] else ("SKIP", "no outbound network")
    _check(rows, "network (public ip)", net)

    STATE.cwd = old_cwd
    STATE.dry_run, STATE.auto_yes, STATE.verbose = old_state
    shutil.rmtree(work, ignore_errors=True)

    colors = {"PASS": "green", "FAIL": "red", "SKIP": "yellow"}
    w = max(len(r[0]) for r in rows)
    print(ui.c("\nOpen Co-Work self-test", "bold"))
    print(f"{'CHECK':<{w}}  STATUS  DETAIL\n{'-' * w}  ------  ------")
    for name, status, detail in rows:
        print(f"{name:<{w}}  {ui.c(f'{status:<6}', colors[status])}  {detail}")
    n = {s: sum(1 for r in rows if r[1] == s) for s in colors}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped (SKIP = optional thing not present)")
    return 1 if n["FAIL"] else 0
