"""A delegation's fixed permission scope (task book §4.4, D2).

The scope only ever narrows what a native agent may do without asking. A native
request is decided inside the scope only when every fact needed for that decision
is present; anything else is reported to the owner, never silently allowed.

A full scope (a filesystem root, network and every command) narrows nothing: the
native agent runs in its own full-access mode (0.9.2, 2026-11-20 task book).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A command prefix cannot vouch for what a shell runs after these.
_SHELL_CONTROL = frozenset(";&|<>`$()\n\r")


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
        target = _absolute(path, cwd)
        return any(target == root or root in target.parents for root in self.write_paths)

    def runs(self, command: str | list[str]) -> bool:
        text = command if isinstance(command, str) else shlex.join(command)
        text = text.strip()
        if not text:
            return False
        # "*" is the owner granting every command, shell control included.
        if "*" in self.commands:
            return True
        if _SHELL_CONTROL & set(text):
            return False
        return any(text == prefix or text.startswith(f"{prefix} ") for prefix in self.commands)

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
