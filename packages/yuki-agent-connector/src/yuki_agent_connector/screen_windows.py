"""Windows: GDI screenshots, GDI+ JPEG and SendInput, all through ctypes.

The process is per-monitor-v2 DPI aware, so monitor rectangles, the virtual screen and
captures are physical pixels and a 150 % display is not scaled twice. ``SendInput`` is
silently dropped by UIPI for a window of a higher integrity level, so input is refused
up front as ``target_elevated``; the secure desktop (lock screen, UAC) is refused as
``desktop_unavailable`` before any capture or input.
"""

from __future__ import annotations

import ctypes
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .screen import EVENT_TAG, Display, Refused, Stale, Window, to_virtual_desk

assert sys.platform == "win32"

# comtypes initialises COM in the thread that first imports it: make that, like every
# thread that uses UI Automation here, the multithreaded apartment.
sys.coinit_flags = 0  # type: ignore[attr-defined]

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shcore = ctypes.WinDLL("shcore")
gdiplus = ctypes.WinDLL("gdiplus")
dwmapi = ctypes.WinDLL("dwmapi")

ULONG_PTR = ctypes.c_size_t
HANDLE = wintypes.HANDLE


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTS(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("value", _INPUTS)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class EncoderParameter(ctypes.Structure):
    _fields_ = [
        ("Guid", GUID),
        ("NumberOfValues", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("Value", ctypes.c_void_p),
    ]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", wintypes.UINT), ("Parameter", EncoderParameter * 1)]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


def guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


JPEG_ENCODER = guid("557cf401-1a04-11d3-9a73-0000f81ef32e")
ENCODER_QUALITY = guid("1d5be4b5-fa4a-452d-9cdd-5db35105e7eb")
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

user32.SetProcessDpiAwarenessContext.argtypes = [HANDLE]
user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC,
    ctypes.c_void_p,
    MONITORENUMPROC,
    wintypes.LPARAM,
]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.OpenInputDesktop.restype = HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.GetUserObjectInformationW.argtypes = [
    HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
user32.CloseDesktop.argtypes = [HANDLE]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.StretchBlt.argtypes = (
    [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.DWORD]
)
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFOHEADER),
    wintypes.UINT,
]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.OpenProcess.restype = HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.GetCurrentProcess.restype = HANDLE
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetIconInfo.argtypes = [HANDLE, ctypes.POINTER(ICONINFO)]
user32.DrawIconEx.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    HANDLE,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
    HANDLE,
    wintypes.UINT,
]
dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
advapi32.OpenProcessToken.argtypes = [HANDLE, wintypes.DWORD, ctypes.POINTER(HANDLE)]
advapi32.GetTokenInformation.argtypes = [
    HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
shcore.GetDpiForMonitor.argtypes = [
    wintypes.HMONITOR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
]
gdiplus.GdiplusStartup.argtypes = [
    ctypes.POINTER(ULONG_PTR),
    ctypes.POINTER(GdiplusStartupInput),
    ctypes.c_void_p,
]
gdiplus.GdipCreateBitmapFromScan0.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
gdiplus.GdipSaveImageToFile.argtypes = [
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(GUID),
    ctypes.POINTER(EncoderParameters),
]
gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
BUTTON_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
CURSOR_SHOWING, DI_NORMAL, SM_CXCURSOR, SM_CYCURSOR = 0x1, 0x3, 13, 14
# UI Automation: the element no longer exists (UIA_E_ELEMENTNOTAVAILABLE).
ELEMENT_NOT_AVAILABLE = -2147220991
CONTROL_TYPES = dict(
    enumerate(
        (
            "button calendar check_box combo_box edit hyperlink image list_item list menu "
            "menu_bar menu_item progress_bar radio_button scroll_bar slider spinner status_bar "
            "tab tab_item text tool_bar tool_tip tree tree_item custom group thumb data_grid "
            "data_item document split_button window pane header header_item table title_bar "
            "separator semantic_zoom app_bar"
        ).split(),
        start=50000,
    )
)
CONTAINER_TYPES = frozenset({"pane", "group", "custom"})
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
WHEEL_DELTA = 120
SRCCOPY, CAPTUREBLT, HALFTONE = 0x00CC0020, 0x40000000, 4
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
MONITORINFOF_PRIMARY = 1
PROCESS_QUERY_LIMITED_INFORMATION, TOKEN_QUERY, TOKEN_INTEGRITY_LEVEL = 0x1000, 0x0008, 25
GA_ROOT, GWL_EXSTYLE, WS_EX_TOOLWINDOW = 2, -20, 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED, PW_RENDERFULLCONTENT = 9, 14, 2
PIXEL_FORMAT_32BPP_RGB = 0x00022009
VIRTUAL_KEYS = {
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
    **{chr(code): code for code in range(ord("0"), ord("9") + 1)},
    **{f"f{number}": 0x6F + number for number in range(1, 21)},
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "backspace": 0x08,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "cmd": 0x5B,
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}
# Navigation keys share scan codes with the numeric keypad unless sent as extended keys.
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}


