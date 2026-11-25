"""Cloud tools: AWS (boto3 if importable, else the aws CLI), generic checks (public IP, ports,
ping, HTTP health), one-command deploy, and DigitalOcean/Hetzner/GCP via their CLIs.

AWS credentials are never required: without them tools return a clear message.
"""
from __future__ import annotations

import datetime as dt
import calendar
import json
import socket
import time
from typing import Any, Optional

import requests

import ui
from tools._core import STATE, audit, confirm, have, missing_binary, resolve_path, result, run_cmd
from tools.registry import tool

_REGION = {"region": ("string", "AWS region (default: from your AWS config)")}
_SENSITIVE = {22: "SSH", 3389: "RDP", 3306: "MySQL", 5432: "Postgres", 6379: "Redis", 27017: "MongoDB",
              9200: "Elasticsearch", 5900: "VNC", 23: "Telnet", 1433: "MSSQL"}


def _aws(service: str, boto_method: str, cli_op: str, boto_kwargs: Optional[dict[str, Any]] = None,
         cli_args: Optional[list[str]] = None, region: Optional[str] = None, mutating: bool = False,
         tool_name: str = "aws") -> dict[str, Any]:
    """Call AWS via boto3 if available, else the aws CLI. Both return the same JSON shape."""
    desc = f"aws {service} {cli_op}" + (f" --region {region}" if region else "")
    if STATE.dry_run and mutating:
        ui.dim(f"[dry-run] would run: {desc} {' '.join(cli_args or [])}")
        audit(tool_name, desc, "dry-run")
        return result(True, output=f"[dry-run] would run: {desc} {' '.join(cli_args or [])}", dry_run=True)
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError  # type: ignore
    except ImportError:
        boto3 = None
    if boto3 is not None:
        try:
            sess = boto3.Session(region_name=region)
            if sess.get_credentials() is None:
                return result(False, error="No AWS credentials found. Run `aws configure`, or set AWS_ACCESS_KEY_ID / "
                              "AWS_SECRET_ACCESS_KEY (or AWS_PROFILE) in .env or your environment.", exit_code=1)
            data = getattr(sess.client(service), boto_method)(**(boto_kwargs or {}))
            data.pop("ResponseMetadata", None)
            audit(tool_name, f"boto3 {service}.{boto_method}", 0)
            return result(True, data=json.loads(json.dumps(data, default=str)))
        except NoCredentialsError:
            return result(False, error="No AWS credentials found. Run `aws configure` or set AWS_* env vars.", exit_code=1)
        except (ClientError, BotoCoreError) as e:
            audit(tool_name, f"boto3 {service}.{boto_method}", 1)
            return result(False, error=f"AWS error: {e}", exit_code=1)
    if not have("aws"):
        return result(False, error="Neither boto3 nor the aws CLI is installed. Install one: pip install boto3  |  "
                      "https://aws.amazon.com/cli/", exit_code=127, missing="aws")
    cmd = ["aws", service, cli_op, *(cli_args or []), "--output", "json"] + (["--region", region] if region else [])
    res = run_cmd(cmd, tool=tool_name, timeout=120, confirmed=True)
    if res["ok"]:
        try:
            res["data"] = json.loads(res["output"] or "{}")
        except ValueError:
            pass
    elif "Unable to locate credentials" in res["error"]:
        res["error"] = "No AWS credentials found. Run `aws configure` or set AWS_* env vars.\n" + res["error"]
    return res


def _fail(res: dict[str, Any]) -> bool:
    return not res["ok"] or "data" not in res


# ------------------------------------------------------------------ AWS
@tool("aws_ec2_list", "List EC2 instances (id, name, state, type, IPs).",
      {**_REGION, "state": ("string", "Filter by state e.g. running|stopped")}, category="cloud")
