"""Computer-use phase 3b spike (Windows): background window capture and UIA actions.

Run on the owner's Windows PC, from a normal (not elevated) terminal, with nothing typed
or clicked while it runs (about one minute):

    uv run --no-project --python 3.12 --with comtypes python computer_3b_windows.py [out-dir]

It only touches windows it opens itself: a Notepad on a temporary file, a Calculator and
a Chrome or Edge window on a private temporary profile showing a local test page, plus an
opaque "occluder" window it draws over them. Notepad and Calculator are skipped when one is
already open (Windows 11 would reuse the owner's window). It never clicks or types into any
other window, and closes what it opened. Results: ``<out-dir>/result.json`` and BMP images.

Questions (see docs/operations/computer-3b-spike-20261110.md):
  W1 PrintWindow(PW_RENDERFULLCONTENT) vs PrintWindow(0) vs screen BitBlt for a visible,
     occluded, off-screen and minimized window, classic Win32 and GPU/DirectComposition
     (Chromium, WinUI). Black-image fraction per image.
  W2 Window-relative pixel <-> screen mapping: DPI of the window, DWM extended frame
     bounds vs GetWindowRect, client origin.
  W3 UIA on a background (occluded, not foreground) window: element at a window-relative
     point found by walking that window's own subtree (not IUIAutomation::ElementFromPoint,
     which hit-tests the topmost window), InvokePattern / ValuePattern / ScrollPattern.
     For every action: cursor position, foreground window, the target's z-order and
     GetLastInputInfo before and after, and whether the effect is visible via UIA.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    raise SystemExit("Windows only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2, as the Connector

PW_CLIENTONLY, PW_RENDERFULLCONTENT = 1, 2
SW_SHOWMINNOACTIVE, SW_SHOWNOACTIVATE = 7, 4
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
GW_HWNDPREV = 3
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "spike3b-windows-out").resolve()
OUT.mkdir(parents=True, exist_ok=True)

PAGE = """<!doctype html><title>SPIKE P0 V= S=0</title>
<body style="font:14px sans-serif;margin:8px">
<button id=press style="font-size:16px">Press me</button>
<input id=field aria-label=field><div id=list aria-label=list
style="height:150px;overflow:auto;border:1px solid #999">ROWS</div>
<script>let p=0;const l=document.getElementById('list'),f=document.getElementById('field');
function u(){document.title='SPIKE P'+p+' V='+f.value+' S='+l.scrollTop+
' F'+(document.hasFocus()?'+':'-')}
document.getElementById('press').onclick=()=>{p++;u()};f.oninput=u;l.onscroll=u;
onfocus=u;onblur=u;u()</script>""".replace(
    "ROWS", "".join(f"<div>row {i}</div>" for i in range(200))
)


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


def state(hwnd: int | None = None) -> dict[str, Any]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(info))
    foreground = user32.GetForegroundWindow()
    return {
        "t": time.time(),
        "cursor": [point.x, point.y],
        "foreground": foreground,
        "foreground_title": title(foreground),
        "last_input_tick": info.dwTime,
        "z": z_index(hwnd) if hwnd else None,
    }


def title(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def z_index(hwnd: int) -> int:
    """Number of visible top-level windows above ``hwnd`` (0 = topmost)."""
    count, current = 0, user32.GetWindow(hwnd, GW_HWNDPREV)
    while current:
        if user32.IsWindowVisible(current):
            count += 1
        current = user32.GetWindow(current, GW_HWNDPREV)
    return count


def rect(hwnd: int) -> dict[str, Any]:
    window = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(window))
    frame = wintypes.RECT()
    dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(frame), ctypes.sizeof(frame)
    )
    client = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    cloaked = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return {
        "window": [window.left, window.top, window.right, window.bottom],
        "dwm_frame": [frame.left, frame.top, frame.right, frame.bottom],
        "client_size": [client.right, client.bottom],
        "client_origin": [origin.x, origin.y],
        "dpi": user32.GetDpiForWindow(hwnd),
        "minimized": bool(user32.IsIconic(hwnd)),
        "cloaked": cloaked.value,
    }


def save_bmp(path: Path, width: int, height: int, pixels: bytes) -> float:
    """Write a 32-bit top-down BMP; return the fraction of (near-)black pixels."""
    header = struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    path.write_bytes(header + info + pixels)
    step = max(1, (width * height) // 4000)
    sample = [pixels[i * 4 : i * 4 + 3] for i in range(0, width * height, step)]
    return round(sum(1 for p in sample if max(p) < 8) / len(sample), 3)


def capture(hwnd: int, name: str, method: str) -> dict[str, Any]:
    window = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(window))
    width, height = window.right - window.left, window.bottom - window.top
    if width <= 0 or height <= 0:
        return {"method": method, "error": f"empty rect {width}x{height}"}
    screen = user32.GetDC(0)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    gdi32.SelectObject(memory, bitmap)
    started = time.perf_counter()
    if method == "bitblt":
        ok = bool(
            gdi32.BitBlt(memory, 0, 0, width, height, screen, window.left, window.top, 0x00CC0020)
        )
    else:
        flags = {"printwindow0": 0, "printwindow_full": PW_RENDERFULLCONTENT}[method]
        ok = bool(user32.PrintWindow(hwnd, memory, flags))
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(info), 0)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory)
    user32.ReleaseDC(0, screen)
    path = OUT / f"{name}-{method}.bmp"
    black = save_bmp(path, width, height, buffer.raw)
    return {
        "method": method,
        "ok": ok,
        "ms": elapsed,
        "size": [width, height],
        "black": black,
        "path": str(path),
    }


def captures(hwnd: int, name: str) -> dict[str, Any]:
    return {
        "rect": rect(hwnd),
        "images": [capture(hwnd, name, m) for m in ("printwindow_full", "printwindow0", "bitblt")],
    }


def windows_titled(needle: str) -> list[int]:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd) and needle in title(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return found


def find_window(pid_or_title: str, timeout: float = 15.0) -> int:
    """First visible top-level window whose title contains the text."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = windows_titled(pid_or_title)
        if found:
            return found[0]
        time.sleep(0.3)
    raise TimeoutError(f"no window titled *{pid_or_title}*")


