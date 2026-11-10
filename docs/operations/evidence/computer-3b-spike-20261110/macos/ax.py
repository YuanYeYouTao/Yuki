"""AX experiments on one window of one app, without raising it or touching the cursor.

Usage: ax.py <name> <pid> <wid> <op> [args]
  op tree                      dump the window's subtree (role, title, frame, actions)
  op hit <wx> <wy>             element at a window-relative point: own-subtree walk vs
                               AXUIElementCopyElementAtPosition(app) vs system-wide
  op press <wx> <wy>           AXPress the element at the window-relative point
  op focus <wx> <wy>           set AXFocused on the text field at the point
  op setvalue <wx> <wy> <text> set AXValue of the text field at the point
  op scroll <wx> <wy>          scroll the scroll area at the point (scroll bar AXValue,
                               else AXScrollToVisible on its last row)
Window-relative points are pixels of the window image (scale from the SCK filter = 1 here).
"""

import json
import sys
import time

import objc
import Quartz
from ApplicationServices import (
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from probe import dump, snapshot, windows

Quartz.CGMainDisplayID()
# Private: CGWindowID of an AX window (used by Cua, Hammerspoon, yabai, ...).
_bundle = objc.loadBundle(
    "HIServices",
    {},
    "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework",
)
_funcs = {}
objc.loadBundleFunctions(_bundle, _funcs, [("_AXUIElementGetWindow", b"i@o^I")])
ax_window_id = _funcs.get("_AXUIElementGetWindow")

name, pid, wid, op = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
args = sys.argv[5:]


def attr(element, key):
    err, value = AXUIElementCopyAttributeValue(element, key, None)
    return value if err == 0 else None


def frame(element):
    pos, size = attr(element, "AXPosition"), attr(element, "AXSize")
    if pos is None or size is None:
        return None
    _, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    _, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    return (p.x, p.y, s.width, s.height)


def describe(element):
    if element is None:
        return None
    _, actions = AXUIElementCopyActionNames(element, None)
    value = attr(element, "AXValue")
    return {
        "role": attr(element, "AXRole"),
        "subrole": attr(element, "AXSubrole"),
        "title": attr(element, "AXTitle") or attr(element, "AXDescription"),
        "value": str(value)[:60] if value is not None else None,
        "frame": frame(element),
        "actions": list(actions or []),
    }


app = AXUIElementCreateApplication(pid)
manual = AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)  # Electron
enhanced = None
if manual != 0:
    enhanced = AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", True)  # Chrome
t_enable = time.time()


def target_window():
    for _ in range(20):
        for window in attr(app, "AXWindows") or []:
            if ax_window_id:
                err, number = ax_window_id(window, None)
                if err == 0 and number == wid:
                    return window
        time.sleep(0.2)
    return None


def walk(root, limit=5000):
    out, stack = [], [root]
    while stack and len(out) < limit:
        element = stack.pop()
        out.append(element)
        stack.extend(attr(element, "AXChildren") or [])
    return out


def element_at(root, gx, gy):
    best, area = None, None
    for element in walk(root):
        f = frame(element)
        if f and f[0] <= gx < f[0] + f[2] and f[1] <= gy < f[1] + f[3]:
            a = f[2] * f[3]
            if area is None or a <= area:
                best, area = element, a
    return best


window = target_window()
bounds = next(w["bounds"] for w in windows(False) if w["id"] == wid)
result = {
    "name": name,
    "op": op,
    "pid": pid,
    "wid": wid,
    "bounds": bounds,
    "AXManualAccessibility": manual,
    "AXEnhancedUserInterface": enhanced,
    "window_found_by_id": window is not None,
}
if window is None:
    print(json.dumps(result))
    sys.exit(1)


def to_global(wx, wy):
    return bounds[0] + wx, bounds[1] + wy


if op == "tree":
    # Chromium builds the web tree asynchronously after accessibility is switched on.
    time.sleep(1.0)
    elements = walk(window)
    result["count"] = len(elements)
    result["tree"] = [describe(e) for e in elements[:200]]
elif op == "hit":
    gx, gy = to_global(float(args[0]), float(args[1]))
    t0 = time.perf_counter()
    own = element_at(window, gx, gy)
    result["own_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    result["own_subtree"] = describe(own)
    err, at_app = AXUIElementCopyElementAtPosition(app, gx, gy, None)
    result["app_hit_test"] = {"err": err, "element": describe(at_app) if err == 0 else None}
    err, at_sys = AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), gx, gy, None)
    result["system_hit_test"] = {"err": err, "element": describe(at_sys) if err == 0 else None}
else:
    gx, gy = to_global(float(args[0]), float(args[1]))
    element = element_at(window, gx, gy)
    result["element"] = describe(element)
    before = snapshot(wid)
    t0 = time.perf_counter()
    if op in ("focus", "setvalue"):
        # WebKit exposes the input's inner editor as a smaller group; climb to the field.
        while element is not None and attr(element, "AXRole") not in (
            "AXTextField",
            "AXTextArea",
            "AXComboBox",
            "AXSearchField",
        ):
            element = attr(element, "AXParent")
        result["element"] = describe(element)
    if op == "press":
        result["err"] = AXUIElementPerformAction(element, "AXPress")
    elif op == "focus":
        result["err"] = AXUIElementSetAttributeValue(element, "AXFocused", True)
    elif op == "setvalue":
        result["err"] = AXUIElementSetAttributeValue(element, "AXValue", args[2])
    elif op == "scroll":
        node, area = element, None
        while node is not None:
            if attr(node, "AXRole") == "AXScrollArea":
                area = node
                break
            node = attr(node, "AXParent")
        bar = attr(area, "AXVerticalScrollBar") if area is not None else None
        if bar is not None:
            result["via"] = "AXVerticalScrollBar.AXValue=0.5"
            result["err"] = AXUIElementSetAttributeValue(bar, "AXValue", 0.5)
        else:
            rows = [
                e
                for e in walk(area or element)
                if attr(e, "AXRole") in ("AXStaticText", "AXRow", "AXGroup")
            ]
            result["via"] = f"AXScrollToVisible on last of {len(rows)}"
            result["err"] = (
                AXUIElementPerformAction(rows[-1], "AXScrollToVisible") if rows else None
            )
    result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    time.sleep(0.5)
    after = snapshot(wid)
    result["after_element"] = describe(element)
    result.update(
        before=before,
        after=after,
        cursor_moved=before["cursor"] != after["cursor"],
        front_changed=before["front"] != after["front"],
        z_changed=before["z"] != after["z"],
    )
dump(f"ax-{name}", result)
short = {
    k: result.get(k)
    for k in ("name", "err", "ms", "via", "cursor_moved", "front_changed", "z_changed")
}
print(
    json.dumps(short),
    "element:",
    json.dumps(result.get("element") or result.get("own_subtree"))[:200],
)
if op == "hit":
    print("app:", json.dumps(result["app_hit_test"])[:200])
    print("sys:", json.dumps(result["system_hit_test"])[:200])
if "after" in result:
    print(
        "front",
        result["after"]["front"],
        "z",
        result["before"]["z"],
        "->",
        result["after"]["z"],
        "idle",
        result["after"]["idle"],
    )