class WindowsScreen:
    def __init__(self) -> None:
        # Fails harmlessly when the executable's manifest already set an awareness.
        user32.SetProcessDpiAwarenessContext(HANDLE(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        startup = GdiplusStartupInput(1, None, False, False)
        token = ULONG_PTR()
        status = gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None)
        if status:
            raise OSError(f"GdiplusStartup failed: {status}")

    def permissions(self) -> tuple[bool, bool]:
        # No user-granted capture or input permission exists on Windows.
        return True, True

    def displays(self) -> list[Display]:
        monitors: list[int] = []

        def found(monitor: int, dc: Any, rect: Any, data: Any) -> bool:
            monitors.append(monitor)
            return True

        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(found), 0)
        result = []
        for monitor in monitors:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
            shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
            rect = info.rcMonitor
            width, height = rect.right - rect.left, rect.bottom - rect.top
            result.append(
                Display(
                    info.szDevice,
                    rect.left,
                    rect.top,
                    width,
                    height,
                    width,
                    height,
                    bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    dpi_x.value / 96,
                )
            )
        return result

    def unavailable(self) -> dict[str, Any] | None:
        # The lock screen and UAC prompts are the Winlogon secure desktop: the input
        # desktop cannot be opened or is not this session's Default desktop.
        desktop = user32.OpenInputDesktop(0, False, 0x0001)
        name = ""
        if desktop:
            buffer = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            user32.GetUserObjectInformationW(
                desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
            )
            user32.CloseDesktop(desktop)
            name = buffer.value
        if name.lower() != "default":
            return {
                "error": "desktop_unavailable",
                "message": "这台电脑正显示锁屏或系统安全提示（UAC）；本人处理后才能看和操作屏幕。",
            }
        return None

    def _virtual(self) -> tuple[int, int, int, int]:
        left, top, width, height = (user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))
        return left, top, width, height

    def capture(
        self,
        display: Display,
        width: int,
        height: int,
        path: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> None:
        _jpeg(self.grab(display, width, height, region), width, height, path)

    def grab(
        self,
        display: Display,
        width: int,
        height: int,
        region: tuple[float, float, float, float] | None = None,
    ) -> Any:
        """Top-down 32-bit pixels of ``display`` (or ``region`` of it), the pointer drawn in."""
        left, top, extent_x, extent_y = region or (0, 0, display.width, display.height)
        source = (
            round(display.x + left),
            round(display.y + top),
            round(extent_x),
            round(extent_y),
        )
        screen = user32.GetDC(None)
        try:
            # GDI copies no cursor; it is drawn over the scaled picture, at its scale.
            return _stretch(
                screen, source, width, height, SRCCOPY | CAPTUREBLT, cursor=(source, width)
            )
        finally:
            user32.ReleaseDC(None, screen)

    def pointer(self) -> tuple[float, float] | None:
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return float(point.x), float(point.y)

    def hit(self, x: float, y: float) -> dict[str, str] | None:
        import comtypes

        module, client = automation()
        try:
            element = client.ElementFromPoint(module.tagPOINT(round(x), round(y)))
            return describe(element) if element else None
        except comtypes.COMError:
            return None

    def windows(self) -> list[dict[str, str]]:
        handles: list[int] = []

        def found(handle: int, data: Any) -> bool:
            handles.append(handle)
            return True

        user32.EnumWindows(WNDENUMPROC(found), 0)
        own = kernel32.GetCurrentProcessId()
        listed = []
        for handle in handles:
            title = _title(handle)
            if (
                title
                and user32.IsWindowVisible(handle)
                and not user32.GetWindowLongW(handle, GWL_EXSTYLE) & WS_EX_TOOLWINDOW
                # Cloaked: a suspended UWP frame or a window on another virtual desktop.
                and not _cloaked(handle)
                and _process(handle) != own
            ):
                listed.append({"id": str(handle), "app": _app(handle), "title": title})
        return listed

    def window(self, identity: str) -> WindowsTarget:
        handle = int(identity) if identity.isdigit() else 0
        if (
            not handle
            or not user32.IsWindow(handle)
            or user32.GetAncestor(handle, GA_ROOT) != handle
        ):
            raise Refused(
                "window_not_found",
                f"找不到窗口 {identity}（可能已关闭）；"
                "先 computer(action=screenshot) 看 windows 列表。",
            )
        if user32.IsIconic(handle) or not user32.IsWindowVisible(handle):
            raise Refused(
                "window_unavailable",
                "这个窗口已最小化或隐藏；Yuki 不还原它（还原会抢走前台）。本人还原后再试，"
                "或省略 window 在前台操作。",
            )
        bounds = wintypes.RECT()
        # The visible frame, without the invisible resize borders GetWindowRect includes.
        result = dwmapi.DwmGetWindowAttribute(
            handle, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(bounds), ctypes.sizeof(bounds)
        )
        if result:
            raise OSError(f"DwmGetWindowAttribute failed: {result:#x}")
        width, height = bounds.right - bounds.left, bounds.bottom - bounds.top
        return WindowsTarget(
            Window(
                identity,
                _app(handle),
                _title(handle),
                bounds.left,
                bounds.top,
                width,
                height,
                width,
                height,
            ),
            handle,
            _process(handle),
        )

    def _ticks(self) -> tuple[int, int]:
        # Session-wide: local devices, Remote Desktop and every SendInput, Yuki's included
        # (pointer movement too). The 32-bit tick of the last input, unwrapped against one
        # 64-bit tick read: exact.
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        now = int(kernel32.GetTickCount64())
        return now - ((now - info.dwTime) & 0xFFFFFFFF), now

    def last_input(self) -> tuple[float, float]:
        tick = float(self._ticks()[0])
        return tick, tick

    def idle(self) -> float:
        tick, now = self._ticks()
        return (now - tick) / 1000

    def elevated(self, point: tuple[float, float] | None) -> bool:
        window = (
            user32.WindowFromPoint(wintypes.POINT(int(point[0]), int(point[1])))
            if point is not None
            else user32.GetForegroundWindow()
        )
        if not window:
            return False
        return _elevated(_process(user32.GetAncestor(window, GA_ROOT)))

    def _send(self, inputs: list[INPUT]) -> None:
        array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            error = ctypes.WinError(ctypes.get_last_error())
            # Only part of the batch went in (UIPI, a desktop switch): whatever it pressed
            # is released, so no key or button stays down (A17).
            releases = _releases(inputs[:sent])
            if releases:
                user32.SendInput(
                    len(releases), (INPUT * len(releases))(*releases), ctypes.sizeof(INPUT)
                )
            raise error

    def _mouse(self, flags: int, point: tuple[float, float] | None = None, data: int = 0) -> INPUT:
        x = y = 0
        if point is not None:
            x, y = to_virtual_desk(self._virtual(), *point)
            flags |= MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        value = _INPUTS(mi=MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, EVENT_TAG))
        return INPUT(INPUT_MOUSE, value)

    def _held(self, modifiers: tuple[str, ...], inputs: list[INPUT]) -> list[INPUT]:
        """``inputs`` wrapped in ``modifiers``: pressed and released within the same batch, so
        the owner's own keys never pick them up (A18)."""
        codes = [VIRTUAL_KEYS[name] for name in modifiers]
        return (
            [self._keyboard(code, 0) for code in codes]
            + inputs
            + [self._keyboard(code, KEYEVENTF_KEYUP) for code in reversed(codes)]
        )

    def move(self, x: float, y: float) -> str:
        self._send([self._mouse(0, (x, y))])
        return "global_input"

    def click(
        self, x: float, y: float, button: str, count: int, modifiers: tuple[str, ...]
    ) -> str:
        down, up = BUTTON_FLAGS[button]
        # One batch: the clicks of a triple click arrive within the double-click time.
        self._send(
            self._held(
                modifiers,
                [self._mouse(0, (x, y))]
                + [self._mouse(flag) for _ in range(count) for flag in (down, up)],
            )
        )
        return "global_input"

    def drag(
        self, x: float, y: float, x2: float, y2: float, modifiers: tuple[str, ...]
    ) -> str:
        down, up = BUTTON_FLAGS["left"]
        codes = [VIRTUAL_KEYS[name] for name in modifiers]
        # Modifiers go down with the button and up with it: held only while dragging (A18).
        self._send(
            [self._keyboard(code, 0) for code in codes]
            + [self._mouse(0, (x, y)), self._mouse(down)]
        )
        try:
            steps = 12
            for step in range(1, steps + 1):
                # Applications track a drag from intermediate positions, not only its end.
                time.sleep(0.01)
                self._send(
                    [self._mouse(0, (x + (x2 - x) * step / steps, y + (y2 - y) * step / steps))]
                )
        finally:
            self._send(
                [self._mouse(up)]
                + [self._keyboard(code, KEYEVENTF_KEYUP) for code in reversed(codes)]
            )
        return "global_input"

    def scroll(self, x: float, y: float, dx: int, dy: int, modifiers: tuple[str, ...]) -> str:
        inputs = [self._mouse(0, (x, y))]
        if dy:
            # Positive wheel data scrolls forward (up); dy is positive downward.
            inputs.append(self._mouse(MOUSEEVENTF_WHEEL, data=-dy * WHEEL_DELTA))
        if dx:
            inputs.append(self._mouse(MOUSEEVENTF_HWHEEL, data=dx * WHEEL_DELTA))
        self._send(self._held(modifiers, inputs))
        return "global_input"

    @staticmethod
    def _keyboard(code: int, flags: int, scan: int = 0) -> INPUT:
        if code in EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(INPUT_KEYBOARD, _INPUTS(ki=KEYBDINPUT(code, scan, flags, 0, EVENT_TAG)))

    def key(self, modifiers: tuple[str, ...], key: str) -> str:
        code = VIRTUAL_KEYS[key]
        self._send(
            self._held(modifiers, [self._keyboard(code, 0), self._keyboard(code, KEYEVENTF_KEYUP)])
        )
        return "global_input"

    def type(self, text: str) -> str:
        inputs = []
        for character in text.replace("\r\n", "\n"):
            if character in "\n\t":
                code = VIRTUAL_KEYS["return" if character == "\n" else "tab"]
                inputs += [self._keyboard(code, 0), self._keyboard(code, KEYEVENTF_KEYUP)]
                continue
            encoded = character.encode("utf-16-le")
            for index in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[index : index + 2], "little")
                inputs += [
                    self._keyboard(0, KEYEVENTF_UNICODE, unit),
                    self._keyboard(0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, unit),
                ]
        if inputs:
            self._send(inputs)
        return "global_input"


