"""Shared execution core for every tool: safety policy, confirm, dry-run, audit.

Every subprocess in Open Co-Work goes through :func:`run_cmd`, so the
allowlist/blocklist, destructive-command confirmation, ``--dry-run`` and the
redacted audit log apply uniformly.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence, Union

import config
import ui
from credentials import redact


@dataclass
class RuntimeState:
    """Process-wide runtime switches."""

    dry_run: bool = False
    auto_yes: bool = False          # only used by the internal self-test
    verbose: bool = True
    cwd: str = field(default_factory=os.getcwd)


STATE = RuntimeState()

INSTALL_HINTS: dict[str, str] = {
    "git": "sudo apt install git  |  brew install git",
    "docker": "https://docs.docker.com/engine/install/  |  Debian/Ubuntu: sudo apt install docker.io  |  macOS: brew install --cask docker",
    "python3": "sudo apt install python3  |  brew install python",
    "node": "sudo apt install nodejs  |  brew install node",
    "npm": "sudo apt install npm  |  brew install node",
    "terraform": "https://developer.hashicorp.com/terraform/install  |  brew install terraform",
    "aws": "https://aws.amazon.com/cli/  |  pip install awscli  |  brew install awscli",
    "gh": "https://cli.github.com/  |  sudo apt install gh  |  brew install gh",
    "kubectl": "https://kubernetes.io/docs/tasks/tools/  |  brew install kubectl",
    "helm": "https://helm.sh/docs/intro/install/  |  brew install helm",
    "doctl": "https://docs.digitalocean.com/reference/doctl/how-to/install/  |  brew install doctl",
    "hcloud": "https://github.com/hetznercloud/cli  |  brew install hcloud",
    "gcloud": "https://cloud.google.com/sdk/docs/install  |  brew install --cask google-cloud-sdk",
    "go": "https://go.dev/doc/install  |  sudo apt install golang  |  brew install go",
    "rg": "sudo apt install ripgrep  |  brew install ripgrep",
    "curl": "sudo apt install curl  |  brew install curl",
    "jq": "sudo apt install jq  |  brew install jq",
    "ollama": "https://ollama.com/download  |  curl -fsSL https://ollama.com/install.sh | sh",
    "systemctl": "systemd is Linux-only",
    "lsof": "sudo apt install lsof  |  preinstalled on macOS",
    "ping": "sudo apt install iputils-ping",
}

# ---------------------------------------------------------------- safety policy
_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(?:-\S+\s+)*-\S*[rR]\S*\s+(?:-\S+\s+)*(?:--no-preserve-root\s+)?"
                r"(?:/|/\*|~/?|~/\*|\$HOME/?|/home/?|/etc|/usr|/var|/bin|/sbin|/boot|/lib\S*|/root|/opt)(?:\s|$|;|&|\|)"),
     "recursive delete of a system/home root"),
    (re.compile(r"--no-preserve-root"), "--no-preserve-root"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "mkfs (formats a filesystem)"),
    (re.compile(r"\bdd\s+[^|;&]*\bif="), "dd if= (raw disk copy)"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r">\s*/dev/(?:sd|nvme|hd|vd|xvd|disk)\w*"), "write to raw block device"),
    (re.compile(r"\bchmod\s+(?:-\S+\s+)*[0-7]{3,4}\s+/(?:\s|$)"), "chmod on /"),
    (re.compile(r"\bchown\s+(?:-\S+\s+)*\S+\s+/(?:\s|$)"), "chown on /"),
    (re.compile(r"\bwipefs\b|\bshred\s+[^|;&]*/dev/"), "disk wipe"),
    (re.compile(r"\bmv\s+(?:-\S+\s+)*(?:/|~|/home|/etc|/usr|/var|/bin)\s+\S+"), "moving a system root"),
]

_DESTRUCTIVE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\b"), "rm"),
    (re.compile(r"\bdocker\s+(?:system|volume|network|image|container|builder)\s+prune\b"), "docker prune"),
    (re.compile(r"\bdocker\s+(?:rm|rmi|kill)\b"), "docker rm/rmi/kill"),
    (re.compile(r"(?i)\bdrop\s+(?:database|table|schema|index|user)\b"), "SQL drop"),
    (re.compile(r"\bgit\b[^|;&]*\bpush\b[^|;&]*(?:--force\b|--force-with-lease\b|\s-f\b)"), "git push --force"),
    (re.compile(r"\bgit\s+reset\s+[^|;&]*--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+[^|;&]*-\S*f"), "git clean -f"),
    (re.compile(r"\bgit\s+branch\s+[^|;&]*-D\b"), "git branch -D"),
    (re.compile(r"\b(?:kill|pkill|killall)\b"), "kill"),
    (re.compile(r"\bapt(?:-get)?\s+(?:\S+\s+)*?(?:remove|purge|autoremove)\b"), "apt remove"),
    (re.compile(r"\bterraform\s+(?:\S+\s+)*?destroy\b"), "terraform destroy"),
    (re.compile(r"\bfind\b[^|;&]*\s-delete\b"), "find -delete"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff|halt)\b"), "shutdown/reboot"),
    (re.compile(r"\bjournalctl\b[^|;&]*--vacuum"), "journal vacuum"),
]


def check_blocked(cmd: str) -> Optional[str]:
    """Return the reason if ``cmd`` is hard-blocked by the safety policy."""
    for pat, why in _BLOCKED:
        if pat.search(cmd):
            return why
    return None


def is_destructive(cmd: str) -> Optional[str]:
    """Return a label if ``cmd`` requires explicit user confirmation."""
    for pat, why in _DESTRUCTIVE:
        if pat.search(cmd):
            return why
    return None


# ---------------------------------------------------------------- helpers
def audit(tool: str, command: str, exit_code: Union[int, str]) -> None:
    """Append one redacted line to ``logs/audit.log``."""
    ts = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    cmd = redact(command).replace("\n", "\\n")
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} | {tool} | {cmd} | exit={exit_code}\n")
    except OSError:
        pass


def confirm(prompt: str) -> bool:
    """Ask a yes/no question (default No). Non-interactive sessions decline."""
    if STATE.auto_yes:
        return True
    if not sys.stdin.isatty():
        ui.warn("Confirmation required but stdin is not a terminal; declining.")
        return False
    try:
        return input(ui.c(f"{prompt} [y/N]: ", "yellow")).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def result(ok: bool, output: str = "", error: str = "", exit_code: int = 0, **extra: Any) -> dict[str, Any]:
    """Build the standard tool result dict."""
    d: dict[str, Any] = {"ok": ok, "exit_code": exit_code, "output": output, "error": error}
    d.update(extra)
    return d


def clip(text: str, limit: int = 8000) -> str:
    """Trim long output keeping head and tail."""
    if len(text) <= limit:
        return text
    h = limit * 2 // 3
    t = limit - h
    return text[:h] + f"\n...[{len(text) - limit} chars truncated]...\n" + text[-t:]


def missing_binary(name: str) -> dict[str, Any]:
    """Standard 'not installed' result with an install hint."""
    hint = INSTALL_HINTS.get(name, f"install '{name}' with your package manager")
    return result(False, error=f"{name} not installed, install with: {hint}", exit_code=127, missing=name)


def have(binary: str) -> bool:
    """True if ``binary`` is on PATH."""
    return shutil.which(binary) is not None


def resolve_path(p: Optional[str]) -> Path:
    """Resolve ``p`` relative to the current working dir (expands ``~``)."""
    if not p:
        return Path(STATE.cwd)
    path = Path(os.path.expanduser(p))
    return path if path.is_absolute() else Path(STATE.cwd) / path


def run_cmd(
    cmd: Union[str, Sequence[str]],
    *,
    tool: str,
    cwd: Optional[str] = None,
    timeout: float = 120,
    env: Optional[dict[str, str]] = None,
    input_text: Optional[str] = None,
    shell: bool = False,
    ok_codes: Sequence[int] = (0,),
    confirmed: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    """Run a real subprocess under the safety policy and return a result dict.

    Order of checks: hard block -> dry-run -> missing binary -> destructive
    confirmation -> execute -> audit (redacted).
    """
    cmd_str = cmd if isinstance(cmd, str) else shlex.join(str(c) for c in cmd)
    shown = redact(cmd_str)

    why = check_blocked(cmd_str)
    if why:
        audit(tool, f"{shown} [BLOCKED: {why}]", -1)
        return result(False, error=f"Blocked by safety policy: {why}. This command is never executed.",
                      exit_code=-1, blocked=True)

    if STATE.dry_run:
        ui.dim(f"[dry-run] would run: {shown}")
        audit(tool, shown, "dry-run")
        return result(True, output=f"[dry-run] would run: {shown}", dry_run=True)

    workdir = cwd or STATE.cwd
    if not os.path.isdir(workdir):
        return result(False, error=f"Directory does not exist: {workdir}", exit_code=2)

    if not shell and isinstance(cmd, (list, tuple)) and not shutil.which(str(cmd[0])):
        audit(tool, shown, 127)
        return missing_binary(str(cmd[0]))

    label = is_destructive(cmd_str)
    if label and not confirmed:
        if not confirm(f"DESTRUCTIVE ({label}): {shown}\nProceed?"):
            audit(tool, f"{shown} [DECLINED]", -2)
            return result(False, error="Cancelled: destructive command was not confirmed.",
                          exit_code=-2, declined=True)

    if STATE.verbose and not quiet:
        ui.dim(f"$ {shown}")
    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            cmd_str if shell else list(map(str, cmd)),
            cwd=workdir, env=full_env, shell=shell, input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        audit(tool, shown, "timeout")
        partial = redact((e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""))
        return result(False, output=clip(partial), error=f"Timed out after {timeout:.0f}s", exit_code=124)
    except OSError as e:
        audit(tool, shown, "oserror")
        return result(False, error=f"Could not execute: {e}", exit_code=126)

    out = redact(proc.stdout or "")
    err = redact(proc.stderr or "")
    ok = proc.returncode in ok_codes
    audit(tool, shown, proc.returncode)
    combined = out if not err or not out else out + ("\n" if not out.endswith("\n") else "") + err
    if not out:
        combined = err
    return result(ok, output=clip(combined.strip("\n")),
                  error="" if ok else clip(err.strip() or out.strip() or f"exit code {proc.returncode}", 3000),
                  exit_code=proc.returncode)
