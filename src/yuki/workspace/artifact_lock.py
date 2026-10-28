"""Shared and exclusive lock for one private artifact root.

The lock file keeps a stable identity. Acquire does not write, truncate, or
replace it, and it stores no pid, age, or version.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_LOCK_NAME = ".artifact.lock"


class ArtifactLockBusy(OSError):
    pass


class ArtifactRootLock:
    def __init__(self, root: Path) -> None:
        self.path = root / _LOCK_NAME

    def acquire(self, *, exclusive: bool, wait: bool = True) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o660)
        try:
            _lock(descriptor, exclusive=exclusive, wait=wait)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def release(self, descriptor: int) -> None:
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)


if sys.platform == "win32":

    def _lock(descriptor: int, *, exclusive: bool, wait: bool) -> None:
        # LockFileEx shared/exclusive. msvcrt.locking is exclusive-only and is not used.
        ctypes, msvcrt, kernel32, overlapped = _windows_locking()
        flags = 0x2 if exclusive else 0
        if not wait:
            flags |= 0x1
        locked = kernel32.LockFileEx(
            msvcrt.get_osfhandle(descriptor),
            flags,
            0,
            1,
            0,
            ctypes.byref(overlapped()),
        )
        if locked:
            return
        error = ctypes.get_last_error()
        if error in {33, 167}:  # ERROR_LOCK_VIOLATION, ERROR_LOCK_FAILED
            raise ArtifactLockBusy("artifact_lock_busy")
        raise OSError(error, "LockFileEx failed")

    def _unlock(descriptor: int) -> None:
        ctypes, msvcrt, kernel32, overlapped = _windows_locking()
        kernel32.UnlockFileEx(
            msvcrt.get_osfhandle(descriptor),
            0,
            1,
            0,
            ctypes.byref(overlapped()),
        )

    def _windows_locking() -> tuple[Any, Any, Any, type[Any]]:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _Overlapped(ctypes.Structure):
            _fields_ = (
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LockFileEx.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        )
        kernel32.LockFileEx.restype = wintypes.BOOL
        kernel32.UnlockFileEx.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        )
        kernel32.UnlockFileEx.restype = wintypes.BOOL
        return ctypes, msvcrt, kernel32, _Overlapped

else:

    def _lock(descriptor: int, *, exclusive: bool, wait: bool) -> None:
        import fcntl

        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not wait:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, flags)
        except BlockingIOError as exc:
            raise ArtifactLockBusy("artifact_lock_busy") from exc

    def _unlock(descriptor: int) -> None:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