def aws_ec2_list(region: Optional[str] = None, state: Optional[str] = None) -> dict[str, Any]:
    """aws ec2 describe-instances, flattened."""
    kw = {"Filters": [{"Name": "instance-state-name", "Values": [state]}]} if state else {}
    cli = ["--filters", f"Name=instance-state-name,Values={state}"] if state else []
    res = _aws("ec2", "describe_instances", "describe-instances", kw, cli, region, tool_name="aws_ec2_list")
    if _fail(res):
        return res
    rows = []
    for r in res["data"].get("Reservations", []):
        for i in r.get("Instances", []):
            name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "-")
            rows.append(f"{i['InstanceId']:<20} {name[:24]:<24} {i['State']['Name']:<10} {i.get('InstanceType', ''):<12} "
                        f"pub={i.get('PublicIpAddress', '-'):<15} priv={i.get('PrivateIpAddress', '-')}")
    return result(True, output="\n".join(rows) or "No instances found.", count=len(rows))


@tool("aws_ec2_power", "Start, stop or reboot an EC2 instance (stop needs confirmation).",
      {**_REGION, "instance_id": ("string", "Instance id (i-...)"),
       "action": ("string", "start|stop|reboot", ["start", "stop", "reboot"])},
      required=["instance_id", "action"], category="cloud")
def aws_ec2_power(instance_id: str, action: str, region: Optional[str] = None) -> dict[str, Any]:
    """start/stop/reboot-instances."""
    if action == "stop" and not STATE.dry_run and not confirm(f"Stop EC2 instance {instance_id}?"):
        return result(False, error="Cancelled by user.", exit_code=-2, declined=True)
    method = {"start": "start_instances", "stop": "stop_instances", "reboot": "reboot_instances"}[action]
    op = {"start": "start-instances", "stop": "stop-instances", "reboot": "reboot-instances"}[action]
    res = _aws("ec2", method, op, {"InstanceIds": [instance_id]}, ["--instance-ids", instance_id], region,
               mutating=True, tool_name=f"aws_ec2_{action}")
    if res["ok"] and "data" in res:
        res["output"] = json.dumps(res["data"], indent=2)[:2000]
    return res


@tool("aws_s3_ls", "List S3 buckets, or objects in a bucket.",
      {"bucket": ("string", "Bucket (omit to list buckets)"), "prefix": ("string", "Key prefix")}, category="cloud")
def aws_s3_ls(bucket: Optional[str] = None, prefix: Optional[str] = None) -> dict[str, Any]:
    """s3 list-buckets / list-objects-v2."""
    if not bucket:
        res = _aws("s3", "list_buckets", "list-buckets", tool_name="aws_s3_ls")
        if _fail(res):
            return res
        return result(True, output="\n".join(f"{b['CreationDate'][:10]}  {b['Name']}" for b in res["data"].get("Buckets", []))
                      or "No buckets.")
    kw: dict[str, Any] = {"Bucket": bucket, "MaxKeys": 200, **({"Prefix": prefix} if prefix else {})}
    cli = ["--bucket", bucket, "--max-items", "200"] + (["--prefix", prefix] if prefix else [])
    res = _aws("s3", "list_objects_v2", "list-objects-v2", kw, cli, tool_name="aws_s3_ls")
    if _fail(res):
        return res
    objs = res["data"].get("Contents", []) or []
    return result(True, output="\n".join(f"{o['LastModified'][:19]}  {o['Size']:>12}  {o['Key']}" for o in objs)
                  or "Bucket is empty (or prefix matched nothing).")


@tool("aws_cost", "Month-to-date AWS spend by service with a simple end-of-month projection "
      "(Cost Explorer; AWS charges ~$0.01 per API request).", {}, category="cloud")