def occluder(rect_: list[int]) -> subprocess.Popen[bytes]:
    """An opaque, topmost, non-activating window drawn over ``rect_`` by a child process."""
    left, top, right, bottom = rect_
    code = (
        "import tkinter as t;r=t.Tk();r.title('Yuki 3b spike occluder');"
        f"r.geometry('{right - left + 40}x{bottom - top + 40}+{left - 20}+{top - 20}');"
        "r.attributes('-topmost',1);r.configure(bg='gray20');"
        "t.Label(r,text='Yuki 3b spike occluder',fg='white',bg='gray20').pack(expand=1);"
        "r.after(60000,r.destroy);r.mainloop()"
    )
    process = subprocess.Popen([sys.executable, "-c", code])
    time.sleep(1.5)
    return process


# ---- UI Automation -----------------------------------------------------------------


def uia() -> Any:
    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as client

    automation = comtypes.client.CreateObject(client.CUIAutomation, interface=client.IUIAutomation)
    return automation, client


def walk(automation: Any, root: Any, limit: int = 4000) -> list[Any]:
    walker = automation.RawViewWalker
    found, stack = [], [root]
    while stack and len(found) < limit:
        element = stack.pop()
        found.append(element)
        child = walker.GetFirstChildElement(element)
        while child:
            stack.append(child)
            child = walker.GetNextSiblingElement(child)
    return found


def element_at(automation: Any, root: Any, x: int, y: int) -> Any:
    """Smallest element of ``root``'s own subtree whose bounds contain the screen point."""
    best, best_area = None, None
    for element in walk(automation, root):
        r = element.CurrentBoundingRectangle
        if r.left <= x < r.right and r.top <= y < r.bottom:
            area = (r.right - r.left) * (r.bottom - r.top)
            if best_area is None or area <= best_area:
                best, best_area = element, area
    return best


