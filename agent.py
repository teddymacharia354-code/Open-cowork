#!/usr/bin/env python3
"""Open Co-Work - an open-source AI DevOps co-worker for your terminal.

The LLM is ONLY a translator: it turns your words into tool calls, then turns
REAL tool results into a human answer. Without any LLM the agent still runs
(direct commands, /tool, and a small offline intent matcher).

Usage:
  python agent.py                 interactive REPL
  python agent.py "disk usage?"   one-shot request
  python agent.py --dry-run       show what WOULD run, run nothing
  python agent.py --test          self-test (PASS/FAIL table)
  python agent.py --help          lists all tools
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

if sys.version_info < (3, 10):
    sys.exit("Open Co-Work needs Python 3.10+")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

config.load_env()

import offline  # noqa: E402
import ui  # noqa: E402
from llm import AllProvidersFailed, Router, classify  # noqa: E402
from memory import Memory  # noqa: E402
from tools import registry  # noqa: E402
from tools._core import STATE, confirm, result  # noqa: E402

__version__ = "1.0.0"
MAX_ROUNDS = 8
HISTORY_LIMIT = 24
PLAN_RE = re.compile(r"^\s*(?:/plan\b|plan\s*[:\-]|make a plan\b|step[- ]by[- ]step plan\b|plan\s+(?:to|for|how)\b)\s*", re.I)
CHAT_RE = re.compile(r"^\s*(?:/chat\b|chat\s*[:\-]?\s+)", re.I)

SYSTEM_PROMPT = """You are Open Co-Work, a DevOps co-worker living in the user's terminal.
You are ONLY a translator between the user and real tools:
  user intent -> tool calls -> real tool results -> a short, honest answer.
Rules:
- Never invent command output, file contents or results. Only report what tool results contain.
- If a tool result has ok=false, say it FAILED and quote the real error. Never claim success without ok=true.
- If a result has dry_run=true, nothing was executed: say so.
- Destructive actions (rm, prune, force push, reset --hard, kill, apt remove, terraform destroy) ask the user for
  confirmation inside the tool itself. Just call the tool; do not ask again yourself.
