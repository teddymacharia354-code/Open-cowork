"""System tools: overview, toolchain check, logs, cleanup, monitoring (watch mode), systemd and cron."""
from __future__ import annotations

import os
import platform
import re
import shutil
import tempfile
import time
from typing import Any, Optional

import config
import ui
from tools._core import STATE, audit, have, missing_binary, resolve_path, result, run_cmd
from tools.registry import tool

IS_LINUX = platform.system() == "Linux"


def _q(cmd: list[str], name: str = "system", **kw: Any) -> dict[str, Any]:
    return run_cmd(cmd, tool=name, quiet=True, timeout=kw.pop("timeout", 20), **kw)


def _sudo(cmd: list[str]) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    return ["sudo", *cmd] if have("sudo") else cmd


def memory_percent() -> Optional[float]:
    """Used memory in percent (Linux /proc/meminfo, macOS vm_stat). None if unknown."""
    try:
        with open("/proc/meminfo") as fh:
            info = {ln.split(":")[0]: int(ln.split()[1]) for ln in fh if ":" in ln}
        return (1 - info["MemAvailable"] / info["MemTotal"]) * 100
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    if have("vm_stat") and have("sysctl"):
        vm, total = _q(["vm_stat"]), _q(["sysctl", "-n", "hw.memsize"])
        try:
            page = int(re.search(r"page size of (\d+)", vm["output"]).group(1))  # type: ignore[union-attr]
            pages = {k: int(v.strip(" .")) for k, v in (ln.split(":") for ln in vm["output"].splitlines() if ":" in ln)
                     if v.strip(" .").isdigit()}
            avail = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0)) * page
            return (1 - avail / int(total["output"])) * 100
        except (AttributeError, ValueError, ZeroDivisionError):
            return None
    return None


def disk_usage() -> list[tuple[str, float, str]]:
    """(mountpoint, percent_used, filesystem) for real filesystems."""
    r = _q(["df", "-P", "-h"])
    out = []
    for ln in r["output"].splitlines()[1:]:
        p = ln.split()
        if len(p) >= 6 and p[4].endswith("%") and not p[0].startswith(("tmpfs", "devtmpfs", "overlay", "map ", "/dev/loop", "udev")) \
                and not p[5].startswith(("/snap", "/System/Volumes/VM", "/System/Volumes/Preboot", "/boot/efi", "/private/var/vm")):
            out.append((p[5], float(p[4].rstrip("%")), p[0]))
    return out


def check_thresholds() -> list[str]:
    """Return alert strings for disk/mem above threshold and downed containers."""
    cfg = config.get_config()
    alerts = []
    for mount, pct, _ in disk_usage():
        if pct > cfg.disk_threshold:
            alerts.append(f"DISK {mount} at {pct:.0f}% (> {cfg.disk_threshold:.0f}%)")
    mem = memory_percent()
    if mem is not None and mem > cfg.mem_threshold:
        alerts.append(f"MEMORY at {mem:.0f}% (> {cfg.mem_threshold:.0f}%)")
    if have("docker"):
        from tools.docker import containers_down
        alerts += [f"CONTAINER down/unhealthy: {n}" for n in containers_down()]
    return alerts


@tool("system_overview", "Full system overview: disk, memory, CPU load, uptime, top processes, failed systemd units, "
      "listening ports, zombie processes.", {}, category="system")
