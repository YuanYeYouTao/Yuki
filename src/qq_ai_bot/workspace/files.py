"""POSIX workspace operations rooted at directory FDs, never host-followed symlinks.

Terminal writes are visible immediately. Versions hash actual bytes, not a stale
artifact index. The host never interprets a container's absolute symlink target.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from qq_ai_bot.workspace.store import WorkspaceError

MAX_TEXT = 32768
MAX_FILE = 200 * 1024 * 1024


class FileWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.lock = threading.RLock()

    def root_fd(self) -> int:
        """Check every component, including user-writable ancestors of work/outputs."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        try:
            for part in self.root.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def parts(path: str) -> tuple[str, ...]:
        if not isinstance(path, str) or len(path) > 4096 or "\x00" in path or "\\" in path:
            raise WorkspaceError("invalid_workspace_path")
        if path == "/workspace":
            path = ""
        elif path.startswith("/workspace/"):
            path = path[len("/workspace/") :]
        elif path.startswith("/"):
            raise WorkspaceError("path_outside_workspace")
        parts = PurePosixPath(path).parts
        if ".." in parts or any(len(p.encode()) > 255 for p in parts):
            raise WorkspaceError("path_outside_workspace")
        return parts

    @contextmanager
    def parent(self, path: str, *, mkdir: bool = False) -> Iterator[tuple[int, str]]:
        parts = self.parts(path)
        if not parts:
            raise WorkspaceError("workspace_root_operation_denied")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = self.root_fd()
        try:
            for part in parts[:-1]:
                if mkdir:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                        if os.geteuid() == 0:
                            os.chown(part, 10001, 10001, dir_fd=fd, follow_symlinks=False)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)

    @contextmanager
    def open_file(self, path: str) -> Iterator[int]:
        with self.parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspaceError("unsafe_workspace_file")
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def fingerprint(fd: int) -> tuple[str, os.stat_result]:
        before = os.fstat(fd)
        if before.st_size > MAX_FILE:
            raise WorkspaceError("use_terminal_for_large_file")
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while data := os.read(fd, 65536):
            digest.update(data)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise WorkspaceError("file_changed_during_read")
        return digest.hexdigest(), after

    def read(self, path: str, *, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise WorkspaceError("invalid_offset")
        with self.open_file(path) as fd:
            version, info = self.fingerprint(fd)
            os.lseek(fd, offset, os.SEEK_SET)
            data = os.read(fd, MAX_TEXT)
        result: dict[str, Any] = {
            "path": "/workspace/" + "/".join(self.parts(path)),
            "version": version,
            "size": info.st_size,
            "offset": offset,
            "next_offset": offset + len(data),
            "truncated": offset + len(data) < info.st_size,
            "external_untrusted": True,
        }
        try:
            import codecs

            decoder = codecs.getincrementaldecoder("utf-8")()
            value = decoder.decode(data, final=offset + len(data) >= info.st_size)
            result["next_offset"] -= len(decoder.getstate()[0])
            if "\x00" in value:
                raise UnicodeError
            result["text"] = value
        except UnicodeError:
            result["binary"] = True
        return result

    def write(self, path: str, data: bytes, expected_version: str | None = None) -> dict[str, Any]:
        if len(data) > MAX_FILE:
            raise WorkspaceError("file_too_large")
        with self.lock, self.parent(path, mkdir=True) as (parent, name):
            try:
                with self.open_file(path) as fd:
                    actual, _ = self.fingerprint(fd)
            except FileNotFoundError:
                actual = "missing"
            if (actual != "missing" and expected_version != actual) or (
                actual == "missing" and expected_version not in {None, "missing"}
            ):
                raise WorkspaceError("version_conflict")
            temporary = ".yuki-write-" + uuid4().hex
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    if os.geteuid() == 0:
                        os.fchown(stream.fileno(), 10001, 10001)
                # Recheck after staging: terminal edits during a slow write conflict.
                try:
                    with self.open_file(path) as current:
                        latest, _ = self.fingerprint(current)
                except FileNotFoundError:
                    latest = "missing"
                if latest != actual:
                    raise WorkspaceError("version_conflict")
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {
            "path": "/workspace/" + "/".join(self.parts(path)),
            "version": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }

    @contextmanager
    def directory(self, path: str) -> Iterator[int]:
        if not self.parts(path):
            fd = self.root_fd()
        else:
            with self.parent(path) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            yield fd
        finally:
            os.close(fd)

    def listing(self, path: str = "", *, cursor: str = "", limit: int = 50) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise WorkspaceError("invalid_limit")
        with self.directory(path) as fd:
            names = sorted(name for name in os.listdir(fd) if name > cursor)
            items = []
            for name in names[:limit]:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode)
                    else "special"
                )
                items.append(
                    {
                        "name": name,
                        "path": str(PurePosixPath("/workspace", *self.parts(path), name)),
                        "kind": kind,
                        "size": info.st_size,
                        "modified_at": info.st_mtime,
                    }
                )
        return {
            "items": items,
            "next_cursor": names[limit - 1] if len(names) > limit else None,
            "external_untrusted": True,
        }

    def mkdir(self, path: str) -> dict[str, Any]:
        with self.parent(path, mkdir=True) as (fd, name):
            try:
                os.mkdir(name, 0o755, dir_fd=fd)
                if os.geteuid() == 0:
                    os.chown(name, 10001, 10001, dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                with self.directory(path):
                    pass
        return {"path": path, "created": True}

    def move(self, path: str, destination: str, expected_version: str | None) -> dict[str, Any]:
        with (
            self.lock,
            self.parent(path) as (src, name),
            self.parent(destination, mkdir=True) as (dst, target),
        ):
            info = os.stat(name, dir_fd=src, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                with self.open_file(path) as fd:
                    actual, _ = self.fingerprint(fd)
                if actual != expected_version:
                    raise WorkspaceError("version_conflict")
            elif not stat.S_ISDIR(info.st_mode):
                raise WorkspaceError("unsafe_workspace_file")
            try:
                os.stat(target, dir_fd=dst, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceError("destination_exists")
            os.rename(name, target, src_dir_fd=src, dst_dir_fd=dst)
        return {"path": destination, "moved": True}

    def delete(self, path: str, expected_version: str | None) -> dict[str, Any]:
        with self.lock, self.parent(path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                # Explicit empty-directory removal; recursive cleanup can use the terminal.
                os.rmdir(name, dir_fd=parent)
            else:
                with self.open_file(path) as fd:
                    actual, _ = self.fingerprint(fd)
                if actual != expected_version:
                    raise WorkspaceError("version_conflict")
                os.unlink(name, dir_fd=parent)
        return {"path": path, "deleted": True}

    def patch(self, path: str, old: str, new: str, expected_version: str) -> dict[str, Any]:
        current = self.read(path)
        if current.get("binary") or current["size"] > MAX_TEXT:
            raise WorkspaceError("use_terminal_for_large_or_binary_edit")
        if current["version"] != expected_version:
            raise WorkspaceError("version_conflict")
        if not old or current["text"].count(old) != 1:
            raise WorkspaceError("patch_match_not_unique")
        return self.write(path, current["text"].replace(old, new, 1).encode(), expected_version)

    def search(self, query: str, path: str = "") -> dict[str, Any]:
        if not query or len(query) > 512:
            raise WorkspaceError("invalid_query")
        results: list[dict[str, Any]] = []
        pending = [(path, "")]
        visited = 0
        while pending and visited < 1000 and len(results) < 50:
            current, cursor = pending.pop()
            page = self.listing(current, cursor=cursor, limit=100)
            if page["next_cursor"]:
                pending.append((current, page["next_cursor"]))
            for item in page["items"]:
                visited += 1
                if item["kind"] == "directory":
                    if item["name"] not in {".git", "node_modules", ".venv"}:
                        pending.append((item["path"], ""))
                elif item["kind"] == "file" and item["size"] <= MAX_TEXT:
                    try:
                        content = self.read(item["path"]).get("text", "")
                    except (WorkspaceError, OSError):
                        continue
                    for number, line in enumerate(content.splitlines(), 1):
                        if query in line:
                            results.append(
                                {"path": item["path"], "line": number, "text": line[:500]}
                            )
                            if len(results) >= 50:
                                break
                if len(results) >= 50 or visited >= 1000:
                    break
        return {
            "matches": results,
            "bounded_search": True,
            "truncated": bool(pending) or visited >= 1000 or len(results) >= 50,
            "external_untrusted": True,
        }
