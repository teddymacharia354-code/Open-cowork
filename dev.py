"""Dev tools: run any command (safety-checked), run tests, install deps, and run-with-autofix
(read error -> LLM patches the file -> retry, max 3 attempts, backups kept)."""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path
from typing import Any, Optional

import ui
from tools._core import STATE, audit, confirm, have, missing_binary, resolve_path, result, run_cmd
from tools.registry import tool


@tool("run_command", "Run a shell command (hard-blocked if dangerous, confirmation for destructive ones). "
      "Use when no dedicated tool exists.",
      {"command": ("string", "Shell command"), "cwd": ("string", "Working directory"),
       "timeout": ("integer", "Seconds (default 120)")}, required=["command"], category="dev", destructive=True)
def run_command(command: str, cwd: Optional[str] = None, timeout: int = 120) -> dict[str, Any]:
    """Run via the user's shell under the safety policy."""
    return run_cmd(command, tool="run_command", cwd=str(resolve_path(cwd)), timeout=timeout, shell=True)


def _py(cwd: Path) -> str:
    for cand in (cwd / ".venv" / "bin" / "python", cwd / "venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return "python3" if have("python3") else sys.executable


@tool("run_tests", "Run the project's tests (pytest, npm test or go test) and show the real output.",
      {"path": ("string", "Project dir (default cwd)"),
       "framework": ("string", "auto|pytest|npm|go", ["auto", "pytest", "npm", "go"]),
       "args": ("string", "Extra args")}, category="dev")
def run_tests(path: Optional[str] = None, framework: str = "auto", args: str = "") -> dict[str, Any]:
    """Detect the framework and run it."""
    d = resolve_path(path)
    if framework == "auto":
        if (d / "package.json").exists():
            framework = "npm"
        elif (d / "go.mod").exists():
            framework = "go"
        elif any((d / f).exists() for f in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py", "tests", "test")) \
                or list(d.glob("test_*.py")):
            framework = "pytest"
        else:
            return result(False, error=f"No tests detected in {d} (looked for pytest/npm/go).", exit_code=1)
    extra = args.split() if args else []
    if framework == "pytest":
        cmd = [_py(d), "-m", "pytest", "-q", *extra]
    elif framework == "npm":
        if not have("npm"):
            return missing_binary("npm")
        cmd = ["npm", "test", "--silent", *(["--", *extra] if extra else [])]
    else:
        if not have("go"):
            return missing_binary("go")
        cmd = ["go", "test", *(extra or ["./..."])]
    return run_cmd(cmd, tool="run_tests", cwd=str(d), timeout=900)


@tool("install_deps", "Install dependencies with pip, npm or apt (apt always asks for confirmation).",
      {"manager": ("string", "auto|pip|npm|apt", ["auto", "pip", "npm", "apt"]),
       "packages": ("array", "Packages (omit to install from requirements.txt / package.json)"),
       "path": ("string", "Project dir (default cwd)")}, category="dev")
def install_deps(manager: str = "auto", packages: Optional[list[str]] = None, path: Optional[str] = None) -> dict[str, Any]:
    """Install packages for the detected ecosystem."""
    d = resolve_path(path)
    if manager == "auto":
        manager = "npm" if (d / "package.json").exists() and not (d / "requirements.txt").exists() else "pip"
    if manager == "pip":
        cmd = [_py(d), "-m", "pip", "install"] + (packages or (["-r", "requirements.txt"] if (d / "requirements.txt").exists() else []))
        if cmd[-1] == "install":
            return result(False, error="No packages given and no requirements.txt found.", exit_code=2)
    elif manager == "npm":
        if not have("npm"):
            return missing_binary("npm")
        cmd = ["npm", "install", *(packages or [])]
    elif manager == "apt":
        if not packages:
            return result(False, error="apt needs package names", exit_code=2)
        if not have("apt-get"):
            return result(False, error="apt not available on this OS (macOS: use `brew install`).", exit_code=127)
        if not STATE.dry_run and not confirm(f"Install system packages with apt: {' '.join(packages)}?"):
            return result(False, error="Cancelled by user.", exit_code=-2, declined=True)
        import os
        cmd = (["sudo"] if hasattr(os, "geteuid") and os.geteuid() != 0 and have("sudo") else []) + \
              ["apt-get", "install", "-y", *packages]
    else:
        return result(False, error=f"Unknown manager '{manager}'", exit_code=2)
    return run_cmd(cmd, tool="install_deps", cwd=str(d), timeout=1200, confirmed=(manager == "apt"))


def _guess_file(output: str, cwd: Path) -> Optional[Path]:
    """Find the last project file mentioned in a traceback/stack trace."""
    cands = re.findall(r'File "([^"]+)", line \d+', output) + re.findall(r"(\S+\.(?:js|ts|mjs|go|rb|php|sh)):\d+", output) + \
        re.findall(r"^(\S+\.py):\d+", output, re.M)
    for c in reversed(cands):
        p = Path(c) if Path(c).is_absolute() else cwd / c
        try:
            if p.is_file() and cwd.resolve() in p.resolve().parents and "site-packages" not in str(p) and "node_modules" not in str(p):
                return p
        except OSError:
            continue
    return None


@tool("run_with_autofix", "Run a command; if it fails, read the error, have the LLM patch the offending file, and retry "
      "(max 3 attempts, backups kept). Use for 'create it, run it, fix errors'.",
      {"command": ("string", "Command to run, e.g. 'python app.py'"), "file": ("string", "File to patch (default: detected from the traceback)"),
       "cwd": ("string", "Working directory"), "max_attempts": ("integer", "Attempts (default 3, max 3)")},
      required=["command"], category="dev")
def run_with_autofix(command: str, file: Optional[str] = None, cwd: Optional[str] = None,
                     max_attempts: int = 3) -> dict[str, Any]:
    """Run -> on failure patch -> rerun."""
    wd = resolve_path(cwd)
    max_attempts = max(1, min(max_attempts, 3))
    history: list[str] = []
    last: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 2):
        last = run_cmd(command, tool="run_with_autofix", cwd=str(wd), shell=True, timeout=120)
        if last["ok"] or STATE.dry_run:
            history.append(f"attempt {attempt}: " + ("OK" if last["ok"] else "dry-run"))
            return result(last["ok"], output="\n".join(history) + "\n\n" + last["output"], error=last["error"],
                          exit_code=last["exit_code"], attempts=attempt)
        history.append(f"attempt {attempt}: FAILED (exit {last['exit_code']})")
        if attempt > max_attempts:
            break
        err_text = (last["error"] or last["output"])[-3000:]
        target = resolve_path(file) if file else _guess_file(err_text + "\n" + last["output"], wd)
        if not target or not Path(target).is_file():
            history.append("could not determine which file to patch; stopping")
            break
        before = Path(target).read_text(encoding="utf-8", errors="replace")
        from llm import get_router
        reply = get_router().complete_text(
            "You are a precise code fixer. Given a source file and the error produced when running it, reply with the "
            "COMPLETE corrected file inside a single fenced code block and nothing else. Change only what is needed.",
            f"Command: {command}\nFile: {target}\n\nError output:\n{err_text}\n\nCurrent file:\n{before[:12000]}", "complex")
        if not reply:
            history.append("no LLM available to propose a fix; stopping")
            break
        m = re.search(r"```[\w+-]*\n(.*?)```", reply, re.S)
        after = (m.group(1) if m else reply).rstrip("\n") + "\n"
        if after.strip() == before.strip():
            history.append("LLM proposed no change; stopping")
            break
        from tools.files import _backup
        bk = _backup(Path(target))
        Path(target).write_text(after, encoding="utf-8")
        audit("run_with_autofix", f"patched {target} (backup {bk})", 0)
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "before", "after"))
        ui.info(f"Patched {target} (backup: {bk})\n{diff[:1500]}")
        history.append(f"patched {Path(target).name}")
    return result(False, output="\n".join(history) + "\n\n" + last.get("output", ""), error=last.get("error", ""),
                  exit_code=last.get("exit_code", 1))