def aws_cost() -> dict[str, Any]:
    """Cost Explorer get_cost_and_usage for the current month."""
    today = dt.date.today()
    start = today.replace(day=1)
    end = today + dt.timedelta(days=1)
    if end.month != start.month:  # first/last day edge cases: CE needs Start < End
        end = start + dt.timedelta(days=32)
        end = end.replace(day=1)
    period = {"Start": start.isoformat(), "End": end.isoformat()}
    kw = {"TimePeriod": period, "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
          "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
    cli = ["--time-period", f"Start={period['Start']},End={period['End']}", "--granularity", "MONTHLY",
           "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE"]
    res = _aws("ce", "get_cost_and_usage", "get-cost-and-usage", kw, cli, "us-east-1", tool_name="aws_cost")
    if _fail(res):
        return res
    items: list[tuple[str, float]] = []
    unit = "USD"
    for t in res["data"].get("ResultsByTime", []):
        for g in t.get("Groups", []):
            m = g["Metrics"]["UnblendedCost"]
            unit = m.get("Unit", unit)
            items.append((g["Keys"][0], float(m["Amount"])))
    total = sum(a for _, a in items)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    proj = total / max(today.day, 1) * days_in_month
    lines = [f"{a:>10.2f} {unit}  {k}" for k, a in sorted(items, key=lambda x: -x[1]) if a >= 0.005][:15]
    lines.append(f"\nMonth-to-date: {total:.2f} {unit}")
    lines.append(f"Linear projection for the month: ~{proj:.2f} {unit} (naive estimate, not a forecast)")
    return result(True, output="\n".join(lines))


@tool("aws_security_groups", "Audit security groups and flag world-open (0.0.0.0/0) ingress, especially risky ports.",
      {**_REGION, "group_id": ("string", "Only this group (sg-...)")}, category="cloud")
def aws_security_groups(region: Optional[str] = None, group_id: Optional[str] = None) -> dict[str, Any]:
    """describe-security-groups with risk flags."""
    kw = {"GroupIds": [group_id]} if group_id else {}
    cli = ["--group-ids", group_id] if group_id else []
    res = _aws("ec2", "describe_security_groups", "describe-security-groups", kw, cli, region, tool_name="aws_security_groups")
    if _fail(res):
        return res
    out, risky = [], 0
    for g in res["data"].get("SecurityGroups", []):
        out.append(f"{g['GroupId']}  {g['GroupName']}  (vpc {g.get('VpcId', '-')})")
        for p in g.get("IpPermissions", []):
            lo, hi = p.get("FromPort", 0), p.get("ToPort", 65535)
            cidrs = [r["CidrIp"] for r in p.get("IpRanges", [])] + [r["CidrIpv6"] for r in p.get("Ipv6Ranges", [])]
            world = any(c in ("0.0.0.0/0", "::/0") for c in cidrs)
            ports = "all" if p.get("IpProtocol") == "-1" else (f"{lo}" if lo == hi else f"{lo}-{hi}")
            flag = ""
            if world:
                hits = [f"{n}({q})" for q, n in _SENSITIVE.items() if p.get("IpProtocol") == "-1" or lo <= q <= hi]
                flag = "  !! OPEN TO WORLD" + (f" incl. {', '.join(hits)}" if hits else "")
                risky += 1
            out.append(f"   in  {p.get('IpProtocol')}/{ports} from {', '.join(cidrs) or 'sg-ref'}{flag}")
    out.append(f"\n{risky} world-open ingress rule(s).")
    return result(True, output="\n".join(out), risky=risky)


@tool("aws_ebs", "List EBS volumes; flags unattached (billing for nothing) and unencrypted volumes.", _REGION, category="cloud")
def aws_ebs(region: Optional[str] = None) -> dict[str, Any]:
    """describe-volumes with findings."""
    res = _aws("ec2", "describe_volumes", "describe-volumes", {}, [], region, tool_name="aws_ebs")
    if _fail(res):
        return res
    lines, unattached, unenc = [], 0, 0
    for v in res["data"].get("Volumes", []):
        att = ",".join(a["InstanceId"] for a in v.get("Attachments", [])) or "-"
        flags = []
        if v["State"] == "available":
            flags.append("UNATTACHED"); unattached += 1
        if not v.get("Encrypted"):
            flags.append("unencrypted"); unenc += 1
        lines.append(f"{v['VolumeId']}  {v['Size']:>5} GiB  {v['VolumeType']:<5} {v['State']:<10} att={att:<20} {' '.join(flags)}")
    lines.append(f"\n{len(lines)} volume(s); {unattached} unattached, {unenc} unencrypted.")
    return result(True, output="\n".join(lines))


@tool("aws_logs_tail", "Fetch recent CloudWatch log events from a log group.",
      {**_REGION, "log_group": ("string", "Log group name"), "minutes": ("integer", "Look back N minutes (default 15)"),
       "filter": ("string", "CloudWatch filter pattern")}, required=["log_group"], category="cloud")
def aws_logs_tail(log_group: str, minutes: int = 15, filter: Optional[str] = None,
                  region: Optional[str] = None) -> dict[str, Any]:
    """filter-log-events for the last N minutes."""
    start_ms = int((time.time() - minutes * 60) * 1000)
    kw: dict[str, Any] = {"logGroupName": log_group, "startTime": start_ms, "limit": 200, **({"filterPattern": filter} if filter else {})}
    cli = ["--log-group-name", log_group, "--start-time", str(start_ms), "--max-items", "200"] + (["--filter-pattern", filter] if filter else [])
    res = _aws("logs", "filter_log_events", "filter-log-events", kw, cli, region, tool_name="aws_logs_tail")
    if _fail(res):
        return res
    ev = res["data"].get("events", [])
    lines = [f"{dt.datetime.fromtimestamp(e['timestamp'] / 1000):%H:%M:%S} {e['message'].rstrip()}" for e in ev[-200:]]
    return result(True, output="\n".join(lines) or f"No events in the last {minutes} min.")


# ------------------------------------------------------------------ generic
@tool("public_ip", "Show this machine's public IP address.", {}, category="cloud")
def public_ip() -> dict[str, Any]:
    """Query public IP echo services."""
    if STATE.dry_run:
        return result(True, output="[dry-run] would GET https://api.ipify.org")
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://ifconfig.me/ip"):
        try:
            r = requests.get(url, timeout=5)
            if r.ok and r.text.strip():
                audit("public_ip", f"GET {url}", r.status_code)
                return result(True, output=r.text.strip())
        except requests.RequestException:
            continue
    return result(False, error="Could not determine public IP (network blocked or offline).", exit_code=1)


@tool("port_check", "Check whether TCP ports are open on a host.",
      {"host": ("string", "Hostname or IP"), "ports": ("array", "Ports, e.g. [22,80,443] (default 22,80,443)"),
       "timeout": ("number", "Per-port timeout seconds (default 2)")}, required=["host"], category="cloud")
def port_check(host: str, ports: Optional[list[str]] = None, timeout: float = 2.0) -> dict[str, Any]:
    """TCP connect test for each port."""
    plist = [int(p) for p in (ports or ["22", "80", "443"])]
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would TCP-connect to {host} ports {plist}")
    lines, open_n = [], 0
    for p in plist:
        t0 = time.time()
        try:
            with socket.create_connection((host, p), timeout=timeout):
                lines.append(f"{host}:{p}  OPEN  ({(time.time() - t0) * 1000:.0f} ms)"); open_n += 1
        except socket.gaierror:
            return result(False, error=f"Cannot resolve host '{host}'", exit_code=1)
        except OSError as e:
            lines.append(f"{host}:{p}  closed/filtered ({e.__class__.__name__})")
    audit("port_check", f"tcp {host} {plist}", 0)
    return result(True, output="\n".join(lines), open_ports=open_n)


@tool("ping_host", "Ping a host.", {"host": ("string", "Host"), "count": ("integer", "Packets (default 3)")},
      required=["host"], category="cloud")
def ping_host(host: str, count: int = 3) -> dict[str, Any]:
    """ping -c N host."""
    if not have("ping"):
        return missing_binary("ping")
    return run_cmd(["ping", "-c", str(count), host], tool="ping_host", timeout=count * 4 + 5)


@tool("http_check", "HTTP health check: status code, latency, server header.",
      {"url": ("string", "URL"), "expect_status": ("integer", "Expected status (default: any 2xx/3xx)"),
       "timeout": ("number", "Seconds (default 8)")}, required=["url"], category="cloud")
def http_check(url: str, expect_status: Optional[int] = None, timeout: float = 8.0) -> dict[str, Any]:
    """GET the URL and judge health."""
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would GET {url}")
    t0 = time.time()
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=False)
    except requests.RequestException as e:
        audit("http_check", f"GET {url}", "error")
        return result(False, error=f"{url} unreachable: {type(e).__name__}: {e}", exit_code=1)
    ms = (time.time() - t0) * 1000
    healthy = (r.status_code == expect_status) if expect_status else r.status_code < 400
    audit("http_check", f"GET {url}", r.status_code)
    return result(healthy, output=f"{url} -> HTTP {r.status_code} in {ms:.0f} ms  server={r.headers.get('server', '-')}",
                  error="" if healthy else f"Unexpected status {r.status_code}", status=r.status_code)


