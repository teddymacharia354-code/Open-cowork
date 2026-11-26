"""File tools: ls, cat, search, create, edit (with backup), delete (confirm), move, copy, chmod, find large files."""
from __future__ import annotations

import difflib
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Optional

import config
from credentials import redact
from tools._core import STATE, audit, confirm, have, resolve_path, result, run_cmd
from tools.registry import tool

_PROTECTED = {Path(p) for p in ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/sbin",
                                "/sys", "/usr", "/var", "/home", "/opt", "/Users", "/System", "/Library")}
_PROTECTED.add(Path.home())


def _protected(p: Path) -> Optional[str]:
    rp = Path(os.path.realpath(p))
    if rp in _PROTECTED or p in _PROTECTED:
        return f"{rp} is a protected system/home directory"
    return None


def _backup(p: Path) -> Optional[Path]:
    if not p.is_file():
        return None
    dest_dir = config.DATA_DIR / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{p.name}"
    shutil.copy2(p, dest)
    return dest


@tool("ls", "List a directory (name, type, size, modified).",
      {"path": ("string", "Directory (default cwd)"), "all": ("boolean", "Include hidden files")}, category="files")
def ls(path: Optional[str] = None, all: bool = False) -> dict[str, Any]:
    """List directory entries."""
    p = resolve_path(path)
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would list {p}")
    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return result(False, error=f"Cannot list {p}: {e}", exit_code=1)
    lines = []
    for e in entries:
        if e.name.startswith(".") and not all:
            continue
        try:
            st = e.lstat()
            kind = "d" if stat.S_ISDIR(st.st_mode) else ("l" if stat.S_ISLNK(st.st_mode) else "-")
            lines.append(f"{kind} {stat.filemode(st.st_mode)[1:]} {st.st_size:>10} {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))} {e.name}{'/' if kind == 'd' else ''}")
        except OSError:
            lines.append(f"? {e.name}")
    audit("ls", f"ls {p}", 0)
    return result(True, output=f"{p}\n" + ("\n".join(lines) or "(empty)"))


@tool("cat", "Read a text file (secrets are redacted).",
      {"path": ("string", "File path"), "max_lines": ("integer", "Max lines (default 300)"),
       "start": ("integer", "Start line (1-based)")}, required=["path"], category="files")
def cat(path: str, max_lines: int = 300, start: int = 1) -> dict[str, Any]:
    """Read a file."""
    p = resolve_path(path)
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would read {p}")
    try:
        raw = p.read_bytes()
    except OSError as e:
        return result(False, error=f"Cannot read {p}: {e}", exit_code=1)
    if b"\0" in raw[:4096]:
        return result(False, error=f"{p} looks like a binary file ({len(raw)} bytes)", exit_code=1)
    lines = raw.decode("utf-8", "replace").splitlines()
    s = max(start, 1) - 1
    chunk = lines[s:s + max_lines]
    audit("cat", f"cat {p}", 0)
    tail = f"\n... ({len(lines) - s - len(chunk)} more lines)" if len(lines) > s + len(chunk) else ""
    return result(True, output=redact("\n".join(chunk)) + tail)


@tool("search", "Search file contents (ripgrep if installed, else grep).",
      {"pattern": ("string", "Regex/text to find"), "path": ("string", "Directory (default cwd)"),
       "glob": ("string", "File glob e.g. '*.py'"), "ignore_case": ("boolean", "Case-insensitive")},
      required=["pattern"], category="files")
def search(pattern: str, path: Optional[str] = None, glob: Optional[str] = None, ignore_case: bool = False) -> dict[str, Any]:
    """rg/grep for a pattern."""
    d = str(resolve_path(path))
    if have("rg"):
        cmd = ["rg", "-n", "--no-heading", "--max-count", "20"] + (["-i"] if ignore_case else []) + \
              (["-g", glob] if glob else []) + ["-e", pattern, d]
    else:
        cmd = ["grep", "-rnI", "--exclude-dir=.git", "--exclude-dir=node_modules"] + (["-i"] if ignore_case else []) + \
              ([f"--include={glob}"] if glob else []) + ["-e", pattern, d]
    res = run_cmd(cmd, tool="search", ok_codes=(0, 1))
    if res["ok"] and not res["output"]:
        res["output"] = "No matches."
    lines = res["output"].splitlines()
    if len(lines) > 100:
        res["output"] = "\n".join(lines[:100]) + f"\n... {len(lines) - 100} more matches"
    return res


