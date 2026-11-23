"""A delegation's fixed permission scope (task book §4.4, D2).

The scope only ever narrows what a native agent may do without asking. A native
request is decided inside the scope only when every fact needed for that decision
is present; anything else is reported to the owner, never silently allowed.

A full scope (a filesystem root, network and every command) narrows nothing: the
native agent runs in its own full-access mode (0.9.2, 2026-11-20 task book).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

# A command prefix cannot vouch for what a shell runs after these.
_SHELL_CONTROL = frozenset(";&|<>`$()\n\r")
# Every literal occurrence splits, quoted or not: a quoted separator only makes an
# extra cut, never a missing one, whatever the shell's quoting rules (#647 D1).
_SEPARATORS = re.compile(r"&&|\|\||;")
# Characters a shell may expand or unescape in a ``cd`` target, quoted or not.
_CD_EXPANDS = frozenset("~*?[{^#\\")
# Side-effect tools a narrowed scope makes Claude (SDK or claude-agent-acp) ask about.
# `ask` wins over the user's own allow rules while setting sources stay loaded (§15
# E6). MCP tools are side effects no scope grants: always reported (#650).
SCOPE_ASK = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "mcp__*",
)
# Cross-session messages reach the user's other sessions: always reported (§15 E4).
SCOPE_REPORTED = ("SendMessage",)


@dataclass(frozen=True)
class Scope:
    write_paths: tuple[Path, ...]
    network: bool
    commands: tuple[str, ...]

    @classmethod
    def parse(cls, value: dict[str, Any] | None, cwd: str) -> Scope | None:
        if value is None:
            return None
        paths = value.get("write_paths") or [cwd]
        return cls(
            tuple(_absolute(str(path), cwd) for path in paths),
            value.get("network") is True,
            tuple(str(prefix).strip() for prefix in value.get("commands") or [] if prefix),
        )

    @property
    def full(self) -> bool:
        """Every path, network and every command: nothing left to narrow."""
        return (
            self.network
            and "*" in self.commands
            and any(path.parent == path for path in self.write_paths)
        )

    def writes(self, path: str, cwd: str) -> bool:
        try:
            target = _absolute(path, cwd)
        except (RuntimeError, ValueError):
            # An unknown user's ``~`` or a NUL byte: nobody can say where it lands.
            return False
        return any(target == root or root in target.parents for root in self.write_paths)

    def runs(self, command: str | list[str]) -> bool:
        """Every segment between ``&&``, ``||`` and ``;`` is inside (#647 D1)."""
        text = command if isinstance(command, str) else shlex.join(command)
        if not text.strip():
            return False
        # "*" is the owner granting every command, shell control included.
        if "*" in self.commands:
            return True
        try:
            shlex.split(text)
        except ValueError:
            return False
        return all(self._runs_segment(part.strip(" \t")) for part in _SEPARATORS.split(text))

    def _runs_segment(self, text: str) -> bool:
        """One command between ``&&``, ``||`` and ``;``: a granted prefix or an inside ``cd``."""
        # A trailing backslash escapes the separator's first character in a POSIX
        # shell: `\||` is a literal `|` and a pipe, `\&&` a literal `&` and `&`.
        if not text or text.endswith("\\") or _SHELL_CONTROL & set(text):
            return False
        if any(text == prefix or text.startswith(f"{prefix} ") for prefix in self.commands):
            return True
        if not text.startswith("cd ") or _CD_EXPANDS & set(text):
            return False
        try:
            words = shlex.split(text)
        except ValueError:
            # A quoted separator cut the segment mid-quote.
            return False
        # Absolute only: the shell's own cwd (Claude's Bash keeps it across calls) and
        # CDPATH are unknown here. No `..`: the shell resolves it before symlinks.
        target = PurePath(words[1]) if len(words) == 2 else PurePath()
        return (
            target.is_absolute()
            and ".." not in target.parts
            and self.writes(str(target), str(target))
        )

    def covers(self, path: str) -> bool:
        """Whether the whole directory is writable (an agent mode may then write it)."""
        root = Path(path).resolve()
        return any(root == item or item in root.parents for item in self.write_paths)


def expand_home(path: str) -> str:
    """A directory Yuki may write relative to this user's home (``~/...``)."""
    return os.path.expanduser(path)


def _absolute(path: str, cwd: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    # Resolve symlinks and `..` so a path cannot leave a root by spelling.
    return candidate.resolve()
