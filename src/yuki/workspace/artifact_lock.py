"""Shared and exclusive lock for one private artifact root.

The lock file keeps a stable identity. Acquire does not write, truncate, or
replace it, and it stores no pid, age, or version.
"""

from __future__ import annotations

import os
from pathlib import Path

_LOCK_NAME = ".artifact.lock"


class ArtifactLockBusy(OSError):
    pass


class ArtifactRootLock:
    def __init__(self, root: Path) -> None:
        self.path = root / _LOCK_NAME

    def acquire(self, *, exclusive: bool, wait: bool = True) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
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


def _lock(descriptor: int, *, exclusive: bool, wait: bool) -> None:
    if os.name == "nt":
        _lock_windows(descriptor, exclusive=exclusive, wait=wait)
        return
    import fcntl

    flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not wait:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(descriptor, flags)
    except BlockingIOError as exc:
        raise ArtifactLockBusy("artifact_lock_busy") from exc


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        _unlock_windows(descriptor)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _lock_windows(descriptor: int, *, exclusive: bool, wait: bool) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    flags = 0x2 if exclusive else 0
    if not wait:
        flags |= 0x1

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    overlapped = _Overlapped()
    handle = msvcrt.get_osfhandle(descriptor)
    locked = kernel32.LockFileEx(
        wintypes.HANDLE(handle),
        wintypes.DWORD(flags),
        wintypes.DWORD(0),
        wintypes.DWORD(0xFFFFFFFF),
        wintypes.DWORD(0xFFFFFFFF),
        ctypes.byref(overlapped),
    )
    if not locked:
        raise ArtifactLockBusy("artifact_lock_busy")


def _unlock_windows(descriptor: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    handle = msvcrt.get_osfhandle(descriptor)
    kernel32.UnlockFileEx(
        wintypes.HANDLE(handle),
        wintypes.DWORD(0),
        wintypes.DWORD(0xFFFFFFFF),
        wintypes.DWORD(0xFFFFFFFF),
        ctypes.byref(_Overlapped()),
    )
