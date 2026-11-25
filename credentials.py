"""Credential handling: secret redaction and git authentication.

Lookup order for HTTPS git auth (never printed, never logged):
  1. OS keyring (service ``open-cowork-git``)
  2. env vars ``GIT_USERNAME`` / ``GIT_TOKEN`` (from ``.env``, user's own risk)
  3. the user's configured git credential helper (``git credential fill``)
  4. interactive prompt (getpass), optionally saved to the keyring

SSH remotes are detected and pushed with the user's ssh-agent / keys as-is.
"""
from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse

import config
import ui

KEYRING_SERVICE = "open-cowork-git"
REDACTED = "***REDACTED***"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bgsk_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)[^\s'\"]+"), r"\1" + REDACTED),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + REDACTED + r"\2"),
    (re.compile(
        r"(?i)\b((?:password|passwd|token|secret|api[_-]?key|access[_-]?key|"
        r"secret[_-]?access[_-]?key)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]+"), r"\1" + REDACTED),
]
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD", re.I)
_REGISTERED: set[str] = set()


def register_secret(value: str) -> None:
    """Remember a secret so it is scrubbed from all output and logs."""
    if value and len(value) >= 4:
        _REGISTERED.add(value)


def _known_secrets() -> list[str]:
    vals = set(_REGISTERED)
    for k, v in os.environ.items():
        if v and len(v) >= 8 and _SECRET_ENV.search(k):
            vals.add(v)
    return sorted(vals, key=len, reverse=True)


def redact(text: str) -> str:
    """Return ``text`` with tokens, passwords and API keys masked."""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    for secret in _known_secrets():
        text = text.replace(secret, REDACTED)
    return text


# --------------------------------------------------------------------------- git auth
@dataclass
class GitCreds:
    """A username/secret pair plus where it came from (never printed)."""

    username: str
    secret: str
    source: str

    def __repr__(self) -> str:
        return f"GitCreds(username={self.username!r}, source={self.source!r})"


def is_ssh_url(url: str) -> bool:
    """True for ssh-style remotes (git@host:x/y.git, ssh://...)."""
    return url.startswith("ssh://") or bool(re.match(r"^[\w.-]+@[\w.-]+:", url))


def host_of(url: str) -> str:
    """Hostname of an http(s) git URL ('' if not parseable)."""
    return (urlparse(url).hostname or "").lower()


def label_for(host: str) -> str:
    """Human label for a git host."""
    if "github" in host:
        return "GitHub"
    if "gitlab" in host:
        return "GitLab"
    if "bitbucket" in host:
        return "Bitbucket"
    return host or "git"


def detect_ssh_keys() -> list[str]:
    """Names of private SSH keys found in ~/.ssh."""
    ssh = Path.home() / ".ssh"
    if not ssh.is_dir():
        return []
    names = ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa")
    return [n for n in names if (ssh / n).is_file()]


def keyring_available() -> bool:
    """True if a usable (non-fail) OS keyring backend exists."""
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except Exception:
        return False


def keyring_get(host: str) -> Optional[GitCreds]:
    """Fetch stored credentials for ``host`` from the OS keyring."""
    if not keyring_available():
        return None
    try:
        import keyring  # type: ignore
        raw = keyring.get_password(KEYRING_SERVICE, host)
        if raw:
            d = json.loads(raw)
            return GitCreds(d["u"], d["t"], "keyring")
    except Exception:
        return None
    return None


def keyring_set(host: str, username: str, secret: str) -> bool:
    """Store credentials in the OS keyring. Returns success."""
    if not keyring_available():
        return False
    try:
        import keyring  # type: ignore
        keyring.set_password(KEYRING_SERVICE, host, json.dumps({"u": username, "t": secret}))
        return True
    except Exception:
        return False


def env_creds(host: str) -> Optional[GitCreds]:
    """Credentials from GIT_USERNAME/GIT_TOKEN, only for allow-listed hosts."""
    cfg = config.get_config()
    if cfg.git_username and cfg.git_token and host in cfg.git_cred_hosts:
        return GitCreds(cfg.git_username, cfg.git_token, "env")
    return None