def _releases(inputs: list[INPUT]) -> list[INPUT]:
    """Key-up and button-up events for what ``inputs`` pressed and did not release."""
    keys: dict[int, None] = {}
    buttons: dict[int, int] = {}
    for item in inputs:
        if item.type == INPUT_KEYBOARD:
            flags = item.value.ki.dwFlags
            if flags & KEYEVENTF_UNICODE:
                continue
            if flags & KEYEVENTF_KEYUP:
                keys.pop(item.value.ki.wVk, None)
            else:
                keys[item.value.ki.wVk] = None
        else:
            flags = item.value.mi.dwFlags
            for down, up in BUTTON_FLAGS.values():
                if flags & down:
                    buttons[down] = up
                if flags & up:
                    buttons.pop(down, None)
    return [
        INPUT(INPUT_MOUSE, _INPUTS(mi=MOUSEINPUT(0, 0, 0, up, 0, EVENT_TAG)))
        for up in buttons.values()
    ] + [
        INPUT(
            INPUT_KEYBOARD,
            _INPUTS(
                ki=KEYBDINPUT(
                    code,
                    0,
                    KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if code in EXTENDED else 0),
                    0,
                    EVENT_TAG,
                )
            ),
        )
        for code in reversed(keys)
    ]


def _integrity(process: Any) -> int:
    token = HANDLE()
    if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_INTEGRITY_LEVEL, buffer, needed, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = SID_AND_ATTRIBUTES.from_buffer(buffer).Sid
        count = advapi32.GetSidSubAuthorityCount(sid)[0]
        return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        kernel32.CloseHandle(token)