def system_overview() -> dict[str, Any]:
    """Collect real system metrics from the OS."""
    if STATE.dry_run:
        return run_cmd(["df", "-h"], tool="system_overview")
    sec: list[str] = []
    sec.append("== DISK ==\n" + _q(["df", "-h"])["output"])
    mem = _q(["free", "-h"]) if have("free") else _q(["vm_stat"])
    sec.append("== MEMORY ==\n" + mem["output"])
    try:
        l1, l5, l15 = os.getloadavg()
        sec.append(f"== CPU LOAD ==\n1m {l1:.2f}  5m {l5:.2f}  15m {l15:.2f}  (cores: {os.cpu_count()})")
    except OSError:
        pass
    sec.append("== UPTIME ==\n" + _q(["uptime"])["output"])
    ps_cmd = ["ps", "-eo", "pid,pcpu,pmem,stat,comm", "--sort=-pcpu"] if IS_LINUX else ["ps", "-Ao", "pid,pcpu,pmem,stat,comm", "-r"]
    ps = _q(ps_cmd)
    lines = ps["output"].splitlines()
    sec.append("== TOP PROCESSES (by CPU) ==\n" + "\n".join(lines[:7]))
    zombies = [ln for ln in lines[1:] if len(ln.split()) >= 4 and ln.split()[3].startswith("Z")]
    sec.append(f"== ZOMBIES ==\n{len(zombies)} zombie process(es)" + ("".join("\n" + z for z in zombies[:10])))
    if have("systemctl") and os.path.isdir("/run/systemd/system"):
        failed = _q(["systemctl", "--failed", "--no-legend", "--plain"])["output"].strip()
        sec.append("== FAILED SYSTEMD UNITS ==\n" + (failed or "none"))
    else:
        sec.append("== FAILED SYSTEMD UNITS ==\n(systemd not available on this system)")
    if have("ss"):
        ports = _q(["ss", "-tulpnH"])["output"]
    elif have("lsof"):
        ports = _q(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])["output"]
    else:
        ports = "(neither ss nor lsof installed)"
    sec.append("== LISTENING PORTS ==\n" + "\n".join(ports.splitlines()[:25]))
    alerts = check_thresholds()
    sec.append("== ALERTS ==\n" + ("\n".join(alerts) if alerts else "none (disk/mem below thresholds)"))
    return result(True, output="\n\n".join(sec), alerts=alerts)


_TOOLCHAIN = [("git", ["git", "--version"]), ("docker", ["docker", "--version"]), ("python3", ["python3", "--version"]),
              ("node", ["node", "--version"]), ("npm", ["npm", "--version"]), ("terraform", ["terraform", "version"]),
              ("aws", ["aws", "--version"]), ("gh", ["gh", "--version"]), ("kubectl", ["kubectl", "version", "--client"]),
              ("helm", ["helm", "version", "--short"]), ("go", ["go", "version"]), ("gcloud", ["gcloud", "--version"]),
              ("doctl", ["doctl", "version"]), ("hcloud", ["hcloud", "version"]), ("rg", ["rg", "--version"]),
              ("curl", ["curl", "--version"]), ("jq", ["jq", "--version"]), ("ollama", ["ollama", "--version"])]


@tool("toolchain", "Check which DevOps tools are installed (git, docker, python, node, terraform, aws, gh, kubectl...) "
      "and how to install the missing ones.", {}, category="system")
def toolchain() -> dict[str, Any]:
    """Probe each tool's version."""
    from tools._core import INSTALL_HINTS
    ok_lines, missing = [], []
    for name, cmd in _TOOLCHAIN:
        if not have(name):
            missing.append(f"  MISSING {name:<10} install: {INSTALL_HINTS.get(name, 'see docs')}")
            continue
        v = _q(cmd, "toolchain", timeout=10) if not STATE.dry_run else {"output": "?"}
        ok_lines.append(f"  OK      {name:<10} {(v['output'] or v.get('error', '')).splitlines()[0][:70] if (v['output'] or v.get('error')) else ''}")
    out = "INSTALLED\n" + "\n".join(ok_lines) + ("\n\nMISSING\n" + "\n".join(missing) if missing else "\n\nNothing missing.")
    return result(True, output=out, missing=[m.split()[1] for m in missing])


@tool("logs_tail", "Tail logs from a file, journalctl (systemd unit) or a docker container.",
      {"source": ("string", "file|journal|docker", ["file", "journal", "docker"]),
       "target": ("string", "File path / systemd unit (omit for all) / container name"),
       "lines": ("integer", "Lines (default 100)")}, required=["source"], category="system")
