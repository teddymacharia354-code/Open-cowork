"""Tiny terminal UI helpers: ANSI colours, tables, output clipping."""
from __future__ import annotations

import os
import sys
from typing import Sequence

_CODES = {
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "dim": "2", "bold": "1",
}


def _color_enabled() -> bool:
    """Return True when ANSI colour should be emitted."""
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def c(text: str, color: str) -> str:
    """Wrap ``text`` in the ANSI colour ``color`` if the terminal supports it."""
    if not _color_enabled() or color not in _CODES:
        return text
    return f"\033[{_CODES[color]}m{text}\033[0m"


def info(msg: str) -> None:
    """Print an informational line."""
    print(msg)


def dim(msg: str) -> None:
    """Print a dimmed line."""
    print(c(msg, "dim"))


def warn(msg: str) -> None:
    """Print a warning line."""
    print(c(msg, "yellow"))


def error(msg: str) -> None:
    """Print an error line."""
    print(c(msg, "red"))


def good(msg: str) -> None:
    """Print a success line."""
    print(c(msg, "green"))


def clip_lines(text: str, max_lines: int = 40) -> str:
    """Return ``text`` truncated to ``max_lines`` lines (with a marker)."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head = lines[: max_lines // 2]
    tail = lines[-(max_lines // 2):]
    return "\n".join(head + [f"... ({len(lines) - max_lines} lines hidden) ..."] + tail)


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a simple left-aligned text table."""
    cells = [[str(x) for x in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in cells:
        for i, x in enumerate(r):
            widths[i] = max(widths[i], len(x.split("\n")[0]))
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*[x.split("\n")[0] for x in r]) for r in cells]
    return "\n".join(lines)
