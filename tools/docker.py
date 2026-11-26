"""Docker tools: ps/images/logs/stats/inspect, container lifecycle, exec/build/run, compose,
prune (with preview + confirm), volume/network, health check, auto-restart of crashed containers."""
from __future__ import annotations

import json
import re
import shlex
import time
from typing import Any, Optional

from tools._core import STATE, have, missing_binary, resolve_path, result, run_cmd
from tools.registry import tool


def _docker(args: list[str], tool_name: str, **kw: Any) -> dict[str, Any]:
    if not have("docker"):
        return missing_binary("docker")
    res = run_cmd(["docker", *args], tool=tool_name, **kw)
    err = res.get("error", "")
    if not res["ok"] and re.search(r"cannot connect to the docker daemon|is the docker daemon running", err, re.I):
        res["hint"] = ("Docker daemon not reachable. Start it: Linux `sudo systemctl start docker`; "
                       "macOS/Windows: open Docker Desktop. You may also need to join the docker group.")
    return res


@tool("docker_ps", "List containers.", {"all": ("boolean", "Include stopped containers")}, category="docker")
def docker_ps(all: bool = False) -> dict[str, Any]:
    """docker ps [-a]."""
    return _docker(["ps"] + (["-a"] if all else []) + ["--format", "table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"], "docker_ps")


@tool("docker_images", "List images.", {}, category="docker")
def docker_images() -> dict[str, Any]:
    """docker images."""
    return _docker(["images"], "docker_images")


@tool("docker_logs", "Show the last N log lines of a container.",
      {"container": ("string", "Container name or id"), "tail": ("integer", "Lines (default 100)")},
      required=["container"], category="docker")
def docker_logs(container: str, tail: int = 100) -> dict[str, Any]:
    """docker logs --tail N."""
    return _docker(["logs", "--tail", str(tail), container], "docker_logs")


@tool("docker_stats", "One-shot CPU/memory stats for running containers.", {}, category="docker")
def docker_stats() -> dict[str, Any]:
    """docker stats --no-stream."""
    return _docker(["stats", "--no-stream"], "docker_stats")


@tool("docker_inspect", "Inspect a container/image (JSON).", {"target": ("string", "Name or id"),
      "format": ("string", "Optional Go template, e.g. '{{.State.Status}}'")}, required=["target"], category="docker")
def docker_inspect(target: str, format: Optional[str] = None) -> dict[str, Any]:
    """docker inspect [--format]."""
    return _docker(["inspect"] + (["--format", format] if format else []) + [target], "docker_inspect")


@tool("docker_container", "Start, stop, restart or remove (rm needs confirmation) a container.",
      {"action": ("string", "start|stop|restart|rm", ["start", "stop", "restart", "rm"]),
       "container": ("string", "Container name or id")}, required=["action", "container"], category="docker",
      destructive=True)
def docker_container(action: str, container: str) -> dict[str, Any]:
    """docker start|stop|restart|rm."""
    return _docker([action, container], f"docker_{action}")


@tool("docker_exec", "Run a command inside a running container.",
      {"container": ("string", "Container"), "command": ("string", "Shell command to run")},
      required=["container", "command"], category="docker")
def docker_exec(container: str, command: str) -> dict[str, Any]:
    """docker exec container sh -c command."""
    return _docker(["exec", container, "sh", "-c", command], "docker_exec", timeout=120)


@tool("docker_build", "Build an image.",
      {"path": ("string", "Build context dir (default .)"), "tag": ("string", "Image tag"),
       "file": ("string", "Dockerfile path")}, category="docker")
def docker_build(path: Optional[str] = None, tag: Optional[str] = None, file: Optional[str] = None) -> dict[str, Any]:
    """docker build."""
    return _docker(["build"] + (["-t", tag] if tag else []) + (["-f", file] if file else []) +
                   [str(resolve_path(path))], "docker_build", timeout=1800)


@tool("docker_run", "Run a container from an image.",
      {"image": ("string", "Image"), "name": ("string", "Container name"), "ports": ("array", "e.g. ['8080:80']"),
       "env": ("array", "e.g. ['KEY=value']"), "volumes": ("array", "e.g. ['./data:/data']"),
       "detach": ("boolean", "Run in background (default true)"), "command": ("string", "Command override")},
      required=["image"], category="docker")