def logs_tail(source: str, target: Optional[str] = None, lines: int = 100) -> dict[str, Any]:
    """Read the tail of a log source."""
    if source == "docker":
        if not target:
            return result(False, error="docker source needs a container name", exit_code=2)
        from tools.docker import docker_logs
        return docker_logs(target, lines)
    if source == "journal":
        if not have("journalctl"):
            return result(False, error="journalctl not available (systemd is Linux-only). On macOS use: log show --last 10m",
                          exit_code=127)
        return run_cmd(["journalctl", "--no-pager", "-n", str(lines)] + (["-u", target] if target else []), tool="logs_tail")
    if not target:
        return result(False, error="file source needs a path", exit_code=2)
    p = resolve_path(target)
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would read last {lines} lines of {p}")
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read().decode("utf-8", "replace").splitlines()[-lines:]
    except OSError as e:
        return result(False, error=f"Cannot read {p}: {e}", exit_code=1)
    audit("logs_tail", f"tail -n {lines} {p}", 0)
    from credentials import redact
    return result(True, output=redact("\n".join(data)))


@tool("cleanup", "Free disk space: apt_autoremove, journal_vacuum, docker_prune, tmp_old (files in /tmp older than N days). "
      "Previews first; deletion needs confirmation.",
      {"action": ("string", "apt_autoremove|journal_vacuum|docker_prune|tmp_old",
                  ["apt_autoremove", "journal_vacuum", "docker_prune", "tmp_old"]),
       "days": ("integer", "Age threshold for tmp_old / journal retention (default 7)"),
       "preview_only": ("boolean", "Only show what would be removed")}, required=["action"], category="system",
      destructive=True)
def cleanup(action: str, days: int = 7, preview_only: bool = False) -> dict[str, Any]:
    """Run a cleanup action under the safety layer."""
    if action == "docker_prune":
        from tools.docker import docker_prune
        return docker_prune("system", preview_only=preview_only)
    if action == "apt_autoremove":
        if not have("apt-get"):
            return result(False, error="apt not available (Debian/Ubuntu only). macOS: `brew autoremove`", exit_code=127)
        sim = run_cmd(["apt-get", "-s", "autoremove"], tool="cleanup", quiet=True)
        if preview_only:
            return result(True, output="PREVIEW:\n" + sim["output"])
        ui.info("Would remove:\n" + "\n".join(ln for ln in sim["output"].splitlines() if ln.startswith("Remv"))[:1500])
        return run_cmd(_sudo(["apt", "autoremove", "-y"]), tool="cleanup", timeout=600)
    if action == "journal_vacuum":
        if not have("journalctl"):
            return missing_binary("systemctl")
        before = _q(["journalctl", "--disk-usage"])["output"]
        if preview_only:
            return result(True, output=f"PREVIEW: {before}\nWould run: journalctl --vacuum-time={days}d")
        res = run_cmd(_sudo(["journalctl", f"--vacuum-time={days}d"]), tool="cleanup", timeout=120)
        res["before"] = before
        return res
    if action == "tmp_old":
        listing = _q(["find", "/tmp", "-xdev", "-type", "f", "-mtime", f"+{days}", "-user", str(os.getuid())])
        files = listing["output"].splitlines()
        total = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        summary = f"{len(files)} file(s) in /tmp older than {days}d owned by you, {total / 1e6:.1f} MB"
        if preview_only or not files:
            return result(True, output=("PREVIEW: " if files else "") + summary + "\n" + "\n".join(files[:30]))
        ui.info(summary)
        return run_cmd(["find", "/tmp", "-xdev", "-type", "f", "-mtime", f"+{days}", "-user", str(os.getuid()), "-delete"],
                       tool="cleanup")
    return result(False, error=f"Unknown cleanup action '{action}'", exit_code=2)


@tool("watch_system", "Monitoring watch mode: every N seconds check disk>90%, memory>90% and downed containers; alert "
      "the user. Press Ctrl+C to stop (or set cycles).",
      {"interval": ("integer", "Seconds between checks (default 60)"), "cycles": ("integer", "Number of checks (0 = until Ctrl+C)")},
      category="system")