@tool("create_file", "Create a file with the given content (parent dirs created). Refuses to overwrite unless overwrite=true.",
      {"path": ("string", "File path"), "content": ("string", "File content"),
       "overwrite": ("boolean", "Allow overwriting (a backup is kept)"), "executable": ("boolean", "chmod +x")},
      required=["path", "content"], category="files")
def create_file(path: str, content: str, overwrite: bool = False, executable: bool = False) -> dict[str, Any]:
    """Write a new file."""
    p = resolve_path(path)
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would write {len(content)} bytes to {p}")
    if p.exists() and not overwrite:
        return result(False, error=f"{p} already exists (pass overwrite=true to replace; a backup is kept)", exit_code=1)
    try:
        b = _backup(p) if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if executable:
            p.chmod(p.stat().st_mode | 0o111)
    except OSError as e:
        return result(False, error=f"Cannot write {p}: {e}", exit_code=1)
    audit("create_file", f"write {p} ({len(content)} bytes)", 0)
    return result(True, output=f"Wrote {p} ({len(content)} bytes)" + (f"; backup at {b}" if b else ""))


@tool("edit_file", "Edit a file by replacing text (old_text -> new_text) or replacing the whole content. A backup is kept in .open-cowork/backups/.",
      {"path": ("string", "File path"), "old_text": ("string", "Exact text to replace (must be unique)"),
       "new_text": ("string", "Replacement text"), "content": ("string", "Full new content (instead of old/new)"),
       "replace_all": ("boolean", "Replace every occurrence")}, required=["path"], category="files")
def edit_file(path: str, old_text: Optional[str] = None, new_text: Optional[str] = None,
              content: Optional[str] = None, replace_all: bool = False) -> dict[str, Any]:
    """Edit with backup and a unified diff in the result."""
    p = resolve_path(path)
    try:
        before = p.read_text(encoding="utf-8")
    except OSError as e:
        return result(False, error=f"Cannot read {p}: {e}", exit_code=1)
    if content is not None:
        after = content
    elif old_text is not None and new_text is not None:
        n = before.count(old_text)
        if n == 0:
            return result(False, error="old_text not found in file", exit_code=1)
        if n > 1 and not replace_all:
            return result(False, error=f"old_text matches {n} places; make it unique or set replace_all=true", exit_code=1)
        after = before.replace(old_text, new_text)
    else:
        return result(False, error="Provide old_text+new_text, or content", exit_code=2)
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{p.name}", f"b/{p.name}"))
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would edit {p}:\n{diff}")
    b = _backup(p)
    p.write_text(after, encoding="utf-8")
    audit("edit_file", f"edit {p} (backup {b})", 0)
    return result(True, output=f"Edited {p}; backup: {b}\n{diff[:3000]}")


@tool("delete_path", "Delete a file or directory (always asks for confirmation; refuses protected paths).",
      {"path": ("string", "Path to delete"), "recursive": ("boolean", "Allow deleting a directory tree")},
      required=["path"], category="files", destructive=True)
def delete_path(path: str, recursive: bool = False) -> dict[str, Any]:
    """Delete after confirmation."""
    p = resolve_path(path)
    bad = _protected(p)
    if bad:
        audit("delete_path", f"{p} [BLOCKED]", -1)
        return result(False, error=f"Blocked by safety policy: {bad}", exit_code=-1, blocked=True)
    if not p.exists() and not p.is_symlink():
        return result(False, error=f"{p} does not exist", exit_code=1)
    if p.is_dir() and not p.is_symlink() and not recursive:
        return result(False, error=f"{p} is a directory; set recursive=true to delete the whole tree", exit_code=1)
    if STATE.dry_run:
        audit("delete_path", f"{p} [dry-run]", "dry-run")
        return result(True, output=f"[dry-run] would delete {p}")
    if not confirm(f"DESTRUCTIVE (delete): {p}{' (entire directory tree)' if p.is_dir() else ''}\nProceed?"):
        audit("delete_path", f"{p} [DECLINED]", -2)
        return result(False, error="Cancelled: delete was not confirmed.", exit_code=-2, declined=True)
    try:
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    except OSError as e:
        audit("delete_path", f"{p}", 1)
        return result(False, error=f"Delete failed: {e}", exit_code=1)
    audit("delete_path", f"delete {p}", 0)
    return result(True, output=f"Deleted {p}")