def docker_run(image: str, name: Optional[str] = None, ports: Optional[list[str]] = None,
               env: Optional[list[str]] = None, volumes: Optional[list[str]] = None, detach: bool = True,
               command: Optional[str] = None) -> dict[str, Any]:
    """docker run."""
    args = ["run"] + (["-d"] if detach else ["--rm"]) + (["--name", name] if name else [])
    for flag, vals in (("-p", ports), ("-e", env), ("-v", volumes)):
        for v in vals or []:
            args += [flag, v]
    args.append(image)
    if command:
        args += shlex.split(command)
    return _docker(args, "docker_run", timeout=300)


def _compose_base() -> Optional[list[str]]:
    if have("docker"):
        r = run_cmd(["docker", "compose", "version"], tool="docker_compose", quiet=True) if not STATE.dry_run else None
        if STATE.dry_run or (r and r["ok"]):
            return ["docker", "compose"]
    if have("docker-compose"):
        return ["docker-compose"]
    return None


@tool("docker_compose", "Docker Compose: up, down, ps, logs, build, pull.",
      {"action": ("string", "up|down|ps|logs|build|pull", ["up", "down", "ps", "logs", "build", "pull"]),
       "path": ("string", "Project dir (default cwd)"), "file": ("string", "Compose file"),
       "services": ("array", "Limit to services"), "tail": ("integer", "Log lines (logs)"),
       "build": ("boolean", "Build before up"), "volumes": ("boolean", "down: also remove volumes (destructive)")},
      required=["action"], category="docker", destructive=True)
def docker_compose(action: str, path: Optional[str] = None, file: Optional[str] = None,
                   services: Optional[list[str]] = None, tail: int = 100, build: bool = False,
                   volumes: bool = False) -> dict[str, Any]:
    """docker compose <action>."""
    if not have("docker") and not have("docker-compose"):
        return missing_binary("docker")
    base = _compose_base()
    if base is None:
        return result(False, error="docker compose plugin not installed, install with: "
                      "sudo apt install docker-compose-plugin  |  Docker Desktop includes it", exit_code=127)
    args = base + (["-f", file] if file else [])
    if action == "up":
        args += ["up", "-d"] + (["--build"] if build else [])
    elif action == "down":
        args += ["down"] + (["-v"] if volumes else [])
    elif action == "logs":
        args += ["logs", "--no-color", "--tail", str(tail)]
    else:
        args += [action]
    args += services or []
    if action == "down" and volumes:
        from tools._core import confirm
        if not STATE.dry_run and not confirm("DESTRUCTIVE: 'compose down -v' deletes named volumes (data loss). Proceed?"):
            return result(False, error="Cancelled: volume removal not confirmed.", exit_code=-2, declined=True)
    return run_cmd(args, tool=f"docker_compose_{action}", cwd=str(resolve_path(path)), timeout=1800,
                   confirmed=(action == "down" and volumes))


@tool("docker_prune", "Preview then prune unused Docker data (system|volume|network|image|container). "
      "Always previews; deletion needs confirmation.",
      {"target": ("string", "system|volume|network|image|container", ["system", "volume", "network", "image", "container"]),
       "all_unused": ("boolean", "Also remove unused (not just dangling) images (-a)"),
       "preview_only": ("boolean", "Only show what would be removed")}, category="docker", destructive=True)
def docker_prune(target: str = "system", all_unused: bool = False, preview_only: bool = False) -> dict[str, Any]:
    """Dry-run preview, then ``docker <target> prune -f`` after confirmation."""
    previews = {
        "system": [["system", "df"], ["ps", "-a", "--filter", "status=exited"], ["images", "-f", "dangling=true"]],
        "volume": [["volume", "ls", "-f", "dangling=true"]],
        "network": [["network", "ls"]],
        "image": [["images", "-f", "dangling=true"]] + ([["images"]] if all_unused else []),
        "container": [["ps", "-a", "--filter", "status=exited"]],
    }
    if target not in previews:
        return result(False, error=f"Unknown prune target '{target}'", exit_code=2)
    if not have("docker"):
        return missing_binary("docker")
    parts = []
    for p in previews[target]:
        r = _docker(p, "docker_prune_preview", quiet=True)
        if not r["ok"] and not STATE.dry_run:
            return r
        parts.append(f"$ docker {' '.join(p)}\n{r['output']}")
    preview = "\n\n".join(parts)
    if preview_only:
        return result(True, output="PREVIEW (nothing removed):\n" + preview)
    from tools._core import ui
    ui.info("What would be affected:\n" + preview)
    args = [target, "prune", "-f"] + (["-a"] if all_unused and target in ("system", "image") else [])
    res = _docker(args, "docker_prune")
    res["preview"] = preview
    return res