def _process(window: int) -> int:
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
    return int(process_id.value)


def _elevated(process_id: int) -> bool:
    target = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not target:
        # Not even limited query rights: a protected or higher-integrity process.
        return True
    try:
        return _integrity(target) > _integrity(kernel32.GetCurrentProcess())
    finally:
        kernel32.CloseHandle(target)


def _title(window: int) -> str:
    buffer = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(window) + 1)
    user32.GetWindowTextW(window, buffer, len(buffer))
    return buffer.value


def _app(window: int) -> str:
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, _process(window))
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).stem
    finally:
        kernel32.CloseHandle(process)


def _cloaked(window: int) -> bool:
    cloaked = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(
        window, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return bool(cloaked.value)


def _draw_cursor(memory: Any, rectangle: tuple[int, int, int, int], width: int) -> None:
    """The real pointer over a picture of ``rectangle`` (physical pixels) scaled to ``width``.

    Per-monitor-v2: the cursor position and the rectangle are both physical pixels. Any
    failure leaves the picture without the pointer; the screenshot itself still answers.
    """
    info = CURSORINFO(ctypes.sizeof(CURSORINFO))
    if not user32.GetCursorInfo(ctypes.byref(info)) or not info.flags & CURSOR_SHOWING:
        return
    icon = ICONINFO()
    if not user32.GetIconInfo(info.hCursor, ctypes.byref(icon)):
        return
    for handle in (icon.hbmMask, icon.hbmColor):
        if handle:
            gdi32.DeleteObject(handle)
    scale = width / rectangle[2]
    user32.DrawIconEx(
        memory,
        round((info.ptScreenPos.x - icon.xHotspot - rectangle[0]) * scale),
        round((info.ptScreenPos.y - icon.yHotspot - rectangle[1]) * scale),
        info.hCursor,
        max(1, round(user32.GetSystemMetrics(SM_CXCURSOR) * scale)),
        max(1, round(user32.GetSystemMetrics(SM_CYCURSOR) * scale)),
        0,
        None,
        DI_NORMAL,
    )


def _stretch(
    source: Any,
    rectangle: tuple[int, int, int, int],
    width: int,
    height: int,
    operation: int,
    cursor: tuple[tuple[int, int, int, int], int] | None = None,
) -> Any:
    """Scale ``rectangle`` of the device context ``source`` to ``width``x``height`` pixels."""
    memory = gdi32.CreateCompatibleDC(source)
    bitmap = gdi32.CreateCompatibleBitmap(source, width, height)
    try:
        previous = gdi32.SelectObject(memory, bitmap)
        gdi32.SetStretchBltMode(memory, HALFTONE)
        gdi32.SetBrushOrgEx(memory, 0, 0, None)
        copied = gdi32.StretchBlt(memory, 0, 0, width, height, source, *rectangle, operation)
        if copied and cursor is not None:
            _draw_cursor(memory, *cursor)
        gdi32.SelectObject(memory, previous)
        if not copied:
            raise ctypes.WinError(ctypes.get_last_error())
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth, header.biHeight = width, -height  # top-down rows
        header.biPlanes, header.biBitCount = 1, 32
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return pixels
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)