@tool("deploy", "Deploy: git pull + docker compose up -d --build + verify (compose ps, optional HTTP health check).",
      {"path": ("string", "Project dir (default cwd)"), "health_url": ("string", "URL to verify after deploy"),
       "build": ("boolean", "Rebuild images (default true)"), "skip_pull": ("boolean", "Skip git pull")}, category="cloud")
def deploy(path: Optional[str] = None, health_url: Optional[str] = None, build: bool = True,
           skip_pull: bool = False) -> dict[str, Any]:
    """Run the deploy pipeline step by step; stops at the first failure."""
    from tools.docker import docker_compose
    from tools.git import git_pull
    d = str(resolve_path(path))
    log: list[str] = []
    ok = True

    def step(name: str, res: dict[str, Any]) -> bool:
        log.append(f"[{'OK' if res['ok'] else 'FAIL'}] {name}\n{(res['output'] or res['error'])[-600:]}")
        return bool(res["ok"])

    if not skip_pull and not step("git pull", git_pull(repo=d)):
        ok = False
    if ok and not step("docker compose up -d", docker_compose("up", path=d, build=build)):
        ok = False
    if ok and not step("docker compose ps", docker_compose("ps", path=d)):
        ok = False
    if ok and health_url and not STATE.dry_run:
        last = result(False, error="no attempt")
        for _ in range(6):
            last = http_check(health_url, timeout=5)
            if last["ok"]:
                break
            time.sleep(2)
        ok = step(f"verify {health_url}", last)
    try:
        from memory import Memory
        if not STATE.dry_run:
            Memory().record_deploy(d, ok)
    except Exception:
        pass
    return result(ok, output="\n\n".join(log), error="" if ok else "Deploy failed; see failing step above.")