def describe(element: Any) -> dict[str, Any]:
    r = element.CurrentBoundingRectangle
    return {
        "name": element.CurrentName,
        "control_type": element.CurrentControlType,
        "class": element.CurrentClassName,
        "framework": element.CurrentFrameworkId,
        "bounds": [r.left, r.top, r.right, r.bottom],
    }


def pattern(element: Any, client: Any, name: str) -> Any:
    ids = {
        "invoke": (client.UIA_InvokePatternId, client.IUIAutomationInvokePattern),
        "value": (client.UIA_ValuePatternId, client.IUIAutomationValuePattern),
        "scroll": (client.UIA_ScrollPatternId, client.IUIAutomationScrollPattern),
    }
    pattern_id, interface = ids[name]
    raw = element.GetCurrentPattern(pattern_id)
    return raw.QueryInterface(interface) if raw else None


def act(hwnd: int, label: str, action: Any) -> dict[str, Any]:
    before = state(hwnd)
    started = time.perf_counter()
    try:
        detail = action()
        error = None
    except Exception as failure:
        detail, error = None, repr(failure)
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    time.sleep(0.5)
    after = state(hwnd)
    return {
        "action": label,
        "ms": elapsed,
        "error": error,
        "detail": detail,
        "title_after": title(hwnd),
        "cursor_moved": before["cursor"] != after["cursor"],
        "foreground_changed": before["foreground"] != after["foreground"],
        "z_before": before["z"],
        "z_after": after["z"],
        "last_input_advanced": before["last_input_tick"] != after["last_input_tick"],
        "before": before,
        "after": after,
    }


def uia_actions(hwnd: int, kind: str) -> list[dict[str, Any]]:
    automation, client = uia()
    root = automation.ElementFromHandle(hwnd)
    results: list[dict[str, Any]] = []
    elements = walk(automation, root)
    by_name = {e.CurrentName: e for e in elements if e.CurrentName}
    results.append({"subtree_size": len(elements), "names": sorted(by_name)[:80]})

    def hit(name: str) -> dict[str, Any]:
        element = by_name[name]
        r = element.CurrentBoundingRectangle
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        own = element_at(automation, root, cx, cy)
        system = automation.ElementFromPoint(client.tagPOINT(cx, cy))
        return {
            "point": [cx, cy],
            "window_scoped": describe(own) if own else None,
            "system_hit_test": describe(system) if system else None,
        }

    if kind == "calc":
        target = next((n for n in ("One", "1", "Eins", "一") if n in by_name), None)
        if target:
            results.append({"hit_test": hit(target)})
            results.append(
                act(
                    hwnd,
                    f"invoke {target}",
                    lambda: pattern(by_name[target], client, "invoke").Invoke(),
                )
            )
    elif kind == "notepad":
        editor = next(
            (
                e
                for e in elements
                if e.CurrentControlType
                in (client.UIA_DocumentControlTypeId, client.UIA_EditControlTypeId)
            ),
            None,
        )
        if editor is not None:
            value = pattern(editor, client, "value")
            results.append(
                act(
                    hwnd,
                    "value set",
                    lambda: value.SetValue("yuki 3b spike") if value else "no ValuePattern",
                )
            )
            results.append({"value_after": value.CurrentValue if value else None})
    elif kind == "chrome":
        if "Press me" in by_name:
            results.append({"hit_test": hit("Press me")})
            results.append(
                act(
                    hwnd,
                    "invoke Press me",
                    lambda: pattern(by_name["Press me"], client, "invoke").Invoke(),
                )
            )
        if "field" in by_name:
            results.append(
                act(
                    hwnd,
                    "value field",
                    lambda: pattern(by_name["field"], client, "value").SetValue("yuki"),
                )
            )
        if "list" in by_name:
            scroll = pattern(by_name["list"], client, "scroll")
            results.append(
                act(
                    hwnd,
                    "scroll list",
                    lambda: scroll.SetScrollPercent(-1, 50.0) if scroll else "no ScrollPattern",
                )
            )
        results.append({"title_after_all": title(hwnd)})
    return results