def _jpeg(pixels: Any, width: int, height: int, path: Path) -> None:
    image = ctypes.c_void_p()
    status = gdiplus.GdipCreateBitmapFromScan0(
        width, height, width * 4, PIXEL_FORMAT_32BPP_RGB, pixels, ctypes.byref(image)
    )
    if status:
        raise OSError(f"GdipCreateBitmapFromScan0 failed: {status}")
    try:
        quality = wintypes.ULONG(80)
        parameters = EncoderParameters(1)
        parameters.Parameter[0] = EncoderParameter(
            ENCODER_QUALITY, 1, 4, ctypes.cast(ctypes.byref(quality), ctypes.c_void_p)
        )
        status = gdiplus.GdipSaveImageToFile(
            image, str(path), ctypes.byref(JPEG_ENCODER), ctypes.byref(parameters)
        )
        if status:
            raise OSError(f"GdipSaveImageToFile failed: {status}")
    finally:
        gdiplus.GdipDisposeImage(image)


def automation() -> tuple[Any, Any]:
    """UI Automation for the calling thread (multithreaded apartment, as UIA clients use)."""
    import comtypes.client

    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    module = comtypes.client.GetModule("UIAutomationCore.dll")
    return module, comtypes.client.CreateObject(
        module.CUIAutomation, interface=module.IUIAutomation
    )