def watch_system(interval: int = 0, cycles: int = 0) -> dict[str, Any]:
    """Loop calling :func:`check_thresholds`."""
    interval = interval or int(config.get_config().watch_interval)
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would check disk/memory/containers every {interval}s")
    ui.info(f"Watching (every {interval}s, Ctrl+C to stop)...")
    n, seen_alerts = 0, 0
    try:
        while True:
            n += 1
            alerts = check_thresholds()
            stamp = time.strftime("%H:%M:%S")
            if alerts:
                seen_alerts += len(alerts)
                for a in alerts:
                    ui.error(f"\a[{stamp}] ALERT {a}")
            else:
                ui.good(f"[{stamp}] all clear")
            audit("watch_system", f"check #{n}: {len(alerts)} alert(s)", 0)
            if cycles and n >= cycles:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
    return result(True, output=f"Stopped after {n} check(s); {seen_alerts} alert(s) raised.")


@tool("systemd_create", "Create and install a simple systemd service (writes /etc/systemd/system via sudo).",
      {"name": ("string", "Service name"), "exec_start": ("string", "Command to run (absolute path)"),
       "workdir": ("string", "Working directory"), "user": ("string", "Run as user"),
       "description": ("string", "Description"), "enable": ("boolean", "Enable and start now")},
      required=["name", "exec_start"], category="system")
def systemd_create(name: str, exec_start: str, workdir: Optional[str] = None, user: Optional[str] = None,
                   description: Optional[str] = None, enable: bool = False) -> dict[str, Any]:
    """Write a unit file and reload systemd."""
    if not have("systemctl"):
        return missing_binary("systemctl")
    name = re.sub(r"[^A-Za-z0-9_.@-]", "-", name).removesuffix(".service")
    unit = (f"[Unit]\nDescription={description or name}\nAfter=network.target\n\n[Service]\nType=simple\n"
            f"ExecStart={exec_start}\n" + (f"WorkingDirectory={workdir}\n" if workdir else "") +
            (f"User={user}\n" if user else "") + "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n")
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would install /etc/systemd/system/{name}.service:\n{unit}")
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as fh:
        fh.write(unit)
        tmp = fh.name
    try:
        r = run_cmd(_sudo(["cp", tmp, f"/etc/systemd/system/{name}.service"]), tool="systemd_create")
        if not r["ok"]:
            return r
        r = run_cmd(_sudo(["systemctl", "daemon-reload"]), tool="systemd_create")
        if r["ok"] and enable:
            r = run_cmd(_sudo(["systemctl", "enable", "--now", name]), tool="systemd_create")
        r["unit"] = unit
        return r
    finally:
        os.unlink(tmp)


@tool("systemd_service", "Manage a systemd service: status, start, stop, restart, enable, disable.",
      {"name": ("string", "Service name"), "action": ("string", "status|start|stop|restart|enable|disable",
       ["status", "start", "stop", "restart", "enable", "disable"])}, required=["name", "action"], category="system")
def systemd_service(name: str, action: str) -> dict[str, Any]:
    """systemctl <action> <name>."""
    if not have("systemctl"):
        return missing_binary("systemctl")
    if action == "status":
        return run_cmd(["systemctl", "status", name, "--no-pager", "-n", "15"], tool="systemd_service", ok_codes=(0, 3))
    return run_cmd(_sudo(["systemctl", action, name]), tool="systemd_service")


@tool("cron_manage", "List or add a user crontab entry.",
      {"action": ("string", "list|add", ["list", "add"]), "schedule": ("string", "e.g. '*/5 * * * *'"),
       "command": ("string", "Command to schedule")}, required=["action"], category="system")
def cron_manage(action: str, schedule: Optional[str] = None, command: Optional[str] = None) -> dict[str, Any]:
    """crontab -l / append a line."""
    if not have("crontab"):
        return result(False, error="crontab not installed, install with: sudo apt install cron", exit_code=127)
    cur = run_cmd(["crontab", "-l"], tool="cron_manage", ok_codes=(0, 1), quiet=True)
    existing = cur["output"] if cur["exit_code"] == 0 else ""
    if action == "list":
        return result(True, output=existing or "(no crontab entries)")
    if not schedule or not command or len(schedule.split()) != 5:
        return result(False, error="add needs a 5-field schedule and a command", exit_code=2)
    new = existing.rstrip("\n") + ("\n" if existing else "") + f"{schedule} {command}\n"
    return run_cmd(["crontab", "-"], tool="cron_manage", input_text=new)
