"""Process tools: list processes, kill (confirmed), find the owner of a port."""
from __future__ import annotations

import platform
from typing import Any, Optional

from tools._core import STATE, have, missing_binary, result, run_cmd
from tools.registry import tool


@tool("ps_list", "List processes, optionally filtered by name, sorted by CPU.",
      {"filter": ("string", "Substring of the command name"), "limit": ("integer", "Max rows (default 20)")}, category="system")
def ps_list(filter: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
    """ps sorted by CPU."""
    cmd = ["ps", "-eo", "pid,ppid,user,pcpu,pmem,etime,args", "--sort=-pcpu"] if platform.system() == "Linux" \
        else ["ps", "-Ao", "pid,ppid,user,pcpu,pmem,etime,command", "-r"]
    res = run_cmd(cmd, tool="ps_list")
    if not res["ok"]:
        return res
    lines = res["output"].splitlines()
    head, rows = lines[:1], lines[1:]
    if filter:
        rows = [r for r in rows if filter.lower() in r.lower() and "ps -eo" not in r and "ps -Ao" not in r]
    res["output"] = "\n".join(head + rows[:limit]) if rows else f"No process matching '{filter}'."
    return res


@tool("kill_process", "Send a signal to a process (always asks for confirmation).",
      {"pid": ("integer", "Process id"), "signal": ("string", "TERM (default) or KILL", ["TERM", "KILL", "HUP", "INT"])},
      required=["pid"], category="system", destructive=True)
def kill_process(pid: int, signal: str = "TERM") -> dict[str, Any]:
    """kill -<signal> pid (the safety layer asks first; kill -9 is flagged)."""
    if pid <= 1:
        return result(False, error="Refusing to signal PID 0/1.", exit_code=-1, blocked=True)
    num = {"TERM": "15", "KILL": "9", "HUP": "1", "INT": "2"}.get(signal.upper(), "15")
    return run_cmd(["kill", f"-{num}", str(pid)], tool="kill_process")


@tool("port_owner", "Find which process is listening on a local port.", {"port": ("integer", "TCP port")},
      required=["port"], category="system")
def port_owner(port: int) -> dict[str, Any]:
    """lsof/ss lookup."""
    if have("lsof"):
        res = run_cmd(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], tool="port_owner", ok_codes=(0, 1))
    elif have("ss"):
        res = run_cmd(["ss", "-ltnp", f"sport = :{port}"], tool="port_owner")
    else:
        return missing_binary("lsof")
    if res["ok"] and not STATE.dry_run and len(res["output"].splitlines()) <= 1 and "LISTEN" not in res["output"]:
        res["output"] = f"Nothing is listening on port {port} (or you lack permission to see the owner; try sudo)."
    return res
