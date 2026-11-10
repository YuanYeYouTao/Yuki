"""Print the AXValue of the first vertical scroll bar and text field of window <pid> <wid>."""

import sys

import ApplicationServices as A
import objc

b = objc.loadBundle(
    "HIServices",
    {},
    "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework",
)
f = {}
objc.loadBundleFunctions(b, f, [("_AXUIElementGetWindow", b"i@o^I")])
pid, wid = int(sys.argv[1]), int(sys.argv[2])


def at(e, k):
    err, v = A.AXUIElementCopyAttributeValue(e, k, None)
    return v if err == 0 else None


app = A.AXUIElementCreateApplication(pid)
win = next(w for w in at(app, "AXWindows") if f["_AXUIElementGetWindow"](w, None)[1] == wid)
stack, out = [win], {}
while stack:
    e = stack.pop()
    r = at(e, "AXRole")
    if r == "AXScrollBar" and "scroll" not in out:
        out["scroll"] = round(float(at(e, "AXValue")), 4)
    if r == "AXTextField":
        out["field"] = at(e, "AXValue")
    stack.extend(at(e, "AXChildren") or [])
print(out)