- Prefer read-only inspection first. Use the dedicated tool when one exists; use run_command otherwise.
- If the user is just chatting or asking a conceptual question, answer directly without tools.
- Be concise. Show key facts, not a recap of every step.
Environment: OS={os}; working directory={cwd}; dry-run={dry}; memory: {memory}."""


def _fmt_args(args: dict[str, Any]) -> str:
    s = ", ".join(f"{k}={json.dumps(v) if not isinstance(v, str) else repr(v)}" for k, v in args.items())
    return s if len(s) < 140 else s[:137] + "..."


def _for_llm(res: dict[str, Any]) -> str:
    slim = {k: v for k, v in res.items() if k in ("ok", "exit_code", "output", "error", "hint", "dry_run", "declined",
                                                  "blocked", "commit_message", "cloned_to", "missing", "attempts")}
    if len(slim.get("output", "")) > 4500:
        o = slim["output"]
        slim["output"] = o[:3000] + "\n...[truncated]...\n" + o[-1500:]
    return json.dumps(slim, ensure_ascii=False)


class Agent:
    """Chat/plan/command loop on top of the router and the tool registry."""

    def __init__(self, router: Optional[Router], mem: Memory, use_llm: bool = True) -> None:
        self.router = router
        self.mem = mem
        self.use_llm = use_llm and router is not None
        self.messages: list[dict[str, Any]] = []
        self.sticky: set[str] = set()

    # ------------------------------------------------------------ helpers
    def system_prompt(self) -> dict[str, str]:
        return {"role": "system", "content": SYSTEM_PROMPT.format(
            os=f"{platform.system()} {platform.release()}", cwd=STATE.cwd, dry="ON" if STATE.dry_run else "off",
            memory=self.mem.summary())}

    def _history(self) -> list[dict[str, Any]]:
        msgs = self.messages[-HISTORY_LIMIT:]
        while msgs and msgs[0]["role"] != "user":
            msgs = msgs[1:]
        return msgs

    def run_tool(self, name: str, args: dict[str, Any], show_args: bool = True) -> dict[str, Any]:
        """Execute a tool, print its real output, update memory."""
        print(ui.c(f"▸ {name}({_fmt_args(args)})" if show_args else f"▸ {name}", "cyan"))
        res = registry.execute(name, args)
        body = res.get("output") or ""
        if body and not (res.get("dry_run") and body.startswith("[dry-run] would run:")):
            print(ui.clip_lines(body, 45))  # (the runner already printed single dry-run lines)
        if res.get("hint"):
            ui.warn(f"hint: {res['hint']}")
        if res["ok"]:
            ui.good(f"✓ {name} ok" + (" (dry-run)" if res.get("dry_run") else ""))
        else:
            ui.error(f"✗ {name} failed (exit {res.get('exit_code')}): {res.get('error', '')[:600]}")
        self.mem.record_tool(name)
        t = registry.get(name)
        if t:
            self.sticky = {t.category}
            if t.category == "git" and res["ok"]:
                self.mem.set("last_repo", STATE.cwd)
        return res

    # ------------------------------------------------------------ LLM loop
    def ask(self, text: str, tools_enabled: bool = True) -> None:
        """One user turn: LLM <-> tools until the LLM answers in text."""
        if not self.use_llm or self.router is None:
            return self.offline_turn(text)
        self.messages.append({"role": "user", "content": text})
        complexity = classify(text) if config.get_config().complexity_routing else "simple"
        schemas = None
        if tools_enabled:
            short = len(text.split()) < 6
            schemas = [t.schema() for t in registry.select(text, self.sticky if short else ())]
        for _ in range(MAX_ROUNDS):
            try:
                resp = self.router.chat([self.system_prompt()] + self._history(), schemas, complexity)
            except AllProvidersFailed as e:
                if e.unreachable:
                    ui.dim("(no LLM reachable right now; providers are re-checked automatically every 60s)")
                else:
                    ui.error("No LLM could answer: " + "; ".join(f"{p}: {r}" for p, r in e.attempts)[:400])
                self.messages.pop() if self.messages and self.messages[-1]["role"] == "user" else None
                return self.offline_turn(text, after_failure=True)
            calls = resp["tool_calls"]
            if not calls:
                if resp["content"]:
                    print("\n" + resp["content"] + "\n")
                self.messages.append({"role": "assistant", "content": resp["content"]})
                return
            self.messages.append({"role": "assistant", "content": resp["content"] or "", "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]})
            if resp["content"]:
                ui.dim(resp["content"])
            for c in calls:
                res = self.run_tool(c["name"], c["arguments"])
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                                      "content": _for_llm(res)})
        ui.warn(f"Stopped after {MAX_ROUNDS} tool rounds. Ask me to continue if needed.")

    # ------------------------------------------------------------ offline
    def offline_turn(self, text: str, after_failure: bool = False) -> None:
        """No LLM available: direct command / intent matcher / honest message."""
        m = offline.match(text)
        if m:
            name, args = m
            if not after_failure:
                ui.dim("(offline mode: no LLM reachable, using the built-in intent matcher)")
            else:
                ui.dim("(falling back to the built-in intent matcher)")
            self.run_tool(name, args)
            return
        ui.warn("No LLM is available and I could not map that to a tool.\n"
                "  - Type a direct command (e.g. `git status`, `docker ps`, `df -h`)\n"
                "  - Use `/tool <name> key=value` (see /tools)\n"
                "  - Get an LLM: install Ollama (`ollama pull qwen2.5:7b`) or add a free key to .env (see /providers)")

    # ------------------------------------------------------------ plan mode
    def plan(self, goal: str) -> None:
        """Generate a step-by-step plan, confirm, then execute step by step."""
        if not goal.strip():
            return ui.warn("Usage: plan: <goal>   e.g. plan: deploy my app")
        if not self.use_llm or self.router is None:
            return ui.warn("Plan mode needs an LLM to write the plan. See /providers. "
                           "You can still run tools directly with /tool.")
        catalog = "\n".join(f"- {t.name}({', '.join(t.properties)}): {t.description[:90]}" for t in registry.all_tools())
        system = ("You are a DevOps planner. Produce a plan as JSON ONLY, no prose: "
                  '{"steps":[{"title":"short description","tool":"tool_name","args":{...}}]}. '
                  "Use only tools from the catalog and only argument values known NOW (no placeholders). "
                  "Prefer read-only checks first. 3-10 steps. Working directory: " + STATE.cwd + f". Memory: {self.mem.summary()}")
        text = self.router.complete_text(system, f"Goal: {goal}\n\nTool catalog:\n{catalog}", "complex")
        steps: list[dict[str, Any]] = []
        if text:
            m = re.search(r"\{.*\}", text, re.S)
            try:
                raw = json.loads(m.group(0)) if m else {}
                steps = [s for s in raw.get("steps", []) if isinstance(s, dict) and registry.get(str(s.get("tool", "")))]
            except ValueError:
                steps = []
        if not steps:
            return ui.error("Could not obtain a valid plan from the LLM. Try rephrasing, or run tools directly.")
        ui.info(ui.c(f"\nPlan: {goal}", "bold"))
        for i, s in enumerate(steps, 1):
            print(f"  {i}. {s.get('title', s['tool'])}  {ui.c(s['tool'] + '(' + _fmt_args(s.get('args') or {}) + ')', 'dim')}")
        if STATE.dry_run:
            ui.warn("DRY-RUN is on: steps will be shown, not executed.")
        if not confirm("\nExecute this plan?"):
            return ui.info("Plan cancelled.")
        done = 0
        for i, s in enumerate(steps, 1):
            ui.info(ui.c(f"\n[{i}/{len(steps)}] {s.get('title', s['tool'])}", "bold"))
            res = self.run_tool(s["tool"], dict(s.get("args") or {}))
            if res["ok"]:
                done += 1
                continue
            if not confirm(f"Step {i} failed. Continue with the remaining steps?"):
                break
        ui.info(ui.c(f"\nPlan finished: {done}/{len(steps)} step(s) succeeded.", "green" if done == len(steps) else "yellow"))

    # ------------------------------------------------------------ slash commands
    def slash(self, line: str) -> bool:
        """Handle /commands. Returns False to quit."""
        parts = shlex.split(line) if line.count('"') % 2 == 0 and line.count("'") % 2 == 0 else line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/help":
            print(HELP)
        elif cmd == "/tools":
            self.print_tools(args[0] if args else None)
        elif cmd == "/status":
            self.print_status()
        elif cmd == "/clear":
            self.messages.clear()
            self.sticky.clear()
            ui.info("Conversation cleared (workspace memory kept).")
        elif cmd == "/dry-run":
            STATE.dry_run = (args[0].lower() in ("on", "true", "1")) if args else not STATE.dry_run
            ui.info(f"Dry-run is now {'ON: nothing will be executed' if STATE.dry_run else 'off'}.")
        elif cmd == "/plan":
            self.plan(" ".join(args))
        elif cmd == "/model":
            self.cmd_model(args)
        elif cmd == "/providers":
            self.cmd_providers(refresh="--refresh" in args or "-r" in args)
        elif cmd == "/cd":
            p = Path(os.path.expanduser(args[0] if args else "~"))
            p = p if p.is_absolute() else Path(STATE.cwd) / p
            if p.is_dir():
                STATE.cwd = str(p.resolve())
                os.chdir(STATE.cwd)
                self.mem.set("cwd", STATE.cwd)
                ui.info(f"Working directory: {STATE.cwd}")
            else:
                ui.error(f"Not a directory: {p}")
        elif cmd == "/watch":
            self.run_tool("watch_system", {"interval": int(args[0]) if args and args[0].isdigit() else 0})
        elif cmd == "/tool":
            self.cmd_tool(args)
        else:
            ui.warn(f"Unknown command {cmd}. Try /help.")
        return True

    def cmd_tool(self, args: list[str]) -> None:
        if not args:
            return ui.warn("Usage: /tool <name> key=value ...   (see /tools)")
        kv: dict[str, Any] = {}
        for a in args[1:]:
            if "=" in a:
                k, _, v = a.partition("=")
                kv[k] = v
        self.run_tool(args[0], kv)

    def print_tools(self, cat: Optional[str] = None) -> None:
        cur = None
        for t in registry.all_tools():
            if cat and t.category != cat:
                continue
            if t.category != cur:
                cur = t.category
                print(ui.c(f"\n[{cur}]", "bold"))
            flag = ui.c(" (confirms)", "yellow") if t.destructive else ""
            print(f"  {t.name:<22} {t.description[:88]}{flag}")
        print(f"\n{len(registry.all_tools())} tools. Run directly: /tool <name> key=value")

    def print_status(self) -> None:
        r = self.router
        names = r.order("simple") if r else []
        print(f"Open Co-Work {__version__}  |  Python {platform.python_version()}  |  {platform.system()}")
        print(f"Working dir : {STATE.cwd}")
        print(f"Dry-run     : {'ON' if STATE.dry_run else 'off'}")
        print(f"LLM         : " + (", ".join(names[:5]) + (f" (+{len(names) - 5})" if len(names) > 5 else "") if names and self.use_llm
                                   else "none - offline mode (direct commands + intent matcher)"))
        print(f"Memory      : {self.mem.summary()}")
        print(f"Tools       : {len(registry.all_tools())}   Audit log: {config.AUDIT_LOG}")
        if registry.LOAD_ERRORS:
            ui.warn(f"Tool modules failed to load: {registry.LOAD_ERRORS}")

    def cmd_providers(self, refresh: bool = False) -> None:
        if not self.router:
            return ui.info("LLM disabled (--no-llm).")
        if refresh or not self.router.health:
            ui.dim("Pinging providers in parallel...")
            self.router.refresh_health()
        print(ui.table(["provider", "cost", "where", "state", "model", "last used", "last error"], self.router.status_rows()))
        print("\nOrder:", " > ".join(self.router.order("simple")) or "(none configured)")

    def cmd_model(self, args: list[str]) -> None:
        if not self.router:
            return ui.info("LLM disabled (--no-llm).")
        if not args:
            prim = config.get_config().llm_primary or "(none; using LLM_ORDER)"
            print(f"Primary: {prim}")
            for n, mdl in self.router.last_model.items():
                print(f"  last model used on {n}: {mdl}")
            return print("Switch: /model <provider>[:<model>]   e.g. /model groq   /model ollama:qwen2.5:7b")
        name, _, mdl = args[0].partition(":")
        if self.router.set_primary(name.lower(), mdl):
            ui.good(f"Primary provider is now {name}" + (f" with model {mdl}" if mdl else ""))
        else:
            ui.error(f"Unknown provider '{name}'. Loaded: {', '.join(sorted(self.router.providers))}")

    # ------------------------------------------------------------ dispatch
    def handle(self, line: str) -> bool:
        """Handle one line of input. Returns False to quit."""
        line = line.strip()
        if not line:
            return True
        if line.startswith("/") and not line.startswith("/tmp") and not line.startswith("/var") and not line.startswith("/etc"):
            return self.slash(line)
        if line.startswith("!"):
            self.run_tool("run_command", {"command": line[1:].strip()})
            return True
        if line.lower() in ("exit", "quit", "q"):
            return False
        if PLAN_RE.match(line):
            self.plan(PLAN_RE.sub("", line, count=1))
            return True
        if CHAT_RE.match(line):
            self.ask(CHAT_RE.sub("", line, count=1), tools_enabled=False)
            return True
        self.ask(line)
        return True


HELP = """Commands
  /help                 this help
  /tools [category]     list tools (git docker cloud system files dev scaffold)
  /status               environment, LLM and memory status
  /providers [-r]       LLM providers: health, cooldowns, last used (-r re-ping)
  /model [prov[:model]] show or switch the primary provider/model
  /plan <goal>          plan -> confirm -> execute step by step  (also: "plan: deploy my app")
  /dry-run [on|off]     toggle dry-run: show what WOULD run, run nothing
  /clear                clear the conversation (memory kept)
  /cd <dir>             change working directory
  /watch [seconds]      monitoring: disk>90%, mem>90%, containers down
  /tool <name> k=v ...  run a tool directly (works without any LLM)
  /quit                 exit
