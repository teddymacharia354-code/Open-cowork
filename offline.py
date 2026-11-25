"""Offline intent matcher used when NO LLM is reachable (no keys, no Ollama, no network).

It is intentionally simple: regex -> (tool, args). Anything it cannot match is
reported honestly instead of guessed. Raw commands starting with a known binary
(``git status``, ``docker ps`` ...) are passed through the safety-checked runner.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_RAW_BINARIES = ("git", "docker", "docker-compose", "kubectl", "terraform", "aws", "gh", "systemctl", "journalctl",
                 "df", "free", "uptime", "ps", "ls", "cat", "grep", "rg", "ping", "curl", "pytest", "npm", "pip",
                 "python", "python3", "node", "go", "du", "top", "ss", "lsof", "which")

_RULES: list[tuple[str, str, Any]] = [
    (r"\bgit status\b|\bstatus of (?:the |my )?repo", "git_status", {}),
    (r"\bgit diff\b|\bwhat changed\b", "git_diff", {}),
    (r"\bgit log\b|\brecent commits\b|\bcommit history\b", "git_log", {}),
    (r"\bgit branch(?:es)?\b|\blist branches\b", "git_branch", {}),
    (r"\bgit fetch\b", "git_fetch", {}),
    (r"\bgit pull\b|\bpull (?:the )?latest\b", "git_pull", {}),
    (r"\bgit push\b|\bpush (?:my|the) (?:work|code|changes)\b", "git_push", {}),
    (r"\bcommit\b", "git_commit", {"add_all": True}),
    (r"\bgit auth\b|\bgit credentials?\b", "git_auth_status", {}),
    (r"\bdocker (?:ps|containers?)\b|\b(?:list|show) (?:the )?containers\b", "docker_ps", {"all": True}),
    (r"\bdocker images\b|\blist images\b", "docker_images", {}),
    (r"\bdocker stats\b", "docker_stats", {}),
    (r"\bcontainer health\b|\bhealth ?check\b.*\bcontainers?\b|\bdocker health\b", "docker_health", {}),
    (r"\bfix it\b|\brestart crashed\b", "docker_fix", {}),
    (r"\bdocker logs? (\S+)", "docker_logs", "container"),
    (r"\blogs? (?:of|for|from) (\S+)", "docker_logs", "container"),
    (r"\bpublic ip\b|\bmy ip\b", "public_ip", {}),
    (r"\bping (\S+)", "ping_host", "host"),
    (r"\bcheck ports? (?:on )?(\S+)", "port_check", "host"),
    (r"\bport (\d{1,5})\b.*\b(?:owner|using|used|listening)\b|\bwho(?:'s| is) (?:using|on) port (\d{1,5})", "port_owner", "port"),
    (r"\bcheck (https?://\S+)|\bhealth of (https?://\S+)", "http_check", "url"),
    (r"\btoolchain\b|\bwhat(?:'s| is) (?:installed|missing)\b|\bwhich tools\b", "toolchain", {}),
    (r"\bdisk\b|\bmemory\b|\bram\b|\bcpu\b|\bsystem (?:overview|status|info)\b|\boverview\b|\buptime\b|\bserver health\b",
     "system_overview", {}),
    (r"\blarge files?\b|\bbiggest files?\b", "find_large_files", {}),
    (r"\bwatch\b|\bmonitor\b", "watch_system", {}),
    (r"\bs3\b", "aws_s3_ls", {}),
    (r"\bec2\b|\baws instances?\b", "aws_ec2_list", {}),
    (r"\bsecurity groups?\b", "aws_security_groups", {}),
    (r"\bebs\b", "aws_ebs", {}),
    (r"\baws cost\b|\bbilling\b", "aws_cost", {}),
    (r"\brun (?:the )?tests?\b|\bpytest\b", "run_tests", {}),
    (r"\bcat (\S+)|\bshow (?:me )?(?:the )?file (\S+)|\bread (\S+)", "cat", "path"),
    (r"\bls\b|\blist (?:the )?(?:files|directory|folder)\b", "ls", {}),
]


def match(text: str) -> Optional[tuple[str, dict[str, Any]]]:
    """Return (tool_name, args) for ``text`` or None."""
    t = text.strip()
    first = t.split()[0] if t.split() else ""
    if first in _RAW_BINARIES and " " in t:
        return "run_command", {"command": t}
    low = t.lower()
    for pattern, tool_name, arg in _RULES:
        m = re.search(pattern, low)
        if not m:
            continue
        if isinstance(arg, dict):
            return tool_name, dict(arg)
        value = next((g for g in m.groups() if g), None)
        if value is None:
            continue
        orig = re.search(re.escape(value), t, re.I)
        return tool_name, {arg: orig.group(0) if orig else value}
    return None