def _two(tool_name: str, op: Any, src: str, dst: str, verb: str) -> dict[str, Any]:
    s, d = resolve_path(src), resolve_path(dst)
    bad = _protected(s)
    if bad and tool_name == "move_path":
        return result(False, error=f"Blocked by safety policy: {bad}", exit_code=-1, blocked=True)
    if not s.exists():
        return result(False, error=f"{s} does not exist", exit_code=1)
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would {verb} {s} -> {d}")
    try:
        op(s, d)
    except (OSError, shutil.Error) as e:
        audit(tool_name, f"{verb} {s} {d}", 1)
        return result(False, error=f"{verb} failed: {e}", exit_code=1)
    audit(tool_name, f"{verb} {s} {d}", 0)
    return result(True, output=f"{verb}: {s} -> {d}")


@tool("move_path", "Move or rename a file/directory.", {"src": ("string", "Source"), "dst": ("string", "Destination")},
      required=["src", "dst"], category="files")
def move_path(src: str, dst: str) -> dict[str, Any]:
    """shutil.move."""
    return _two("move_path", lambda s, d: shutil.move(str(s), str(d)), src, dst, "move")


@tool("copy_path", "Copy a file or directory tree.", {"src": ("string", "Source"), "dst": ("string", "Destination")},
      required=["src", "dst"], category="files")
def copy_path(src: str, dst: str) -> dict[str, Any]:
    """shutil.copy2 / copytree."""
    def op(s: Path, d: Path) -> None:
        shutil.copytree(s, d) if s.is_dir() else shutil.copy2(s, d)
    return _two("copy_path", op, src, dst, "copy")


@tool("chmod_path", "Change permissions (octal like 755 or symbolic like +x).",
      {"path": ("string", "Path"), "mode": ("string", "e.g. 755, 600, +x"), "recursive": ("boolean", "-R")},
      required=["path", "mode"], category="files")
def chmod_path(path: str, mode: str, recursive: bool = False) -> dict[str, Any]:
    """chmod with protection for system roots."""
    p = resolve_path(path)
    bad = _protected(p)
    if bad:
        return result(False, error=f"Blocked by safety policy: {bad}", exit_code=-1, blocked=True)
    return run_cmd(["chmod"] + (["-R"] if recursive else []) + [mode, str(p)], tool="chmod_path")


@tool("find_large_files", "Find the largest files under a directory.",
      {"path": ("string", "Directory (default cwd)"), "min_mb": ("integer", "Minimum size in MB (default 100)"),
       "limit": ("integer", "Max results (default 20)")}, category="files")
def find_large_files(path: Optional[str] = None, min_mb: int = 100, limit: int = 20) -> dict[str, Any]:
    """Walk the tree (same filesystem, 40s budget) and list big files."""
    root = resolve_path(path)
    if STATE.dry_run:
        return result(True, output=f"[dry-run] would scan {root} for files > {min_mb} MB")
    found: list[tuple[int, str]] = []
    deadline = time.time() + 40
    dev = os.stat(root).st_dev if root.exists() else None
    skipped = False
    for dp, dn, fn in os.walk(root):
        if time.time() > deadline:
            skipped = True
            break
        dn[:] = [d for d in dn if d not in ("proc", "sys", ".git", "node_modules") and not os.path.islink(os.path.join(dp, d))
                 and os.lstat(os.path.join(dp, d)).st_dev == dev]
        for f in fn:
            fp = os.path.join(dp, f)
            try:
                sz = os.lstat(fp).st_size
            except OSError:
                continue
            if sz >= min_mb * 1024 * 1024 and not os.path.islink(fp):
                found.append((sz, fp))
    found.sort(reverse=True)
    audit("find_large_files", f"scan {root} >{min_mb}MB", 0)
    lines = [f"{sz / 1048576:>10.1f} MB  {fp}" for sz, fp in found[:limit]]
    return result(True, output="\n".join(lines) or f"No files larger than {min_mb} MB.") if not skipped else \
        result(True, output="\n".join(lines) + "\n(scan stopped at the 40s time budget; results may be partial)")