@tool("docker_volume_network", "List or prune docker volumes/networks.",
      {"kind": ("string", "volume|network", ["volume", "network"]),
       "action": ("string", "ls|prune", ["ls", "prune"])}, required=["kind"], category="docker", destructive=True)
def docker_volume_network(kind: str, action: str = "ls") -> dict[str, Any]:
    """docker volume|network ls|prune."""
    if action == "prune":
        return docker_prune(target=kind)
    return _docker([kind, "ls"], f"docker_{kind}_ls")


def _scan() -> dict[str, Any]:
    """Classify all containers: running/unhealthy/crashed/restarting/stopped_ok."""
    r = _docker(["ps", "-a", "--format", "{{json .}}"], "docker_health", quiet=True)
    if not r["ok"]:
        return {"error": r}
    groups: dict[str, list[dict[str, str]]] = {k: [] for k in ("running", "unhealthy", "crashed", "restarting", "stopped_ok")}
    for line in r["output"].splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        status, state = c.get("Status", ""), c.get("State", "")
        item = {"name": c.get("Names", ""), "image": c.get("Image", ""), "status": status}
        if state == "running" and "unhealthy" in status:
            groups["unhealthy"].append(item)
        elif state == "running":
            groups["running"].append(item)
        elif state == "restarting":
            groups["restarting"].append(item)
        elif state == "exited":
            m = re.search(r"Exited \((\d+)\)", status)
            (groups["stopped_ok"] if m and m.group(1) == "0" else groups["crashed"]).append(item)
    return groups


def containers_down() -> list[str]:
    """Names of crashed/unhealthy/restarting containers (empty if docker unavailable)."""
    if not have("docker") or STATE.dry_run:
        return []
    g = _scan()
    if "error" in g:
        return []
    return [c["name"] for k in ("crashed", "unhealthy", "restarting") for c in g[k]]


@tool("docker_health", "Health check of ALL containers: running, unhealthy, crashed (non-zero exit), restarting.",
      {}, category="docker")
def docker_health() -> dict[str, Any]:
    """Summarise container health."""
    if not have("docker"):
        return missing_binary("docker")
    if STATE.dry_run:
        return run_cmd(["docker", "ps", "-a"], tool="docker_health")
    g = _scan()
    if "error" in g:
        return g["error"]
    lines = []
    for k in ("running", "unhealthy", "crashed", "restarting", "stopped_ok"):
        lines.append(f"{k.upper()} ({len(g[k])})")
        lines += [f"  - {c['name']}  [{c['image']}]  {c['status']}" for c in g[k]]
    bad = len(g["unhealthy"]) + len(g["crashed"]) + len(g["restarting"])
    lines.append("\nAll containers healthy." if not bad else f"\n{bad} container(s) need attention. Say 'fix it' to restart them.")
    return result(True, output="\n".join(lines), problems=bad)


@tool("docker_fix", "Restart crashed/unhealthy containers (shows last logs first, then verifies). "
      "Use when the user says 'fix it'.", {"container": ("string", "Only this container (default: all problem ones)")},
      category="docker")
def docker_fix(container: Optional[str] = None) -> dict[str, Any]:
    """Restart problem containers and verify they come back."""
    if not have("docker"):
        return missing_binary("docker")
    if STATE.dry_run:
        return run_cmd(["docker", "restart", container or "<each crashed/unhealthy container>"], tool="docker_fix")
    targets = [container] if container else containers_down()
    if not targets:
        return result(True, output="No crashed or unhealthy containers found.")
    report, all_ok = [], True
    for name in targets:
        logs = _docker(["logs", "--tail", "15", name], "docker_fix", quiet=True)
        rs = _docker(["restart", name], "docker_fix")
        time.sleep(3)
        st = _docker(["inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", name],
                     "docker_fix", quiet=True)
        ok = rs["ok"] and st["output"].strip().startswith("running") and "unhealthy" not in st["output"]
        all_ok &= ok
        report.append(f"== {name}: {'RECOVERED' if ok else 'STILL FAILING'} (state: {st['output'].strip() or st['error']})\n"
                      f"last logs before restart:\n{logs['output'][-1200:]}")
    return result(all_ok, output="\n\n".join(report), error="" if all_ok else "Some containers are still failing; see logs above.")
