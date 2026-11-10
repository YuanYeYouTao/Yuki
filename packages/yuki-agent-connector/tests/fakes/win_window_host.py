"""A Windows test window with standard controls, covered by a topmost window it also owns.

Usage: python win_window_host.py <log>
"Yuki test host" holds a button, a single-line edit and a multi-line edit with 300 lines;
"Yuki test cover" is a topmost window over its upper half. Neither is activated. Button
clicks, activation and commands go to <log> as JSON lines; <log>.cmd takes "mini".
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

LOG = Path(sys.argv[1])
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
TIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.DWORD)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, TIMERPROC]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]

WS_OVERLAPPEDWINDOW, WS_CHILD, WS_VISIBLE, WS_BORDER, WS_VSCROLL = (
    0x00CF0000,
    0x40000000,
    0x10000000,
    0x00800000,
    0x00200000,
)
WS_POPUP, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000000, 0x00000008, 0x08000000
ES_MULTILINE, ES_AUTOVSCROLL, ES_AUTOHSCROLL = 0x0004, 0x0040, 0x0080
WM_DESTROY, WM_ACTIVATE, WM_COMMAND = 0x0002, 0x0006, 0x0111
SW_SHOWNOACTIVATE, SW_SHOWMINNOACTIVE = 4, 7


def log(**fields):
    fields["t"] = time.time()
    with LOG.open("a") as out:
        out.write(json.dumps(fields) + "\n")


@WNDPROC
def procedure(window, message, wparam, lparam):
    if message == WM_COMMAND and wparam & 0xFFFF == 1 and wparam >> 16 == 0:
        log(ev="clicked")
    elif message == WM_ACTIVATE:
        log(ev="activate", state=wparam & 0xFFFF)
    elif message == WM_DESTROY:
        user32.PostQuitMessage(0)
    return user32.DefWindowProcW(window, message, wparam, lparam)


@TIMERPROC
def poll(window, message, identity, tick):
    command = LOG.with_suffix(".cmd")
    if not command.exists():
        return
    words = command.read_text().split()
    command.unlink()
    if words[0] == "mini":
        user32.ShowWindow(main, SW_SHOWMINNOACTIVE)
    log(ev="cmd", words=words)


instance = kernel32.GetModuleHandleW(None)
user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4))
window_class = WNDCLASSW()
window_class.lpfnWndProc = procedure
window_class.hInstance = instance
window_class.hbrBackground = wintypes.HBRUSH(6)  # COLOR_WINDOW + 1
window_class.lpszClassName = "YukiTestHost"
user32.RegisterClassW(ctypes.byref(window_class))


def child(kind, text, style, x, y, width, height, identity):
    return user32.CreateWindowExW(
        0, kind, text, WS_CHILD | WS_VISIBLE | style, x, y, width, height, main, identity,
        instance, None,
    )


main = user32.CreateWindowExW(
    0, "YukiTestHost", "Yuki test host", WS_OVERLAPPEDWINDOW, 200, 200, 520, 420, None, None,
    instance, None,
)
button = child("BUTTON", "Press me", 0, 10, 10, 160, 32, 1)
edit = child("EDIT", "", WS_BORDER | ES_AUTOHSCROLL, 180, 14, 300, 24, 2)
lines = child(
    "EDIT",
    "\r\n".join(f"line {index}" for index in range(300)),
    WS_BORDER | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
    10,
    60,
    480,
    280,
    3,
)
user32.ShowWindow(main, SW_SHOWNOACTIVATE)
# A topmost window over the host's upper half: screen capture would show it, PrintWindow not.
cover = user32.CreateWindowExW(
    WS_EX_TOPMOST | WS_EX_NOACTIVATE, "YukiTestHost", "Yuki test cover", WS_POPUP | WS_BORDER,
    150, 180, 420, 120, None, None, instance, None,
)
user32.ShowWindow(cover, SW_SHOWNOACTIVATE)
user32.SetTimer(None, 0, 200, poll)
log(ev="ready", pid=os.getpid(), main=main, button=button, edit=edit, lines=lines, cover=cover)
message = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(message))
    user32.DispatchMessageW(ctypes.byref(message))