def _pattern(element: Any, identifier: int, interface: Any) -> Any | None:
    found = element.GetCurrentPattern(identifier)
    return found.QueryInterface(interface) if found else None


def describe(element: Any) -> dict[str, str]:
    return {
        "role": CONTROL_TYPES.get(int(element.CurrentControlType), "custom").replace("_", " "),
        "name": str(element.CurrentName or ""),
    }


def _press(module: Any, element: Any) -> bool:
    """Invoke, toggle or select ``element``: what a plain left click on it would do."""
    for identifier, interface, act in (
        (module.UIA_InvokePatternId, module.IUIAutomationInvokePattern, "Invoke"),
        (module.UIA_TogglePatternId, module.IUIAutomationTogglePattern, "Toggle"),
        (
            module.UIA_SelectionItemPatternId,
            module.IUIAutomationSelectionItemPattern,
            "Select",
        ),
    ):
        pattern = _pattern(element, identifier, interface)
        if pattern is not None:
            getattr(pattern, act)()
            return True
    return False


@dataclass
class WindowsNode:
    """One UI Automation element of a window, as listed by ``elements``."""

    element: Any
    depth: int
    role: str
    name: str
    container: bool
    visible: bool

    def _live(self, read: Callable[[Any, Any], Any]) -> Any:
        import comtypes

        module, _ = automation()
        try:
            return read(module, self.element)
        except comtypes.COMError as error:
            if error.hresult == ELEMENT_NOT_AVAILABLE:
                raise Stale(self.name) from error
            raise OSError(f"UI Automation: {error}") from error

    def bounds(self) -> tuple[float, float, float, float] | None:
        rect = self._live(lambda module, element: element.CurrentBoundingRectangle)
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def press(self) -> bool:
        return bool(self._live(_press))

    def focus(self) -> None:
        self._live(lambda module, element: element.SetFocus())


