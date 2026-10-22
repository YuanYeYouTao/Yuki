"""FD operations for selected media, immutable publication and direct file copies.

Ordinary file operations belong to the terminal. The Manager only opens its
real shared workspace or runtime files here, without following host symlinks.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from yuki.workspace.store import WorkspaceError


class FileWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

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

    def write(self, path: str, data: bytes) -> dict[str, Any]:
        return self._write(path, (data,))

    def copy(
        self, path: str, source_fd: int, *, expected_version: str | None = None
    ) -> dict[str, Any]:
        os.lseek(source_fd, 0, os.SEEK_SET)
        return self._write(
            path,
            iter(lambda: os.read(source_fd, 65536), b""),
            source_fd=source_fd,
            expected_version=expected_version,
        )

    def _write(
        self,
        path: str,
        chunks: Iterable[bytes],
        *,
        source_fd: int | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        before = os.fstat(source_fd) if source_fd is not None else None
        digest = hashlib.sha256()
        size = 0
        with self.parent(path, mkdir=True) as (parent, name):
            temporary = ".yuki-write-" + uuid4().hex
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    for chunk in chunks:
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    if source_fd is not None and before is not None:
                        after = os.fstat(source_fd)
                        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                        ) or size != after.st_size:
                            raise WorkspaceError("file_changed_during_read")
                    version = digest.hexdigest()
                    if expected_version is not None and version != expected_version:
                        raise WorkspaceError("version_conflict")
                    stream.flush()
                    os.fsync(stream.fileno())
                    if os.geteuid() == 0:
                        os.fchown(stream.fileno(), 10001, 10001)
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {
            "path": "/workspace/" + "/".join(self.parts(path)),
            "version": version,
            "size": size,
        }