Also: "chat: <text>" talks without tools, "!<cmd>" runs a shell command through the safety layer.
Try: "git status", "commit my work", "why is my disk full?", "docker ps", "fix it", "plan: deploy my app"."""


# ====================================================================== CLI
def print_full_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
    print("\nTOOLS (" + str(len(registry.all_tools())) + ")")
    cur = None
    for t in registry.all_tools():
        if t.category != cur:
            cur = t.category
            print(f"\n  [{cur}]")
        print(f"    {t.name:<22} {t.description[:90]}")
    print(f"\n{HELP}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent.py", add_help=False, description="Open Co-Work: AI DevOps co-worker for your terminal.")
    p.add_argument("prompt", nargs="*", help="one-shot request (omit for interactive REPL)")
    p.add_argument("-h", "--help", action="store_true", help="show this help and list all tools")
    p.add_argument("--dry-run", action="store_true", help="show what WOULD run, do not run anything")
    p.add_argument("--test", action="store_true", help="run the self-test and print a PASS/FAIL table")
    p.add_argument("--no-llm", action="store_true", help="never use an LLM (direct commands + intent matcher only)")
    p.add_argument("--version", action="store_true")
    return p


def startup_banner(agent: Agent) -> None:
    ui.info(ui.c(f"Open Co-Work {__version__}", "bold") + "  - the LLM translates, the tools do the work.")
    r = agent.router
    if not agent.use_llm or r is None:
        ui.warn("LLM disabled: offline mode (direct commands, /tool, intent matcher).")
    else:
        if not r.order():
            ui.warn("No LLM configured. Running in offline mode. Options: install Ollama (free, local) or add a free\n"
                    "  key (GROQ_API_KEY, GEMINI_API_KEY, CEREBRAS_API_KEY...) to .env. See /providers.")
        else:
            r.refresh_health()
            up = [n for n in r.order() if r.health.get(n)]
            if up:
                ui.info(f"LLM ready: {', '.join(up[:4])}" + (" ..." if len(up) > 4 else "") + "   (/providers for details)")
            else:
                ui.warn("Configured LLM providers are not reachable right now -> offline mode until one recovers "
                        "(it is retried automatically). See /providers.")
    if STATE.dry_run:
        ui.warn("DRY-RUN: nothing will be executed.")
    ui.dim(f"cwd: {STATE.cwd}   |   /help for commands   |   {platform.system()} {platform.machine()}")


def repl(agent: Agent) -> int:
    try:
        import readline  # noqa: F401  (line editing + history)
        hist = config.DATA_DIR / "history"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            readline.read_history_file(hist)
        except OSError:
            pass
        import atexit
        atexit.register(lambda: readline.write_history_file(hist))
    except ImportError:
        pass
    startup_banner(agent)
    while True:
        try:
            line = input(ui.c(f"\ncowork{' [dry-run]' if STATE.dry_run else ''}> ", "green"))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("\n(Ctrl+C: type /quit to exit)")
            continue
        try:
            if not agent.handle(line):
                break
        except KeyboardInterrupt:
            ui.warn("\nInterrupted.")
        except Exception as e:  # never crash the REPL
            ui.error(f"Internal error: {type(e).__name__}: {e}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry.load_all()
    if args.version:
        print(f"Open Co-Work {__version__}")
        return 0
    if args.help:
        print_full_help(parser)
        return 0
    if args.test:
        from selftest import run_self_test
        return run_self_test()
    STATE.dry_run = args.dry_run
    mem = Memory()
    saved = mem.get("cwd")
    if saved and os.path.isdir(saved) and os.getcwd() == str(config.ROOT):
        STATE.cwd = saved  # started from the project dir: resume last workspace
    os.chdir(STATE.cwd)
    router = None if args.no_llm else Router()
    agent = Agent(router, mem, use_llm=not args.no_llm)
    mem.set("cwd", STATE.cwd)
    if args.prompt:
        if router:
            router.refresh_health()
        agent.handle(" ".join(args.prompt))
        return 0
    return repl(agent)


if __name__ == "__main__":
    sys.exit(main())
