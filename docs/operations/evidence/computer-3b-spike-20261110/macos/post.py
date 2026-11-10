"""CGEventPostToPid experiments. Usage:
post.py <name> <pid> <wid> <kind> <x> <y> [--winfields] [text]
kind: click | rclick | key | type | scroll
x,y are global points (CG, top-left origin). --winfields sets the window-under-pointer
fields (kCGMouseEventWindowUnderMousePointer[ThatCanHandleThisEvent]) to <wid>.
Records cursor, frontmost app, the window's z-order and Combined/HID idle-by-type
before and after; the result is out/post-<name>.json.
"""

import sys
import time

import objc
import Quartz
from probe import dump, idle, snapshot

TAG = 0x59554B49
name, pid, wid, kind = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
x, y = float(sys.argv[5]), float(sys.argv[6])
winfields = "--winfields" in sys.argv
stamped = "--stamped" in sys.argv  # Cua's recipe (cmux #36068 stamped-pid, cua mouse.rs)
via_sl = "--sl" in sys.argv  # SkyLight SLEventPostToPid instead of CGEventPostToPid
_fn = {}
objc.loadBundleFunctions(
    objc.loadBundle("CoreGraphics", {}, "/System/Library/Frameworks/CoreGraphics.framework"),
    _fn,
    [("CGEventSetWindowLocation", b"v@{CGPoint=dd}")],
)
objc.loadBundleFunctions(
    objc.loadBundle("SkyLight", {}, "/System/Library/PrivateFrameworks/SkyLight.framework"),
    _fn,
    [("SLEventPostToPid", b"vi@")],
)
Quartz.CGMainDisplayID()
_b = next(w for w in __import__("probe").windows(False) if w["id"] == wid)["bounds"]
LOCAL = (x - _b[0], y - _b[1])
extra = [a for a in sys.argv[7:] if not a.startswith("--")]
src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
F_UNDER = 91  # kCGMouseEventWindowUnderMousePointer
F_CAN = 92  # kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent


def post(event):
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, TAG)
    if winfields or stamped:
        Quartz.CGEventSetIntegerValueField(event, F_UNDER, wid)
        Quartz.CGEventSetIntegerValueField(event, F_CAN, wid)
    if stamped:
        Quartz.CGEventSetIntegerValueField(event, 51, wid)  # window number
        Quartz.CGEventSetIntegerValueField(event, 40, pid)  # target pid
        if Quartz.CGEventGetType(event) not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
            Quartz.CGEventSetIntegerValueField(event, 7, 3)  # subtype
            Quartz.CGEventSetIntegerValueField(event, 58, 1)  # click group
            _fn["CGEventSetWindowLocation"](event, LOCAL)  # window-local on macOS 27
    if via_sl:
        _fn["SLEventPostToPid"](pid, event)
    else:
        Quartz.CGEventPostToPid(pid, event)


def mouse(t, button=Quartz.kCGMouseButtonLeft, clicks=1):
    e = Quartz.CGEventCreateMouseEvent(src, t, (x, y), button)
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventClickState, clicks)
    post(e)


before = snapshot(wid)
before_hid = idle(Quartz.kCGEventSourceStateHIDSystemState)
t0 = time.perf_counter()
if stamped and kind in ("click", "rclick", "scroll"):
    mouse(Quartz.kCGEventMouseMoved)  # primer: cua mouse.rs "otherwise silently ignored"
    time.sleep(0.03)
if kind == "click":
    mouse(Quartz.kCGEventLeftMouseDown)
    time.sleep(0.03)
    mouse(Quartz.kCGEventLeftMouseUp)
elif kind == "rclick":
    mouse(Quartz.kCGEventRightMouseDown, Quartz.kCGMouseButtonRight)
    mouse(Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight)
elif kind == "key":
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(src, 0x00, down)  # 'a'
        post(e)
elif kind == "type":
    for ch in extra[0] if extra else "hi":
        for down in (True, False):
            e = Quartz.CGEventCreateKeyboardEvent(src, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(e, 1, ch)
            post(e)
            time.sleep(0.005)
elif kind == "scroll":
    e = Quartz.CGEventCreateScrollWheelEvent2(src, Quartz.kCGScrollEventUnitLine, 1, -5, 0, 0)
    Quartz.CGEventSetLocation(e, (x, y))
    post(e)
elapsed = round((time.perf_counter() - t0) * 1000, 1)
time.sleep(0.4)
after = snapshot(wid)
after_hid = idle(Quartz.kCGEventSourceStateHIDSystemState)
result = {
    "name": name,
    "kind": kind,
    "pid": pid,
    "wid": wid,
    "point": [x, y],
    "winfields": winfields,
    "stamped": stamped,
    "sl": via_sl,
    "post_ms": elapsed,
    "preflight_post": bool(Quartz.CGPreflightPostEventAccess()),
    "before": before,
    "after": after,
    "hid_before": before_hid,
    "hid_after": after_hid,
    "cursor_moved": before["cursor"] != after["cursor"],
    "front_changed": before["front"] != after["front"],
    "z_changed": before["z"] != after["z"],
}
dump(f"post-{name}", result)
print(
    {
        k: result[k]
        for k in ("name", "post_ms", "preflight_post", "cursor_moved", "front_changed", "z_changed")
    },
    "z",
    before["z"],
    "->",
    after["z"],
    "front",
    after["front"],
)
print("combined idle after", after["idle"])