class WindowsTarget:
    """One top-level window, driven only through UI Automation patterns in its own subtree.

    Nothing here posts messages or input, focuses or raises the window: no ``PostMessage``
    (unreliable for Chromium, Electron, UWP and apps that read the keyboard state), no
    ``SetFocus`` (UI Automation brings the window to the foreground for it). An input with
    no pattern to carry it is refused.
    """

    # No modifiers, click counts, other buttons, keys, moves or drags: no pattern carries them.
    actions = frozenset({"click", "scroll", "type"})

    def __init__(self, window: Window, handle: int, process_id: int) -> None:
        self.window, self.handle, self.process_id = window, handle, process_id
        self.field: Any | None = None

    def collides(self) -> bool:
        if _process(user32.GetForegroundWindow()) == self.process_id:
            return True
        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        under = user32.WindowFromPoint(cursor)
        return bool(under) and _process(user32.GetAncestor(under, GA_ROOT)) == self.process_id

    def elevated(self) -> bool:
        # UIPI blocks UI Automation into a higher integrity level as it blocks SendInput.
        return _elevated(self.process_id)

    def capture(
        self,
        width: int,
        height: int,
        path: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> None:
        outer = wintypes.RECT()
        if not user32.GetWindowRect(self.handle, ctypes.byref(outer)):
            raise ctypes.WinError(ctypes.get_last_error())
        full = (outer.right - outer.left, outer.bottom - outer.top)
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, *full)
        try:
            previous = gdi32.SelectObject(memory, bitmap)
            # The window draws itself, covered or not, GPU-composed content included.
            if not user32.PrintWindow(self.handle, memory, PW_RENDERFULLCONTENT):
                raise ctypes.WinError(ctypes.get_last_error())
            left, top, extent_x, extent_y = region or (0, 0, self.window.width, self.window.height)
            visible = (
                round(self.window.x + left) - outer.left,
                round(self.window.y + top) - outer.top,
                round(extent_x),
                round(extent_y),
            )
            pixels = _stretch(memory, visible, width, height, SRCCOPY)
            gdi32.SelectObject(memory, previous)
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)
        _jpeg(pixels, width, height, path)

    def _ancestors(self, x: float, y: float) -> Iterator[tuple[Any, Any]]:
        """The smallest element of this window's own subtree containing the point, then up.

        Never ``ElementFromPoint``: it answers for whatever window is on top there.
        """
        module, client = automation()
        root = client.ElementFromHandle(self.handle)
        cache = client.CreateCacheRequest()
        cache.AddProperty(module.UIA_BoundingRectanglePropertyId)
        found = root.FindAllBuildCache(
            module.TreeScope_Subtree, client.CreateTrueCondition(), cache
        )
        best, area = None, None
        for index in range(found.Length):
            element = found.GetElement(index)
            bounds = element.CachedBoundingRectangle
            if bounds.left <= x < bounds.right and bounds.top <= y < bounds.bottom:
                size = (bounds.right - bounds.left) * (bounds.bottom - bounds.top)
                if area is None or size <= area:
                    best, area = element, size
        walker = client.RawViewWalker
        node = best
        while node and not client.CompareElements(node, root):
            yield module, node
            node = walker.GetParentElement(node)

    def hit(self, x: float, y: float) -> dict[str, str] | None:
        node = next(self._ancestors(x, y), None)
        return describe(node[1]) if node is not None else None

    def elements(self) -> list[WindowsNode]:
        """The control view of this window's own subtree, read through one cached request."""
        module, client = automation()
        cache = client.CreateCacheRequest()
        for identifier in (
            module.UIA_NamePropertyId,
            module.UIA_ControlTypePropertyId,
            module.UIA_IsOffscreenPropertyId,
        ):
            cache.AddProperty(identifier)
        cache.TreeScope = module.TreeScope_Element | module.TreeScope_Descendants
        cache.TreeFilter = client.ControlViewCondition
        root = client.ElementFromHandle(self.handle).BuildUpdatedCache(cache)
        found: list[WindowsNode] = []
        stack = [(root, 0)]
        while stack:
            element, depth = stack.pop()
            role = CONTROL_TYPES.get(int(element.CachedControlType), "custom")
            found.append(
                WindowsNode(
                    element,
                    depth,
                    role.replace("_", " "),
                    str(element.CachedName or ""),
                    role in CONTAINER_TYPES,
                    not element.CachedIsOffscreen,
                )
            )
            children = element.GetCachedChildren()
            if children:
                stack.extend(
                    (children.GetElement(index), depth + 1)
                    for index in reversed(range(children.Length))
                )
        return found

    def focus_element(self, node: Any) -> None:
        # As for x,y: no keyboard focus moves; the node's value is what ``type`` writes.
        value = node._live(
            lambda module, element: _pattern(
                element, module.UIA_ValuePatternId, module.IUIAutomationValuePattern
            )
        )
        if value is None or value.CurrentIsReadOnly:
            raise Refused(
                "device_not_supported",
                "这个节点不是可写的输入框（UI Automation 没有 Value），后台输入没有执行。",
                cause="no_focused_field",
            )
        self.field = value

    def _deliver(self, run: Callable[[], bool], what: str) -> str:
        import comtypes

        try:
            delivered = run()
        except comtypes.COMError as error:
            raise OSError(f"UI Automation {what}: {error}") from error
        if not delivered:
            raise Refused(
                "device_not_supported",
                f"窗口里这一处没有可在后台{what}的控件（UI Automation），输入没有执行；"
                "需要时省略 window，在前台操作整个屏幕。",
                cause="no_control_at_point",
            )
        return "accessibility"

    def click(
        self, x: float, y: float, button: str, count: int, modifiers: tuple[str, ...]
    ) -> str:
        def expand_or_collapse(pattern: Any, collapsed: int) -> None:
            if pattern.CurrentExpandCollapseState == collapsed:
                pattern.Expand()
            else:
                pattern.Collapse()

        def run() -> bool:
            for module, node in self._ancestors(x, y):
                collapsed = module.ExpandCollapseState_Collapsed
                for identifier, interface, act in (
                    (
                        module.UIA_InvokePatternId,
                        module.IUIAutomationInvokePattern,
                        lambda pattern: pattern.Invoke(),
                    ),
                    (
                        module.UIA_TogglePatternId,
                        module.IUIAutomationTogglePattern,
                        lambda pattern: pattern.Toggle(),
                    ),
                    (
                        module.UIA_SelectionItemPatternId,
                        module.IUIAutomationSelectionItemPattern,
                        lambda pattern: pattern.Select(),
                    ),
                    (
                        module.UIA_ExpandCollapsePatternId,
                        module.IUIAutomationExpandCollapsePattern,
                        lambda pattern, collapsed=collapsed: expand_or_collapse(
                            pattern, collapsed
                        ),
                    ),
                ):
                    pattern = _pattern(node, identifier, interface)
                    if pattern is not None:
                        act(pattern)
                        return True
            return False

        return self._deliver(run, "点击")

    def scroll(self, x: float, y: float, dx: int, dy: int, modifiers: tuple[str, ...]) -> str:
        def run() -> bool:
            for module, node in self._ancestors(x, y):
                pattern = _pattern(
                    node, module.UIA_ScrollPatternId, module.IUIAutomationScrollPattern
                )
                if pattern is None:
                    continue
                none = module.ScrollAmount_NoAmount
                forward = module.ScrollAmount_SmallIncrement
                back = module.ScrollAmount_SmallDecrement
                for _ in range(abs(dy)):
                    pattern.Scroll(none, forward if dy > 0 else back)
                for _ in range(abs(dx)):
                    pattern.Scroll(forward if dx > 0 else back, none)
                return True
            return False

        return self._deliver(run, "滚动")

    def focus(self, x: float, y: float) -> str:
        # No keyboard focus moves (UI Automation would raise the window for it): the field
        # at x,y is remembered and ``type`` writes its value.
        def run() -> bool:
            for module, node in self._ancestors(x, y):
                value = _pattern(
                    node, module.UIA_ValuePatternId, module.IUIAutomationValuePattern
                )
                if value is not None and not value.CurrentIsReadOnly:
                    self.field = value
                    return True
            return False

        return self._deliver(run, "输入")

    def type(self, text: str) -> str:
        field = self.field
        if field is None:
            raise Refused(
                "device_not_supported",
                "Windows 上按窗口输入要给 x,y 或 element 指向输入框（文字接在原内容之后）；"
                "输入没有执行。",
                cause="no_focused_field",
            )

        def run() -> bool:
            field.SetValue(field.CurrentValue + text)
            return True

        return self._deliver(run, "输入")

    # Delivered only by moving the real cursor or posting window messages: never here.
    def move(self, x: float, y: float) -> str:
        raise Refused(
            "device_not_supported", "Windows 上不能在后台移动指针。", cause="no_background_route"
        )

    def drag(
        self, x: float, y: float, x2: float, y2: float, modifiers: tuple[str, ...]
    ) -> str:
        raise Refused(
            "device_not_supported", "Windows 上不能在后台拖拽。", cause="no_background_route"
        )

    def key(self, modifiers: tuple[str, ...], key: str) -> str:
        raise Refused(
            "device_not_supported", "Windows 上不能在后台按键。", cause="no_background_route"
        )