def chromium() -> str | None:
    for candidate in (
        shutil.which("chrome"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def scenario(hwnd: int, name: str, kind: str) -> dict[str, Any]:
    """Visible → occluded (+ UIA actions) → off-screen → minimized, then restored."""
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    time.sleep(0.8)
    report: dict[str, Any] = {"visible": captures(hwnd, f"{name}-visible")}
    cover = occluder(rect(hwnd)["window"])
    try:
        report["occluded"] = captures(hwnd, f"{name}-occluded")
        report["uia_occluded"] = uia_actions(hwnd, kind)
        report["occluded_after_actions"] = captures(hwnd, f"{name}-occluded-after")
    finally:
        cover.kill()
    left, top = rect(hwnd)["window"][:2]
    user32.SetWindowPos(hwnd, 0, -4000, top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    time.sleep(0.8)
    report["offscreen"] = captures(hwnd, f"{name}-offscreen")
    user32.SetWindowPos(hwnd, 0, left, top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
    time.sleep(0.8)
    report["minimized"] = captures(hwnd, f"{name}-minimized")
    report["uia_minimized"] = uia_actions(hwnd, kind)
    return report


def main() -> None:
    console = user32.GetForegroundWindow()
    result: dict[str, Any] = {"platform": platform.platform(), "start": state()}
    opened: list[subprocess.Popen[bytes]] = []
    profile = Path(tempfile.mkdtemp(prefix="yuki-3b-chrome-"))
    page = profile / "page.html"
    page.write_text(PAGE, encoding="utf-8")
    try:
        targets: list[tuple[str, str, list[str], str]] = [
            (
                "notepad",
                "notepad",
                ["notepad.exe", str(profile / "yuki-3b-spike.txt")],
                "yuki-3b-spike",
            ),
            ("calc", "calc", ["calc.exe"], "Calculator"),
        ]
        browser = chromium()
        if browser:
            targets.append(
                (
                    "chrome",
                    "chrome",
                    [
                        browser,
                        f"--user-data-dir={profile}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--force-renderer-accessibility",
                        "--window-size=520,420",
                        page.as_uri(),
                    ],
                    "SPIKE",
                )
            )
        (profile / "yuki-3b-spike.txt").write_text("", encoding="utf-8")
        for name, kind, command, needle in targets:
            if name != "chrome" and windows_titled(needle if name == "calc" else "Notepad"):
                result[name] = {"skipped": "already open; would reuse the owner's window"}
                continue
            before = state()
            opened.append(subprocess.Popen(command))
            hwnd = find_window(needle)
            launched = state(hwnd)
            # Hand the foreground back so every later measurement is on a background window.
            time.sleep(1.0)
            user32.SetForegroundWindow(console)
            time.sleep(0.5)
            result[f"{name}_launch"] = {
                "before": before,
                "after_launch": launched,
                "after_handback": state(hwnd),
                "hwnd": hwnd,
                "title": title(hwnd),
            }
            try:
                result[name] = scenario(hwnd, name, kind)
            except Exception as failure:
                result[name] = {"error": repr(failure)}
            # Notepad and Calculator start through a stub that exits at once; end the process
            # that owns the window (only ever one this script started: see the skip above).
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            handle = kernel32.OpenProcess(0x0001, False, owner.value)  # PROCESS_TERMINATE
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
    finally:
        for process in opened:
            if process.poll() is None:
                process.kill()
        result["end"] = state()
        (OUT / "result.json").write_text(json.dumps(result, indent=2, default=str))
        print(f"wrote {OUT / 'result.json'}")


if __name__ == "__main__":
    main()
