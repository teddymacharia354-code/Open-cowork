"""Git tools: status/diff/log/branch/checkout/pull/fetch/add/commit/push/clone/stash/reset,
repo init, .gitignore, GitHub repo creation, and secure authentication."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import credentials
import ui
from tools._core import STATE, clip, confirm, have, missing_binary, resolve_path, result, run_cmd
from tools.registry import tool

_REPO = {"repo": ("string", "Repository directory (default: current working dir)")}


def _dir(repo: Optional[str]) -> str:
    return str(resolve_path(repo))


def _git(args: list[str], repo: Optional[str], tool_name: str, **kw: Any) -> dict[str, Any]:
    if not have("git"):
        return missing_binary("git")
    return run_cmd(["git", *args], tool=tool_name, cwd=_dir(repo), **kw)


def _remote_url(repo: Optional[str], remote: str) -> str:
    if re.match(r"^(https?|ssh|git)://|^[\w.-]+@[\w.-]+:", remote):
        return remote
    r = run_cmd(["git", "remote", "get-url", remote], tool="git_remote", cwd=_dir(repo), quiet=True)
    return r["output"].strip() if r["ok"] else ""


_AUTH_FAIL = re.compile(r"authentication failed|could not read (?:username|password)|terminal prompts disabled|"
                        r"invalid username or password|http 40[13]|returned error: 40[13]|"
                        r"permission denied \(publickey\)|repository not found", re.I)


def _git_network(args: list[str], repo: Optional[str], tool_name: str, remote: str, operation: str,
                 require_auth: bool = False, confirmed: bool = False) -> dict[str, Any]:
    """Run a networked git command with the secure credential flow."""
    if not have("git"):
        return missing_binary("git")
    base_env = {"GIT_TERMINAL_PROMPT": "0"}
    url = _remote_url(repo, remote)
    run = lambda extra_args, env: run_cmd(["git", *extra_args, *args], tool=tool_name, cwd=_dir(repo),
                                          env=env, timeout=300, confirmed=confirmed)
    if STATE.dry_run or not url or credentials.is_ssh_url(url) or not url.startswith("http"):
        res = run([], base_env)
        if not res["ok"] and credentials.is_ssh_url(url):
            keys = credentials.detect_ssh_keys()
            res["hint"] = ("SSH remote: ensure your key is loaded (ssh-add) and added to the host. "
                           f"Keys found in ~/.ssh: {', '.join(keys) or 'none'}")
        return res

    creds = credentials.lookup(url)
    if creds is None and not require_auth:
        res = run([], base_env)  # public repos need no auth
        if res["ok"] or not _AUTH_FAIL.search(res["error"] + res["output"]):
            return res
    if creds is None:
        creds = credentials.prompt_credentials(url, operation)
        if creds is None:
            return result(False, error=("Authentication required but no credentials found and no interactive "
                                        "terminal. Set GIT_USERNAME/GIT_TOKEN in .env, store them in the "
                                        "keyring, or use an SSH remote."), exit_code=128)
    with credentials.askpass_env(creds) as env:
        res = run(["-c", "credential.helper="], {**base_env, **env})
    if res["ok"]:
        credentials.offer_save(url, creds)
    elif _AUTH_FAIL.search(res["error"] + res["output"]):
        res["hint"] = f"Authentication failed using credentials from: {creds.source}. Check token scope/expiry."
    return res


# ------------------------------------------------------------------ read-only
@tool("git_status", "Show git status (branch, staged/unstaged/untracked files).", _REPO, category="git")
def git_status(repo: Optional[str] = None) -> dict[str, Any]:
    """git status -sb plus remote tracking info."""
    return _git(["status", "-sb"], repo, "git_status")


@tool("git_diff", "Show git diff (unstaged by default, or staged).",
      {**_REPO, "staged": ("boolean", "Show staged changes"), "file": ("string", "Limit to one file"),
       "stat": ("boolean", "Only show diffstat")}, category="git")
def git_diff(repo: Optional[str] = None, staged: bool = False, file: Optional[str] = None,
             stat: bool = False) -> dict[str, Any]:
    """git diff with optional --staged/--stat/file."""
    args = ["--no-pager", "diff"] + (["--staged"] if staged else []) + (["--stat"] if stat else [])
    if file:
        args += ["--", file]
    return _git(args, repo, "git_diff")


@tool("git_log", "Show recent commits.", {**_REPO, "n": ("integer", "Number of commits (default 10)")}, category="git")
def git_log(repo: Optional[str] = None, n: int = 10) -> dict[str, Any]:
    """git log -n with a compact format."""
    return _git(["--no-pager", "log", f"-n{max(1, min(n, 200))}", "--date=short",
                 "--pretty=format:%h %ad %<(16,trunc)%an %s", "--decorate"], repo, "git_log")


@tool("git_branch", "List branches, or create/delete one.",
      {**_REPO, "name": ("string", "Branch to create/delete (omit to list)"),
       "delete": ("boolean", "Delete the branch (safe -d)")}, category="git")
def git_branch(repo: Optional[str] = None, name: Optional[str] = None, delete: bool = False) -> dict[str, Any]:
    """List/create/delete branches."""
    if not name:
        return _git(["branch", "-a", "-vv"], repo, "git_branch")
    return _git(["branch", "-d", name] if delete else ["branch", name], repo, "git_branch")


# ------------------------------------------------------------------ mutating
@tool("git_checkout", "Switch branch (optionally creating it).",
      {**_REPO, "target": ("string", "Branch, tag or commit"), "create": ("boolean", "Create new branch (-b)")},
      required=["target"], category="git")
def git_checkout(target: str, repo: Optional[str] = None, create: bool = False) -> dict[str, Any]:
    """git checkout [-b] target."""
    return _git(["checkout"] + (["-b"] if create else []) + [target], repo, "git_checkout")


@tool("git_fetch", "Fetch from remotes.", {**_REPO, "remote": ("string", "Remote (default: all)")}, category="git")
def git_fetch(repo: Optional[str] = None, remote: Optional[str] = None) -> dict[str, Any]:
    """git fetch with secure auth."""
    return _git_network(["fetch", remote] if remote else ["fetch", "--all", "--prune"], repo, "git_fetch",
                        remote or "origin", "Fetch")


@tool("git_pull", "Pull (fetch + merge) the current branch.",
      {**_REPO, "remote": ("string", "Remote (default origin)"), "branch": ("string", "Branch")}, category="git")
def git_pull(repo: Optional[str] = None, remote: str = "origin", branch: Optional[str] = None) -> dict[str, Any]:
    """git pull with secure auth."""
    return _git_network(["pull", remote] + ([branch] if branch else []), repo, "git_pull", remote, "Pull")


@tool("git_add", "Stage files.", {**_REPO, "paths": ("array", "Paths to stage (default: all, '.')")}, category="git")
def git_add(repo: Optional[str] = None, paths: Optional[list[str]] = None) -> dict[str, Any]:
    """git add paths."""
    return _git(["add", *(paths or ["."])], repo, "git_add")


def _heuristic_message(name_status: str) -> str:
    kinds = {"A": "Add", "M": "Update", "D": "Remove", "R": "Rename"}
    lines = [ln.split("\t") for ln in name_status.splitlines() if "\t" in ln]
    if not lines:
        return "Update files"
    files = [ln[-1] for ln in lines]
    verbs = {kinds.get(ln[0][0], "Update") for ln in lines}
    verb = verbs.pop() if len(verbs) == 1 else "Update"
    shown = ", ".join(os.path.basename(f) for f in files[:3])
    more = f" and {len(files) - 3} more" if len(files) > 3 else ""
    return f"{verb} {shown}{more}"


def generate_commit_message(repo: Optional[str]) -> tuple[str, str]:
    """Generate a commit message from the staged diff. Returns (message, source)."""
    ns = run_cmd(["git", "diff", "--cached", "--name-status"], tool="git_commit", cwd=_dir(repo), quiet=True)
    diff = run_cmd(["git", "--no-pager", "diff", "--cached", "--no-color", "-U2"], tool="git_commit",
                   cwd=_dir(repo), quiet=True)
    try:
        from llm import get_router
        text = get_router().complete_text(
            "You write git commit messages. Output ONLY the message: an imperative subject line of at most 72 "
            "characters, optionally a blank line and a short body of bullet points. No quotes, no code fences, "
            "no explanations.", "Write a commit message for this staged diff:\n\n" + clip(diff["output"], 9000))
    except Exception:
        text = None
    if text:
        text = re.sub(r"^```\w*\n|\n```$", "", text.strip()).strip().strip('"')
        if text:
            return text, "llm"
    return _heuristic_message(ns["output"]), "heuristic"


@tool("git_commit", "Commit staged changes. If no message is given one is auto-generated from the diff.",
      {**_REPO, "message": ("string", "Commit message (omit to auto-generate)"),
       "add_all": ("boolean", "Stage all changes first (git add -A)")}, category="git")
def git_commit(repo: Optional[str] = None, message: Optional[str] = None, add_all: bool = False) -> dict[str, Any]:
    """git commit with an optional auto-generated message."""
    if not have("git"):
        return missing_binary("git")
    if STATE.dry_run:
        if add_all:
            _git(["add", "-A"], repo, "git_commit")
        return _git(["commit", "-m", message or "<auto-generated from staged diff>"], repo, "git_commit")
    if add_all:
        added = _git(["add", "-A"], repo, "git_commit")
        if not added["ok"]:
            return added
    staged = run_cmd(["git", "diff", "--cached", "--quiet"], tool="git_commit", cwd=_dir(repo), quiet=True,
                     ok_codes=(0, 1))
    if staged["exit_code"] == 0:
        return result(False, error="Nothing staged to commit (use add_all=true or git_add first).", exit_code=1)
    source = "user"
    if not message:
        message, source = generate_commit_message(repo)
        ui.info(f"Commit message ({source}): {message.splitlines()[0]}")
    res = _git(["commit", "-m", message], repo, "git_commit")
    res["commit_message"] = message
    res["message_source"] = source
    return res


@tool("git_push", "Push to a remote. Uses keyring/.env/credential-helper silently, else asks the user (hidden input).",
      {**_REPO, "remote": ("string", "Remote (default origin)"), "branch": ("string", "Branch (default current)"),
       "force": ("boolean", "Force push (--force-with-lease). Requires confirmation"),
       "set_upstream": ("boolean", "Set upstream (-u)")}, category="git", destructive=True)
def git_push(repo: Optional[str] = None, remote: str = "origin", branch: Optional[str] = None,
             force: bool = False, set_upstream: bool = False) -> dict[str, Any]:
    """git push with secure credential flow."""
    args = ["push"] + (["--force-with-lease"] if force else []) + (["-u"] if set_upstream else []) + [remote]
    if branch:
        args.append(branch)
    confirmed = False
    if force and not STATE.dry_run:
        if not confirm(f"DESTRUCTIVE (git push --force) to {remote}{'/' + branch if branch else ''}. Proceed?"):
            return result(False, error="Cancelled: force push was not confirmed.", exit_code=-2, declined=True)
        confirmed = True
    return _git_network(args, repo, "git_push", remote, "Push", require_auth=True, confirmed=confirmed)


@tool("git_clone", "Clone a repository.",
      {"url": ("string", "Repository URL"), "dest": ("string", "Destination directory"),
       "depth": ("integer", "Shallow clone depth")}, required=["url"], category="git")
def git_clone(url: str, dest: Optional[str] = None, depth: Optional[int] = None) -> dict[str, Any]:
    """git clone with secure auth for private https repos."""
    args = ["clone"] + (["--depth", str(depth)] if depth else []) + [url] + ([dest] if dest else [])
    res = _git_network(args, None, "git_clone", url, "Clone")
    if res["ok"] and not STATE.dry_run:
        name = dest or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
        res["cloned_to"] = str(resolve_path(name))
    return res


@tool("git_stash", "Stash changes: push, pop, apply, list or drop.",
      {**_REPO, "action": ("string", "push|pop|apply|list|drop", ["push", "pop", "apply", "list", "drop"]),
       "message": ("string", "Message for push")}, category="git")
def git_stash(repo: Optional[str] = None, action: str = "push", message: Optional[str] = None) -> dict[str, Any]:
    """git stash <action>."""
    args = ["stash", action] + (["-m", message] if action == "push" and message else [])
    return _git(args, repo, "git_stash")


@tool("git_reset", "Reset HEAD. Mode 'hard' discards changes and requires confirmation.",
      {**_REPO, "target": ("string", "Commit/ref (default HEAD)"),
       "mode": ("string", "soft|mixed|hard", ["soft", "mixed", "hard"])}, category="git", destructive=True)
def git_reset(repo: Optional[str] = None, target: str = "HEAD", mode: str = "mixed") -> dict[str, Any]:
    """git reset --<mode> target (hard is confirmed by the safety layer)."""
    return _git(["reset", f"--{mode}", target], repo, "git_reset")


@tool("git_init", "Initialise a new git repository.", _REPO, category="git")
def git_init(repo: Optional[str] = None) -> dict[str, Any]:
    """git init (creates the directory if needed)."""
    d = resolve_path(repo)
    if not STATE.dry_run:
        d.mkdir(parents=True, exist_ok=True)
    return run_cmd(["git", "init"], tool="git_init", cwd=str(d if d.exists() else STATE.cwd)) \
        if have("git") else missing_binary("git")


_IGNORES = {
    "generic": ".env\n.env.*\n!.env.example\n*.log\nlogs/\n.DS_Store\n.idea/\n.vscode/\n",
    "python": "__pycache__/\n*.py[cod]\n.venv/\nvenv/\n.pytest_cache/\n.mypy_cache/\ndist/\nbuild/\n*.egg-info/\n",
    "node": "node_modules/\nnpm-debug.log*\ndist/\ncoverage/\n",
    "go": "bin/\n*.exe\n*.test\nvendor/\n",
    "terraform": ".terraform/\n*.tfstate\n*.tfstate.*\ncrash.log\n*.tfvars\n",
}


@tool("create_gitignore", "Create a .gitignore (generic secrets/logs + language template).",
      {**_REPO, "kind": ("string", "python|node|go|terraform|generic", list(_IGNORES))}, category="git")
def create_gitignore(repo: Optional[str] = None, kind: str = "generic") -> dict[str, Any]:
    """Write .gitignore (appends missing lines if one exists)."""
    path = resolve_path(repo) / ".gitignore"
    content = _IGNORES["generic"] + (_IGNORES.get(kind, "") if kind != "generic" else "")
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would write {path}:\n{content}")
    existing = path.read_text().splitlines() if path.exists() else []
    add = [ln for ln in content.splitlines() if ln not in existing]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(("\n" if existing and not path.read_text().endswith("\n") else "") + "\n".join(add) + ("\n" if add else ""))
    from tools._core import audit
    audit("create_gitignore", f"write {path} (+{len(add)} lines)", 0)
    return result(True, output=f"{path}: added {len(add)} line(s)")


@tool("github_create_repo", "Create a GitHub repository with the gh CLI (and optionally push).",
      {**_REPO, "name": ("string", "Repo name (default: directory name)"),
       "private": ("boolean", "Private repo (default true)"), "push": ("boolean", "Push current branch")},
      category="git")
def github_create_repo(repo: Optional[str] = None, name: Optional[str] = None, private: bool = True,
                       push: bool = False) -> dict[str, Any]:
    """gh repo create --source=. --remote=origin."""
    if not have("gh"):
        return missing_binary("gh")
    d = _dir(repo)
    args = ["gh", "repo", "create", name or os.path.basename(d), "--private" if private else "--public",
            "--source=.", "--remote=origin"] + (["--push"] if push else [])
    if not STATE.dry_run and not credentials._gh_authed():
        return result(False, error="gh is not authenticated. Run: gh auth login", exit_code=1)
    return run_cmd(args, tool="github_create_repo", cwd=d, timeout=120)


@tool("git_auth_status", "Show which git auth methods are available (keyring, env, helper, SSH keys). No secrets shown.",
      {}, category="git")
def git_auth_status() -> dict[str, Any]:
    """Report available auth methods."""
    st = credentials.auth_status()
    lines = [f"{k}: {v}" for k, v in st.items()]
    return result(True, output="\n".join(lines), data=st)
