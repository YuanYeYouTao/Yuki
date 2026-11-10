"""AXScrollToVisible on the static text <text> inside window <pid> <wid>.

Prints the AX error, cursor movement, frontmost app and z-order before/after.
"""

import json
import sys
import time

import ApplicationServices as A
import objc
from probe import snapshot

b = objc.loadBundle(
    "HIServices",
    {},
    "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework",
)
f = {}
objc.loadBundleFunctions(b, f, [("_AXUIElementGetWindow", b"i@o^I")])
pid, wid, text = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]


def at(e, k):
    err, v = A.AXUIElementCopyAttributeValue(e, k, None)
    return v if err == 0 else None


app = A.AXUIElementCreateApplication(pid)
win = next(w for w in at(app, "AXWindows") if f["_AXUIElementGetWindow"](w, None)[1] == wid)
stack, hit = [win], None
while stack and hit is None:
    e = stack.pop()
    if text in str(at(e, "AXValue") or "") + str(at(e, "AXTitle") or "") + str(
        at(e, "AXDescription") or ""
    ):
        hit = e
    stack.extend(at(e, "AXChildren") or [])
before = snapshot(wid)
err = A.AXUIElementPerformAction(hit, "AXScrollToVisible") if hit is not None else "not found"
time.sleep(0.5)
after = snapshot(wid)
print(
    json.dumps(
        {
            "err": err,
            "role": at(hit, "AXRole") if hit is not None else None,
            "cursor_moved": before["cursor"] != after["cursor"],
            "front": [before["front"], after["front"]],
            "z": [before["z"], after["z"]],
        }
    )
)