def helper_creds(url: str) -> Optional[GitCreds]:
    """Ask the user's git credential helper (non-interactively)."""
    p = urlparse(url)
    if not p.hostname:
        return None
    payload = f"protocol={p.scheme}\nhost={p.hostname}\n\n"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        r = subprocess.run(["git", "credential", "fill"], input=payload, env=env,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if kv.get("username") and kv.get("password"):
        return GitCreds(kv["username"], kv["password"], "git-credential-helper")
    return None


def lookup(url: str) -> Optional[GitCreds]:
    """Find stored credentials for an https remote (keyring -> env -> helper)."""
    host = host_of(url)
    if not host:
        return None
    creds = keyring_get(host) or env_creds(host) or helper_creds(url)
    if creds:
        register_secret(creds.secret)
    return creds


def prompt_credentials(url: str, operation: str = "Push") -> Optional[GitCreds]:
    """Ask the user for credentials in the terminal (token hidden)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    label = label_for(host_of(url))
    try:
        user = input(f"{operation} needs auth. Enter {label} username: ").strip()
        secret = getpass.getpass("Enter token/password (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not user or not secret:
        return None
    register_secret(secret)
    return GitCreds(user, secret, "prompt")


def offer_save(url: str, creds: GitCreds) -> None:
    """After a successful prompted auth, offer to save to the keyring."""
    if creds.source != "prompt":
        return
    host = host_of(url)
    cfg = config.get_config()
    if cfg.git_save_plaintext:
        ui.warn("GIT_SAVE_PLAINTEXT=true: saving credentials in PLAINTEXT to .env (chmod 600). "
                "Never commit .env.")
        _save_plaintext(creds)
        return
    try:
        ans = input("Save to keyring for next time? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans in ("y", "yes"):
        if keyring_set(host, creds.username, creds.secret):
            ui.good(f"Saved to OS keyring for {host}.")
        else:
            ui.warn("No usable OS keyring backend (pip install keyring / install a Secret Service). "
                    "Not saved. Set GIT_SAVE_PLAINTEXT=true to opt in to plaintext storage.")


def _save_plaintext(creds: GitCreds) -> None:
    path = config.ENV_FILE
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith(("GIT_USERNAME=", "GIT_TOKEN="))]
    lines += [f"GIT_USERNAME={creds.username}", f"GIT_TOKEN={creds.secret}"]
    path.write_text("\n".join(lines) + "\n")
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@contextlib.contextmanager
def askpass_env(creds: GitCreds) -> Iterator[dict[str, str]]:
    """Yield env vars that make git use ``creds`` silently via GIT_ASKPASS.

    The secret lives only in the child's environment and a 0700 temp script
    that contains no secret; it is never placed on a command line.
    """
    d = tempfile.mkdtemp(prefix="oc-askpass-")
    script = os.path.join(d, "askpass.sh")
    with open(script, "w") as fh:
        fh.write('#!/bin/sh\ncase "$1" in\n  Username*) printf \'%s\' "$OC_GIT_USER" ;;\n'
                 '  *) printf \'%s\' "$OC_GIT_SECRET" ;;\nesac\n')
    os.chmod(script, 0o700)
    try:
        yield {"GIT_ASKPASS": script, "GIT_TERMINAL_PROMPT": "0", "OC_GIT_USER": creds.username,
               "OC_GIT_SECRET": creds.secret}
    finally:
        with contextlib.suppress(OSError):
            os.remove(script)
            os.rmdir(d)


def auth_status() -> dict[str, object]:
    """Describe which auth methods are available (no secrets)."""
    cfg = config.get_config()
    helper = ""
    try:
        r = subprocess.run(["git", "config", "--get", "credential.helper"],
                           capture_output=True, text=True, timeout=5)
        helper = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "keyring_backend": keyring_available(),
        "env_credentials": bool(cfg.git_username and cfg.git_token),
        "credential_helper": helper or None,
        "ssh_keys": detect_ssh_keys(),
        "gh_cli_authenticated": _gh_authed(),
        "prompt_fallback": True,
    }


def _gh_authed() -> bool:
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
