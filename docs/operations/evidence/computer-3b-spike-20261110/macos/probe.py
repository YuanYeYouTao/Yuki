"""Measurement helpers for the 3b spike (no NSApplication, no run loop)."""

import json
import subprocess
import threading
import time
from pathlib import Path

import Quartz

HERE = Path(__file__).parent
OUT = HERE / "out"
IMG = HERE / "img"

DELIBERATE = {
    "keyDown": Quartz.kCGEventKeyDown,
    "leftDown": Quartz.kCGEventLeftMouseDown,
    "rightDown": Quartz.kCGEventRightMouseDown,
    "otherDown": Quartz.kCGEventOtherMouseDown,
    "leftDrag": Quartz.kCGEventLeftMouseDragged,
    "scroll": Quartz.kCGEventScrollWheel,
    "moved": Quartz.kCGEventMouseMoved,
}


def cursor():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return round(p.x, 2), round(p.y, 2)


def front():
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(["lsappinfo", "info", asn], capture_output=True, text=True).stdout
    m = __import__("re").match(r'\s*"([^"]*)"', info)
    return m.group(1) if m else info.strip()[:40]


def idle(state=Quartz.kCGEventSourceStateCombinedSessionState):
    return {
        name: round(float(Quartz.CGEventSourceSecondsSinceLastEventType(state, kind)), 3)
        for name, kind in DELIBERATE.items()
    }


def windows(on_screen=True):
    option = (
        Quartz.kCGWindowListOptionOnScreenOnly if on_screen else Quartz.kCGWindowListOptionAll
    ) | Quartz.kCGWindowListExcludeDesktopElements
    rows = []
    for w in Quartz.CGWindowListCopyWindowInfo(option, Quartz.kCGNullWindowID) or []:
        b = w.get("kCGWindowBounds", {})
        rows.append(
            {
                "id": int(w["kCGWindowNumber"]),
                "pid": int(w["kCGWindowOwnerPID"]),
                "app": w.get("kCGWindowOwnerName"),
                "title": w.get("kCGWindowName"),
                "layer": int(w.get("kCGWindowLayer", 0)),
                "onscreen": bool(w.get("kCGWindowIsOnscreen", False)),
                "bounds": [b.get("X"), b.get("Y"), b.get("Width"), b.get("Height")],
            }
        )
    return rows


def find(title_part, on_screen=False):
    return [w for w in windows(on_screen) if w["title"] and title_part in w["title"]]


def z_index(wid):
    """Position of the window among on-screen layer-0 windows (0 = topmost)."""
    ids = [w["id"] for w in windows(True) if w["layer"] == 0]
    return ids.index(wid) if wid in ids else None


def snapshot(wid=None):
    return {
        "t": time.time(),
        "cursor": cursor(),
        "front": front(),
        "z": z_index(wid) if wid else None,
        "idle": idle(),
    }


def wait(start, seconds=10.0):
    done = threading.Event()
    box = []

    def finished(*values):
        box.extend(values)
        done.set()

    start(finished)
    if not done.wait(seconds):
        raise TimeoutError("no completion")
    return box


def save_png(image, path):
    url = Quartz.CFURLCreateWithFileSystemPath(None, str(path), Quartz.kCFURLPOSIXPathStyle, False)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, image, None)
    assert Quartz.CGImageDestinationFinalize(dest)


def mean_luma(image):
    """Average brightness 0..255 and fraction of non-black pixels (black-image detector)."""
    w, h = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
    raw = bytes(data)
    bpr = Quartz.CGImageGetBytesPerRow(image)
    total = nonblack = 0
    n = 0
    for y in range(0, h, max(1, h // 60)):
        row = raw[y * bpr : y * bpr + w * 4]
        for x in range(0, w, max(1, w // 60)):
            b, g, r = row[x * 4], row[x * 4 + 1], row[x * 4 + 2]
            v = (r + g + b) / 3
            total += v
            nonblack += v > 8
            n += 1
    return round(total / n, 1), round(nonblack / n, 3)


def dump(name, obj):
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(obj, indent=2, default=str))
    return path