_CLI: dict[str, dict[str, list[str]]] = {
    "digitalocean": {"servers": ["doctl", "compute", "droplet", "list"], "account": ["doctl", "account", "get"],
                     "volumes": ["doctl", "compute", "volume", "list"]},
    "hetzner": {"servers": ["hcloud", "server", "list"], "volumes": ["hcloud", "volume", "list"],
                "firewalls": ["hcloud", "firewall", "list"]},
    "gcp": {"servers": ["gcloud", "compute", "instances", "list"], "disks": ["gcloud", "compute", "disks", "list"],
            "firewalls": ["gcloud", "compute", "firewall-rules", "list"]},
}


@tool("cloud_cli", "Inspect DigitalOcean / Hetzner / GCP resources via their CLIs (doctl, hcloud, gcloud).",
      {"provider": ("string", "digitalocean|hetzner|gcp", list(_CLI)),
       "resource": ("string", "servers|volumes|disks|firewalls|account (default servers)")}, required=["provider"],
      category="cloud")
def cloud_cli(provider: str, resource: str = "servers") -> dict[str, Any]:
    """Run a read-only listing command on a provider CLI; degrades gracefully if missing."""
    cmds = _CLI.get(provider)
    if not cmds:
        return result(False, error=f"Unknown provider '{provider}'. Supported: {', '.join(_CLI)}", exit_code=2)
    cmd = cmds.get(resource)
    if not cmd:
        return result(False, error=f"{provider} supports resources: {', '.join(cmds)}", exit_code=2)
    if not have(cmd[0]):
        return missing_binary(cmd[0])
    return run_cmd(cmd, tool=f"cloud_{provider}", timeout=60)
